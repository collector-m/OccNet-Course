import torch
from mmcv.runner import force_fp32
from torch.nn import functional as F
from torch.distributions import Normal
import numpy as np

from mmdet.models import DETECTORS
from mmdet3d.models.detectors import MVXFasterRCNN
from .cam_stream_lss import LiftSplatShoot
from mmcv.cnn import (build_conv_layer, build_norm_layer, build_upsample_layer,
                      constant_init, is_norm, kaiming_init)
from torchvision.utils import save_image
from mmcv.cnn import ConvModule, xavier_init
import torch.nn as nn
class SE_Block(nn.Module):
    def __init__(self, c):
        super().__init__()
        self.att = nn.Sequential(
            nn.AdaptiveAvgPool2d(1),
            nn.Conv2d(c, c, kernel_size=1, stride=1),
            nn.Sigmoid()
        )
    def forward(self, x):
        return x * self.att(x)

@DETECTORS.register_module()
class BEVF_FasterRCNN(MVXFasterRCNN):
    """Multi-modality BEVFusion using Faster R-CNN."""

    def __init__(self, lss=False, lc_fusion=False, camera_stream=False,
                camera_depth_range=[4.0, 45.0, 1.0], img_depth_loss_weight=1.0,  img_depth_loss_method='kld',
                grid=0.6, num_views=6, se=False,
                final_dim=(900, 1600), pc_range=[-50, -50, -5, 50, 50, 3], downsample=4, imc=256, lic=384, **kwargs):
        """
        Args:
            lss (bool): using default downsampled r18 BEV encoder in LSS.
            lc_fusion (bool): fusing multi-modalities of camera and LiDAR in BEVFusion.
            camera_stream (bool): using camera stream.
            camera_depth_range, img_depth_loss_weight, img_depth_loss_method: for depth supervision wich is not mentioned in paper.
            grid, num_views, final_dim, pc_range, downsample: args for LSS, see cam_stream_lss.py.
            imc (int): channel dimension of camera BEV feature.
            lic (int): channel dimension of LiDAR BEV feature.

        """
        super(BEVF_FasterRCNN, self).__init__(**kwargs)
        self.num_views = num_views
        self.lc_fusion = lc_fusion
        self.img_depth_loss_weight = img_depth_loss_weight
        self.img_depth_loss_method = img_depth_loss_method
        self.camera_depth_range = camera_depth_range
        self.lift = camera_stream
        self.se = se
        if camera_stream:
            self.lift_splat_shot_vis = LiftSplatShoot(lss=lss, grid=grid, inputC=imc, camC=64, 
            pc_range=pc_range, final_dim=final_dim, downsample=downsample)
        if lc_fusion:
            if se:
                self.seblock = SE_Block(lic)
            self.reduc_conv = ConvModule(
                lic + imc,
                lic,
                3,
                padding=1,
                conv_cfg=None,
                norm_cfg=dict(type='BN', eps=1e-3, momentum=0.01),
                act_cfg=dict(type='ReLU'),
                inplace=False)
            
        self.freeze_img = kwargs.get('freeze_img', False)
        self.init_weights(pretrained=kwargs.get('pretrained', None))
        self.freeze()

    def freeze(self):
        if self.freeze_img:
            if self.with_img_backbone:
                for param in self.img_backbone.parameters():
                    param.requires_grad = False
            if self.with_img_neck:
                for param in self.img_neck.parameters():
                    param.requires_grad = False
            if self.lift:
                for param in self.lift_splat_shot_vis.parameters():
                    param.requires_grad = False


    def extract_pts_feat(self, pts, img_feats, img_metas):
        """Extract features of points."""
        if not self.with_pts_backbone:
            return None
        voxels, num_points, coors = self.voxelize(pts)
        voxel_features = self.pts_voxel_encoder(voxels, num_points, coors,
                                                )
        batch_size = coors[-1, 0] + 1
        x = self.pts_middle_encoder(voxel_features, coors, batch_size)
        x = self.pts_backbone(x)
        if self.with_pts_neck:
            x = self.pts_neck(x)
        return x
    
    def extract_feat(self, points, img, img_metas, gt_bboxes_3d=None):
        """Extract features from images and points."""
        # img: [1, 6, 3, 448, 800]
        img_feats = self.extract_img_feat(img, img_metas) # img_feats: [6, 256, 56, 100]
        pts_feats = self.extract_pts_feat(points, img_feats, img_metas) # None

        if self.lift:
            BN, C, H, W = img_feats[0].shape # [6, 256, 56, 100]
            batch_size = BN//self.num_views # 6 // 6, bs=1
            img_feats_view = img_feats[0].view(batch_size, self.num_views, C, H, W) # [6, 256, 56, 100] -> [1, 6, 256, 56, 100]
            rots = []
            trans = []
            for sample_idx in range(batch_size): # per batch，逐batch处理
                rot_list = [] # 旋转列表
                trans_list = [] # 平移列表
                for mat in img_metas[sample_idx]['lidar2img']: # per-view, img->lidar
                    mat = torch.Tensor(mat).to(img_feats_view.device) # mat: [4, 4]
                    rot_list.append(mat.inverse()[:3, :3]) # rot_list: [3, 3]
                    trans_list.append(mat.inverse()[:3, 3].view(-1)) # trans_list: [3]
                rot_list = torch.stack(rot_list, dim=0) # [6, 3, 3]
                trans_list = torch.stack(trans_list, dim=0) # [6, 3]
                rots.append(rot_list)
                trans.append(trans_list)
            rots = torch.stack(rots) # [1, 6, 3, 3]
            trans = torch.stack(trans) # [1, 6, 3]
            lidar2img_rt = img_metas[sample_idx]['lidar2img']  #### extrinsic parameters for multi-view images, [4, 4]
            
            # img_bev_feat: [1, 256, 200, 200]
            # depth_dist: [1, 6, 41, 56, 100]
            img_bev_feat, depth_dist = self.lift_splat_shot_vis(img_feats_view, rots, trans, lidar2img_rt=lidar2img_rt, img_metas=img_metas)
            # print(img_bev_feat.shape, pts_feats[-1].shape)
            if pts_feats is None:
                pts_feats = [img_bev_feat] ####cam stream only, [1, 256, 200, 200]
            else: # Fusion stream
                if self.lc_fusion:
                    if img_bev_feat.shape[2:] != pts_feats[0].shape[2:]: # 如果尺寸不一致，双线性插值, 图像bev尺寸放缩到点云bev尺寸
                        img_bev_feat = F.interpolate(img_bev_feat, pts_feats[0].shape[2:], mode='bilinear', align_corners=True)
                    """
                    (reduc_conv): ConvModule(
                        (conv): Conv2d(640, 384, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1), bias=False)
                        (bn): BatchNorm2d(384, eps=0.001, momentum=0.01, affine=True, track_running_stats=True)
                        (activate): ReLU()
                    )
                    """
                    pts_feats = [self.reduc_conv(torch.cat([img_bev_feat, pts_feats[0]], dim=1))]
                    """
                    (seblock): SE_Block(
                        (att): Sequential(
                        (0): AdaptiveAvgPool2d(output_size=1)
                        (1): Conv2d(384, 384, kernel_size=(1, 1), stride=(1, 1))
                        (2): Sigmoid()
                        )
                    )
                    """
                    if self.se:
                        pts_feats = [self.seblock(pts_feats[0])]
        return dict(
            img_feats = img_feats,
            pts_feats = pts_feats,
            depth_dist = depth_dist
        )
        # return (img_feats, pts_feats, depth_dist)
    
    def simple_test(self, points, img_metas, img=None, rescale=False):
        """Test function without augmentaiton."""
        feature_dict = self.extract_feat(
            points, img=img, img_metas=img_metas)
        img_feats = feature_dict['img_feats']
        pts_feats = feature_dict['pts_feats'] 
        depth_dist = feature_dict['depth_dist']

        bbox_list = [dict() for i in range(len(img_metas))]
        if pts_feats and self.with_pts_bbox:
            bbox_pts = self.simple_test_pts(
                # pts_feats, img_feats, img_metas, rescale=rescale)
                pts_feats, img_metas, rescale=rescale)
            for result_dict, pts_bbox in zip(bbox_list, bbox_pts):
                result_dict['pts_bbox'] = pts_bbox
        if img_feats and self.with_img_bbox:
            bbox_img = self.simple_test_img(
                img_feats, img_metas, rescale=rescale)
            for result_dict, img_bbox in zip(bbox_list, bbox_img):
                result_dict['img_bbox'] = img_bbox
        return bbox_list

    def forward_train(self,
                      points=None, # points per sample
                      img_metas=None, # 图像信息，图像路径 转换矩阵等
                      gt_bboxes_3d=None, # 3d GT box
                      gt_labels_3d=None, # label
                      gt_labels=None, # label
                      gt_bboxes=None, # 2d GT box
                      img=None, # [1, 6, 3, 448, 800] B N C H W
                      img_depth=None,
                      proposals=None,
                      gt_bboxes_ignore=None):
        feature_dict = self.extract_feat(
            points, img=img, img_metas=img_metas, gt_bboxes_3d=gt_bboxes_3d)
        img_feats = feature_dict['img_feats'] # [6, 256, 56, 100]
        pts_feats = feature_dict['pts_feats'] # [1, 256, 200, 200]
        depth_dist = feature_dict['depth_dist'] # [1, 6, 41, 56, 100]

        losses = dict()
        if pts_feats:
            losses_pts = self.forward_pts_train(pts_feats, gt_bboxes_3d,
                                                gt_labels_3d, img_metas,
                                                gt_bboxes_ignore)
            losses.update(losses_pts)
        if img_feats:
            losses_img = self.forward_img_train(
                img_feats,
                img_metas=img_metas,
                gt_bboxes=gt_bboxes,
                gt_labels=gt_labels,
                gt_bboxes_ignore=gt_bboxes_ignore,
                proposals=proposals)
            if img_depth is not None: # None
                loss_depth = self.depth_dist_loss(depth_dist, img_depth, loss_method=self.img_depth_loss_method, img=img) * self.img_depth_loss_weight
                losses.update(img_depth_loss=loss_depth)
            losses.update(losses_img)
        return losses
    
    def depth_dist_loss(self, predict_depth_dist, gt_depth, loss_method='kld', img=None):
        # predict_depth_dist: B, N, D, H, W
        # gt_depth: B, N, H', W'
        B, N, D, H, W = predict_depth_dist.shape
        guassian_depth, min_depth = gt_depth[..., 1:], gt_depth[..., 0]
        mask = (min_depth>=self.camera_depth_range[0]) & (min_depth<=self.camera_depth_range[1])
        mask = mask.view(-1)
        guassian_depth = guassian_depth.view(-1, D)[mask]
        predict_depth_dist = predict_depth_dist.permute(0, 1, 3, 4, 2).reshape(-1, D)[mask]
        if loss_method=='kld':
            loss = F.kl_div(torch.log(predict_depth_dist), guassian_depth, reduction='mean', log_target=False)
        elif loss_method=='mse':
            loss = F.mse_loss(predict_depth_dist, guassian_depth)
        else:
            raise NotImplementedError
        return loss

