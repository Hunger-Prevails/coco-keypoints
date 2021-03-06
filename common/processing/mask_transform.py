# --------------------------------------------------------
# Fully Convolutional Instance-aware Semantic Segmentation
# Copyright (c) 2017 Microsoft
# Licensed under The Apache-2.0 License [see LICENSE for details]
# Written by Haozhi Qi, Guodong Zhang
# --------------------------------------------------------

import numpy as np
import cv2

from bbox_transform import bbox_overlaps
from nms import py_nms_wrapper, gpu_nms_wrapper
from ..cython.gpu_mv import mv as mask_voting_kernel
from ..pycocotools import mask as mask_util


def intersect_box_mask(ex_box, gt_box, gt_mask):
    """
    This function calculate the intersection part of a external box
    and gt_box, mask it according to gt_mask
    Args:
        ex_box: external ROIS
        gt_box: ground truth boxes
        gt_mask: ground truth masks, not been resized yet
    Returns:
        regression_target: logical numpy array
    """
    x1 = max(ex_box[0], gt_box[0])
    y1 = max(ex_box[1], gt_box[1])
    x2 = min(ex_box[2], gt_box[2])
    y2 = min(ex_box[3], gt_box[3])
    if x1 > x2 or y1 > y2:
        return np.zeros((28, 28), dtype=bool)

    ex_starty = y1 - ex_box[1]
    ex_startx = x1 - ex_box[0]
    inter_maskb = gt_mask[y1:y2+1, x1:x2+1]
    regression_target = np.zeros((ex_box[3] - ex_box[1] + 1, ex_box[2] - ex_box[0] + 1))
    regression_target[ex_starty: ex_starty + inter_maskb.shape[0],
                      ex_startx: ex_startx + inter_maskb.shape[1]] = inter_maskb

    return regression_target


def mask_overlap(box1, box2, mask1, mask2):
    """
    This function calculate region IOU when masks are
    inside different boxes
    Returns:
        intersection over unions of this two masks
    """
    x1 = max(box1[0], box2[0])
    y1 = max(box1[1], box2[1])
    x2 = min(box1[2], box2[2])
    y2 = min(box1[3], box2[3])
    if x1 > x2 or y1 > y2:
        return 0
    w = x2 - x1 + 1
    h = y2 - y1 + 1
    # get masks in the intersection part
    start_ya = y1 - box1[1]
    start_xa = x1 - box1[0]
    inter_maska = mask1[start_ya: start_ya + h, start_xa:start_xa + w]

    start_yb = y1 - box2[1]
    start_xb = x1 - box2[0]
    inter_maskb = mask2[start_yb: start_yb + h, start_xb:start_xb + w]

    assert inter_maska.shape == inter_maskb.shape

    inter = np.logical_and(inter_maskb, inter_maska).sum()
    union = mask1.sum() + mask2.sum() - inter
    if union < 1.0:
        return 0
    return float(inter) / float(union)


def mask_aggregation(boxes, masks, mask_weights, im_width, im_height, binary_thresh=0.4):
    """
    This function implements mask voting mechanism to give finer mask
    n is the candidate boxes (masks) number
    Args:
        masks: All masks need to be aggregated (n x sz x sz)
        mask_weights: class score associated with each mask (n x 1)
        boxes: tight box enclose each mask (n x 4)
        im_width, im_height: image information
    """
    assert boxes.shape[0] == len(masks) and boxes.shape[0] == mask_weights.shape[0]
    im_mask = np.zeros((im_height, im_width))
    for mask_ind in xrange(len(masks)):
        box = np.round(boxes[mask_ind]).astype(int)
        mask = (masks[mask_ind] >= binary_thresh).astype(float)

        mask_weight = mask_weights[mask_ind]
        im_mask[box[1]:box[3]+1, box[0]:box[2]+1] += mask * mask_weight
    [r, c] = np.where(im_mask >= binary_thresh)
    if len(r) == 0 or len(c) == 0:
        min_y = np.ceil(im_height / 2).astype(int)
        min_x = np.ceil(im_width / 2).astype(int)
        max_y = min_y
        max_x = min_x
    else:
        min_y = np.min(r)
        min_x = np.min(c)
        max_y = np.max(r)
        max_x = np.max(c)

    clipped_mask = im_mask[min_y:max_y+1, min_x:max_x+1]
    clipped_box = np.array((min_x, min_y, max_x, max_y), dtype=np.float32)
    return clipped_mask, clipped_box


def cpu_mask_voting(masks, boxes, scores, num_classes, max_per_image, im_width, im_height,
                    nms_thresh, merge_thresh, binary_thresh=0.4):
    """
    Wrapper function for mask voting, note we already know the class of boxes and masks
    """
    masks = masks.astype(np.float32)
    mask_size = masks.shape[-1]
    nms = py_nms_wrapper(nms_thresh)
    # apply nms and sort to get first images according to their scores

    # Intermediate results
    t_boxes = [[] for _ in xrange(num_classes)]
    t_scores = [[] for _ in xrange(num_classes)]
    t_all_scores = []
    for i in xrange(1, num_classes):
        dets = np.hstack((boxes.astype(np.float32), scores[:, i:i + 1]))
        inds = nms(dets)
        num_keep = min(len(inds), max_per_image)
        inds = inds[:num_keep]
        t_boxes[i] = boxes[inds]
        t_scores[i] = scores[inds, i]
        t_all_scores.extend(scores[inds, i])

    sorted_scores = np.sort(t_all_scores)[::-1]
    num_keep = min(len(sorted_scores), max_per_image)
    thresh = max(sorted_scores[num_keep - 1], 1e-3)

    for i in xrange(1, num_classes):
        keep = np.where(t_scores[i] >= thresh)
        t_boxes[i] = t_boxes[i][keep]
        t_scores[i] = t_scores[i][keep]

    num_detect = boxes.shape[0]
    res_mask = [[] for _ in xrange(num_detect)]
    for i in xrange(num_detect):
        box = np.round(boxes[i]).astype(int)
        mask = cv2.resize(masks[i, 0].astype(np.float32), (box[2] - box[0] + 1, box[3] - box[1] + 1))
        res_mask[i] = mask

    list_result_box = [[] for _ in xrange(num_classes)]
    list_result_mask = [[] for _ in xrange(num_classes)]
    for c in xrange(1, num_classes):
        num_boxes = len(t_boxes[c])
        masks_ar = np.zeros((num_boxes, 1, mask_size, mask_size))
        boxes_ar = np.zeros((num_boxes, 4))
        for i in xrange(num_boxes):
            # Get weights according to their segmentation scores
            cur_ov = bbox_overlaps(boxes.astype(np.float), t_boxes[c][i, np.newaxis].astype(np.float))
            cur_inds = np.where(cur_ov >= merge_thresh)[0]
            cur_weights = scores[cur_inds, c]
            cur_weights = cur_weights / sum(cur_weights)
            # Re-format mask when passing it to mask_aggregation
            p_mask = [res_mask[j] for j in list(cur_inds)]
            # do mask aggregation
            orig_mask, boxes_ar[i] = mask_aggregation(boxes[cur_inds], p_mask, cur_weights, im_width, im_height, binary_thresh)
            masks_ar[i, 0] = cv2.resize(orig_mask.astype(np.float32), (mask_size, mask_size))
        boxes_scored_ar = np.hstack((boxes_ar, t_scores[c][:, np.newaxis]))
        list_result_box[c] = boxes_scored_ar
        list_result_mask[c] = masks_ar
    return list_result_mask, list_result_box


def gpu_mask_voting(masks, boxes, scores, num_classes, max_per_image, im_width, im_height,
                    nms_thresh, merge_thresh, binary_thresh=0.4, device_id=0):
    """
    A wrapper function, note we already know the class of boxes and masks
    """
    nms = gpu_nms_wrapper(nms_thresh, device_id)
    # Intermediate results
    t_boxes = [[] for _ in xrange(num_classes)]
    t_scores = [[] for _ in xrange(num_classes)]
    t_all_scores = []
    for i in xrange(1, num_classes):
        dets = np.hstack((boxes.astype(np.float32), scores[:, i:i+1]))
        inds = nms(dets)
        num_keep = min(len(inds), max_per_image)
        inds = inds[:num_keep]
        t_boxes[i] = boxes[inds]
        t_scores[i] = scores[inds, i]
        t_all_scores.extend(scores[inds, i])

    sorted_scores = np.sort(t_all_scores)[::-1]
    num_keep = min(len(sorted_scores), max_per_image)
    thresh = max(sorted_scores[num_keep - 1], 1e-3)

    # inds array to record which mask should be aggregated together
    candidate_inds = []
    # weight for each element in the candidate inds
    candidate_weights = []
    # start position for candidate array
    candidate_start = []
    candidate_scores = []
    class_bar = [[] for _ in xrange(num_classes)]

    for i in xrange(1, num_classes):
        keep = np.where(t_scores[i] >= thresh)
        t_boxes[i] = t_boxes[i][keep]
        t_scores[i] = t_scores[i][keep]

    # organize helper variable for gpu mask voting
    for c in xrange(1, num_classes):
        num_boxes = len(t_boxes[c])
        for i in xrange(num_boxes):
            cur_ov = bbox_overlaps(boxes.astype(np.float), t_boxes[c][i, np.newaxis].astype(np.float))
            cur_inds = np.where(cur_ov >= merge_thresh)[0]
            candidate_inds.extend(cur_inds)
            cur_weights = scores[cur_inds, c]
            cur_weights = cur_weights / sum(cur_weights)
            candidate_weights.extend(cur_weights)
            candidate_start.append(len(candidate_inds))
        candidate_scores.extend(t_scores[c])
        class_bar[c] = len(candidate_scores)

    candidate_inds = np.array(candidate_inds, dtype=np.int32)
    candidate_weights = np.array(candidate_weights, dtype=np.float32)
    candidate_start = np.array(candidate_start, dtype=np.int32)
    candidate_scores = np.array(candidate_scores, dtype=np.float32)

    # the input masks/boxes are relatively large
    # select only a subset of them are useful for mask merge
    unique_inds = np.unique(candidate_inds)
    unique_inds_order = unique_inds.argsort()
    unique_map = {}
    for i in xrange(len(unique_inds)):
        unique_map[unique_inds[i]] = unique_inds_order[i]
    for i in xrange(len(candidate_inds)):
        candidate_inds[i] = unique_map[candidate_inds[i]]
    boxes = boxes[unique_inds, ...]
    masks = masks[unique_inds, ...]

    boxes = np.round(boxes)
    result_mask, result_box = mask_voting_kernel(boxes, masks, candidate_inds, candidate_start, candidate_weights,
                                                 binary_thresh, im_height, im_width, device_id)
    result_box = np.hstack((result_box, candidate_scores[:, np.newaxis]))

    list_result_box = [[] for _ in xrange(num_classes)]
    list_result_mask = [[] for _ in xrange(num_classes)]
    cls_start = 0
    for i in xrange(1, num_classes):
        cls_end = class_bar[i]
        cls_box = result_box[cls_start:cls_end, :]
        cls_mask = result_mask[cls_start:cls_end, :]
        valid_ind = np.where((cls_box[:, 2] > cls_box[:, 0]) &
                             (cls_box[:, 3] > cls_box[:, 1]))[0]
        list_result_box[i] = cls_box[valid_ind, :]
        list_result_mask[i] = cls_mask[valid_ind, :]
        cls_start = cls_end

    return list_result_mask, list_result_box


def polys_or_rles_to_boxes(polys_or_rles):
    """Convert a list of polys_or_rles into an array of tight bounding boxes."""
    boxes = np.zeros((len(polys_or_rles), 4), dtype=np.float32)
    for i, ann in enumerate(polys_or_rles):
        if type(ann) == list:
            # Polygon format
            x0 = min(min(p[::2]) for p in ann)
            x1 = max(max(p[::2]) for p in ann)
            y0 = min(min(p[1::2]) for p in ann)
            y1 = max(max(p[1::2]) for p in ann)
            boxes[i, :] = [x0, y0, x1, y1]
        else:
            # RLE format
            assert False
    return boxes


def polys_or_rles_to_masks(polys_or_rles, boxes, mask_height, mask_width):
    assert len(polys_or_rles) == len(boxes)
    masks = np.zeros((len(boxes), mask_height, mask_width), dtype=np.float32)

    boxes_height = boxes[:, 3] - boxes[:, 1] + 1
    boxes_width = boxes[:, 2] - boxes[:, 0] + 1
    for i, ann in enumerate(polys_or_rles):
        if type(ann) == list:
            # Polygon format
            polys = []
            for poly in ann:
                poly = np.array(poly, dtype=np.float32)
                poly[1::2] = (poly[1::2] - boxes[i, 1]) * mask_height / boxes_height[i]
                poly[0::2] = (poly[0::2] - boxes[i, 0]) * mask_width / boxes_width[i]
                polys.append(poly)
            rle = mask_util.frPyObjects(polys, mask_height, mask_width)
            mask = np.array(mask_util.decode(rle), dtype=np.float32)
            mask = np.sum(mask, axis=2)
            masks[i, :, :] = np.array(mask > 0, dtype=np.float32)
        else:
            # RLE format
            assert False
            # assert type(ann) == dict and 'counts' in ann and type(ann['counts']) == list
            # rle = mask_util.frPyObjects(ann, 1, 1)
            # mask = np.array(mask_util.decode(rle), dtype=np.float32)
            # mask = mask[int(boxes[i, 1]):int(boxes[i, 3]) + 1, int(boxes[i, 0]):int(boxes[i, 2]) + 1]
            # mask = cv2.resize(mask, (mask_width, mask_height), interpolation=cv2.INTER_LINEAR)
            # masks[i, :, :] = mask >= 0.5
    return masks
