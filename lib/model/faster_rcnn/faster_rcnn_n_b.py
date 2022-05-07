import random
import cv2
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import torchvision.models as models
from torch.autograd import Variable
import numpy as np
from model.utils.config import cfg
from model.rpn.rpn import _RPN
from model.faster_rcnn.srm import SRM
from model.faster_rcnn.bayer import BayarConv2d
from model.faster_rcnn.compact_bilinear_pooling import CompactBilinearPooling
from model.faster_rcnn.mcb import mcb
from model.roi_layers import ROIAlign, ROIPool
# from model.roi_pooling.modules.roi_pool import _RoIPooling
# from model.roi_align.modules.roi_align import RoIAlignAvg
import matplotlib.pyplot as plt
from model.rpn.proposal_target_layer_cascade import _ProposalTargetLayer
import time
import pdb
from model.utils.net_utils import _smooth_l1_loss, _crop_pool_layer, _affine_grid_gen, _affine_theta
from model.faster_rcnn.CBAM import cbam_block
from model.faster_rcnn.ECA import eca_block
from model.faster_rcnn.SEnet import se_block
from model.faster_rcnn.dual import _DAHead
from model.faster_rcnn.ca import CoordAtt
attention_block = [se_block, cbam_block, eca_block,_DAHead,CoordAtt]


class RRU_double_conv(nn.Module):
    def __init__(self, in_ch, out_ch):
        super(RRU_double_conv, self).__init__()
        self.conv = nn.Sequential(
            nn.Conv2d(in_ch, out_ch, 3, padding=2, dilation=2),
            nn.GroupNorm(32, out_ch),
            nn.ReLU(inplace=True),
            nn.Conv2d(out_ch, out_ch, 3, padding=2, dilation=2),
            nn.GroupNorm(32, out_ch)
        )

    def forward(self, x):
        x = self.conv(x)
        return x

class RRU_BLOCK(nn.Module):
    def __init__(self, in_ch, out_ch):
        super(RRU_BLOCK, self).__init__()
        self.conv = RRU_double_conv(in_ch, out_ch)
        self.relu = nn.ReLU(inplace=True)

        self.res_conv = nn.Sequential(
            nn.Conv2d(in_ch, out_ch, kernel_size=1, bias=False),
            nn.GroupNorm(32, out_ch)
        )
        self.res_conv_back = nn.Sequential(
            nn.Conv2d(out_ch, in_ch, kernel_size=1, bias=False)
        )

    def forward(self, x):
        # the first ring conv
        ft1 = self.conv(x)
        r1 = self.relu(ft1 + self.res_conv(x))
        # the second ring conv
        ft2 = self.res_conv_back(r1)
        x = torch.mul(1 + torch.sigmoid(ft2), x)
        # the third ring conv
        ft3 = self.conv(x)
        r3 = self.relu(ft3 + self.res_conv(x))

        return r3




def bgr2gray(bgr):
    b, g, r = bgr[:, 0, :, :], bgr[:, 1, :, :], bgr[:, 2, :, :]
    gray =  0.1140*b + 0.5870*g + 0.2989*r
   # gray =  0.3333*b + 0.3333*g + 0.3333*r
    gray = torch.unsqueeze(gray, 1)
    return gray

class _fasterRCNN(nn.Module):
    """ faster RCNN """

    def __init__(self, classes, class_agnostic,phi=2):
        super(_fasterRCNN, self).__init__()
        self.SRM = SRM()
        self.bayerconv = BayarConv2d()
      #  self.bayerconv1 = BayarConv2d(in_channels=3)
        self.classes = classes
        self.n_classes = len(classes)
        self.mask = 0
        self.cnt = 0
        self.class_agnostic = class_agnostic
        # loss
        self.RCNN_loss_cls = 0
        self.RCNN_loss_bbox = 0
        self.relu = nn.ReLU(inplace=True)
        # define rpn
        self.RCNN_rpn = _RPN(self.dout_base_model)
        self.RCNN_proposal_target = _ProposalTargetLayer(self.n_classes)
        #self.scale = nn.Parameter(torch.FloatTensor([0]),requires_grad=True)
        # self.RCNN_roi_pool = _RoIPooling(cfg.POOLING_SIZE, cfg.POOLING_SIZE, 1.0/16.0)
        # self.RCNN_roi_align = RoIAlignAvg(cfg.POOLING_SIZE, cfg.POOLING_SIZE, 1.0/16.0)

        self.RCNN_roi_pool = ROIPool((cfg.POOLING_SIZE, cfg.POOLING_SIZE), 1.0 / 16.0)
        self.RCNN_roi_align = ROIAlign((cfg.POOLING_SIZE, cfg.POOLING_SIZE), 1.0 / 16.0, 0)
        self.phi  = phi
        if 1 <= self.phi and self.phi <= 4:
            self.one    = attention_block[self.phi - 1](1024)
            self.two    = attention_block[self.phi - 1](1024)
        elif self.phi == 5:
            self.one    = attention_block[self.phi - 1](1024,1024)
            self.two    = attention_block[self.phi - 1](1024,1024)   

    def forward(self, im_data, im_info, gt_boxes, num_boxes):
   
        batch_size = im_data.size(0)

        im_info = im_info.data
        gt_boxes = gt_boxes.data
        num_boxes = num_boxes.data
      
        im_data_n = bgr2gray(im_data)
        im_data_n = self.bayerconv(im_data_n)
        im_data_n = self.SRM(im_data_n)  # 这是噪声流
 
#------------------------------------------------------
      #  im_data_n += self.scale*torch.sigmoid(im_data_n)
#------------------------------------------------------
        if self.mask == 1:
            img = im_data_n.clone()
            img = img.reshape(im_data.size(1),im_data.size(2),im_data.size(3))
            img = img.cpu().detach().numpy()
            img = np.transpose(img, (1, 2, 0))
            img = img[:,:,::-1]
            plt.imshow(img.astype('uint8'))
            path = '/home/stu4/user/ysq/Newfaster-rcnn.pytorch/noise/test' + str(self.cnt) + '.jpg'
            #plt.savefig(path, bbox_inches='tight',dpi = 600)
            plt.savefig(path,dpi = 600)
            self.cnt = self.cnt +1 
        # feed image data to base model to obtain base feature map
        base_feat = self.RCNN_base(im_data)
        # b 1024 h w
        base_feat_n = self.RCNN_base_n(im_data_n)
        
        if 1 <= self.phi and self.phi <= 4:
            base_feat= self.one(base_feat)
            base_feat_n = self.two(base_feat_n) 
        elif self.phi == 5:
            base_feat= self.one(base_feat)
            base_feat_n = self.two(base_feat_n) 
        # feed base feature map tp RPN to obtain rois
        rois, rpn_loss_cls, rpn_loss_bbox = self.RCNN_rpn(base_feat, im_info, gt_boxes, num_boxes)

        # if it is training phrase, then use ground trubut bboxes for refining
        if self.training:
            roi_data = self.RCNN_proposal_target(rois, gt_boxes, num_boxes)
            rois, rois_label, rois_target, rois_inside_ws, rois_outside_ws = roi_data

            rois_label = Variable(rois_label.view(-1).long())
            rois_target = Variable(rois_target.view(-1, rois_target.size(2)))
            rois_inside_ws = Variable(rois_inside_ws.view(-1, rois_inside_ws.size(2)))
            rois_outside_ws = Variable(rois_outside_ws.view(-1, rois_outside_ws.size(2)))
        else:
            rois_label = None
            rois_target = None
            rois_inside_ws = None
            rois_outside_ws = None
            rpn_loss_cls = 0
            rpn_loss_bbox = 0

        rois = Variable(rois)
        # do roi pooling based on predicted rois

        if cfg.POOLING_MODE == 'align':
            pooled_feat = self.RCNN_roi_align(base_feat, rois.view(-1, 5))
            pooled_feat_n = self.RCNN_roi_align(base_feat_n, rois.view(-1, 5))  # 这里是加的
        elif cfg.POOLING_MODE == 'pool':
            pooled_feat = self.RCNN_roi_pool(base_feat, rois.view(-1, 5))
            pooled_feat_n = self.RCNN_roi_pool(base_feat_n, rois.view(-1, 5))  # 这里是加的
     
        # feed pooled features to fc model
        pooled_feat = self._head_to_tail(pooled_feat)
        pooled_feat_n = self._head_to_tail_n(pooled_feat_n)
        print(pooled_feat.shape)

        # feed pooled features to compact bilinear pooling layer
        # 128 2048 4 4
        Bipooling = CompactBilinearPooling(2048, 2048, 16384).cuda()  # 这些代码是我加的 resnet的参数吧 展平了
        Bipooling.train()
        bipooled_feat = Bipooling(pooled_feat, pooled_feat_n)
        bipooled_feat = torch.mul(torch.sign(bipooled_feat), torch.sqrt(torch.abs(bipooled_feat) + 1e-12))

        #bipooled_feat = bipooled_feat / torch.norm(bipooled_feat, dim=0)
        bipooled_feat =  F.normalize(bipooled_feat, dim=1,p=2)
        # compute object classification probability
        print(bipooled_feat.shape)
        cls_score = self.RCNN_cls_score(bipooled_feat)
        cls_prob = F.softmax(cls_score, 1)
        pooled_feat = pooled_feat.mean(3).mean(2)
        bbox_pred = self.RCNN_bbox_pred(pooled_feat)
        if self.training and not self.class_agnostic:
            # select the corresponding columns according to roi labels
            bbox_pred_view = bbox_pred.view(bbox_pred.size(0), int(bbox_pred.size(1) / 4), 4)
            bbox_pred_select = torch.gather(bbox_pred_view, 1,
                                            rois_label.view(rois_label.size(0), 1, 1).expand(rois_label.size(0), 1, 4))
            bbox_pred = bbox_pred_select.squeeze(1)


        RCNN_loss_cls = 0
        RCNN_loss_bbox = 0

        if self.training:
            # classification loss
            RCNN_loss_cls = F.cross_entropy(cls_score, rois_label)

            # bounding box regression L1 loss
            RCNN_loss_bbox = _smooth_l1_loss(bbox_pred, rois_target, rois_inside_ws, rois_outside_ws)

        cls_prob = cls_prob.view(batch_size, rois.size(1), -1)
        bbox_pred = bbox_pred.view(batch_size, rois.size(1), -1)

        return rois, cls_prob, bbox_pred, rpn_loss_cls, rpn_loss_bbox, RCNN_loss_cls, RCNN_loss_bbox, rois_label


    def _init_weights(self):
        def normal_init(m, mean, stddev, truncated=False):
            """
            weight initalizer: truncated normal and random normal.
            """
            # x is a parameter
            if truncated:
                m.weight.data.normal_().fmod_(2).mul_(stddev).add_(mean)  # not a perfect approximation
            else:
                m.weight.data.normal_(mean, stddev)
                m.bias.data.zero_()

        normal_init(self.RCNN_rpn.RPN_Conv, 0, 0.01, cfg.TRAIN.TRUNCATED)
        normal_init(self.RCNN_rpn.RPN_cls_score, 0, 0.01, cfg.TRAIN.TRUNCATED)
        normal_init(self.RCNN_rpn.RPN_bbox_pred, 0, 0.01, cfg.TRAIN.TRUNCATED)
        normal_init(self.RCNN_cls_score, 0, 0.01, cfg.TRAIN.TRUNCATED)
        normal_init(self.RCNN_bbox_pred, 0, 0.001, cfg.TRAIN.TRUNCATED)

    def create_architecture(self):
        self._init_modules()
        self._init_weights()

