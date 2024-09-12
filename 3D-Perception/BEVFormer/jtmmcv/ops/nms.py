import os

import numpy as np
import jittor


# This function is modified from: https://github.com/pyjittor/vision/
class NMSop(jittor.Function):

    @staticmethod
    def execute(bboxes, scores, iou_threshold, offset, score_threshold,
                max_num):
        is_filtering_by_score = score_threshold > 0
        if is_filtering_by_score:
            valid_mask = scores > score_threshold
            bboxes, scores = bboxes[valid_mask], scores[valid_mask]
            valid_inds = jittor.nonzero(
                valid_mask).squeeze(dim=1)
        
        scores = jittor.reshape(scores, (-1, 1))
        bboxes = jittor.concat([bboxes, scores], dim=1)
        
        inds = jittor.nms(
            bboxes, float(iou_threshold))

        if max_num > 0:
            inds = inds[:max_num]
        if is_filtering_by_score:
            inds = valid_inds[inds]
        return inds
    

def nms(boxes, scores, iou_threshold, offset=0, score_threshold=0, max_num=-1):
    """Dispatch to either CPU or GPU NMS implementations.

    The input can be either jittor var or numpy array. GPU NMS will be used
    if the input is gpu tensor, otherwise CPU NMS
    will be used. The returned type will always be the same as inputs.

    Arguments:
        boxes (jittor.Var or np.ndarray): boxes in shape (N, 4).
        scores (jittor.Var or np.ndarray): scores in shape (N, ).
        iou_threshold (float): IoU threshold for NMS.
        offset (int, 0 or 1): boxes' width or height is (x2 - x1 + offset).
        score_threshold (float): score threshold for NMS.
        max_num (int): maximum number of boxes after NMS.

    Returns:
        tuple: kept dets(boxes and scores) and indice, which is always the \
            same data type as the input.

    Example:
        >>> boxes = np.array([[49.1, 32.4, 51.0, 35.9],
        >>>                   [49.3, 32.9, 51.0, 35.3],
        >>>                   [49.2, 31.8, 51.0, 35.4],
        >>>                   [35.1, 11.5, 39.1, 15.7],
        >>>                   [35.6, 11.8, 39.3, 14.2],
        >>>                   [35.3, 11.5, 39.9, 14.5],
        >>>                   [35.2, 11.7, 39.7, 15.7]], dtype=np.float32)
        >>> scores = np.array([0.9, 0.9, 0.5, 0.5, 0.5, 0.4, 0.3],\
               dtype=np.float32)
        >>> iou_threshold = 0.6
        >>> dets, inds = nms(boxes, scores, iou_threshold)
        >>> assert len(inds) == len(dets) == 3
    """
    assert isinstance(boxes, (jittor.Var, np.ndarray))
    assert isinstance(scores, (jittor.Var, np.ndarray))
    is_numpy = False
    if isinstance(boxes, np.ndarray):
        is_numpy = True
        boxes = jittor.array(boxes)
    if isinstance(scores, np.ndarray):
        scores = jittor.array(scores)
    assert boxes.size(1) == 4
    assert boxes.size(0) == scores.size(0)
    assert offset in (0, 1)


    inds = NMSop.execute(boxes, scores, iou_threshold, offset, score_threshold, max_num)
    dets = jittor.cat((boxes[inds], scores[inds].reshape(-1, 1)), dim=1)
    if is_numpy:
        dets = dets.numpy()
        inds = inds.numpy()
    return dets, inds



def batched_nms(boxes, scores, idxs, nms_cfg, class_agnostic=False):
    """Performs non-maximum suppression in a batched fashion.
    目前支持normal nms
    Modified from https://github.com/pyjittor/vision/blob
    /505cd6957711af790211896d32b40291bea1bc21/jittorvision/ops/boxes.py#L39.
    In order to perform NMS independently per class, we add an offset to all
    the boxes. The offset is dependent only on the class idx, and is large
    enough so that boxes from different classes do not overlap.

    Arguments:
        boxes (jittor.Var): boxes in shape (N, 4).
        scores (jittor.Var): scores in shape (N, ).
        idxs (jittor.Var): each index value correspond to a bbox cluster,
            and NMS will not be applied between elements of different idxs,
            shape (N, ).
        nms_cfg (dict): specify nms type and other parameters like iou_thr.
            Possible keys includes the following.

            - iou_thr (float): IoU threshold used for NMS.
            - split_thr (float): threshold number of boxes. In some cases the
                number of boxes is large (e.g., 200k). To avoid OOM during
                training, the users could set `split_thr` to a small value.
                If the number of boxes is greater than the threshold, it will
                perform NMS on each group of boxes separately and sequentially.
                Defaults to 10000.
        class_agnostic (bool): if true, nms is class agnostic,
            i.e. IoU thresholding happens over all boxes,
            regardless of the predicted class.

    Returns:
        tuple: kept dets and indice.
    """
    nms_cfg_ = nms_cfg.copy()
    class_agnostic = nms_cfg_.pop('class_agnostic', class_agnostic)
    if class_agnostic:
        boxes_for_nms = boxes
    else:
        max_coordinate = boxes.max()
        offsets = idxs.to(boxes) * (max_coordinate + jittor.Var(1).to(boxes))
        boxes_for_nms = boxes + offsets[:, None]

    iou_thr = nms_cfg_.pop('iou_thr', 0)
    split_thr = nms_cfg_.pop('split_thr', 10000)
    # Won't split to multiple nms nodes when exporting to onnx
    if boxes_for_nms.shape[0] < split_thr:
        
        dets, keep = nms(boxes_for_nms, scores, iou_thr)
        boxes = boxes[keep]
        # -1 indexing works abnormal in TensorRT
        # This assumes `dets` has 5 dimensions where
        # the last dimension is score.
        # TODO: more elegant way to handle the dimension issue.
        # Some type of nms would reweight the score, such as SoftNMS
        scores = dets[:, 4]
    else:
        max_num = nms_cfg_.pop('max_num', -1)
        total_mask = scores.new_zeros(scores.size())
        # Some type of nms would reweight the score, such as SoftNMS
        scores_after_nms = scores.new_zeros(scores.size())
        for id in jittor.unique(idxs):
            mask = (idxs == id).nonzero().view(-1)
            dets, keep = nms(boxes_for_nms[mask], scores[mask], iou_thr)
            total_mask[mask[keep]] = True
            scores_after_nms[mask[keep]] = dets[:, -1]
        keep = total_mask.nonzero().view(-1)

        scores, inds = scores_after_nms[keep].sort(descending=True)
        keep = keep[inds]
        boxes = boxes[keep]

        if max_num > 0:
            keep = keep[:max_num]
            boxes = boxes[:max_num]
            scores = scores[:max_num]

    return jittor.concat([boxes, scores[:, None]], -1), keep


if __name__ == '__main__':
    
    boxes = jittor.array([[4., 3., 5., 3.],
                        [4., 3., 5., 4.],
                        [3., 1., 3., 1.],
                        [3., 1., 3., 1.],
                        [3., 2., 3., 6.],
                        [3., 3., 3., 5.]], dtype=jittor.float32)

    scores = jittor.array([0.7, 0.6, 0.5, 0.4, 0.3, 0.2], dtype=jittor.float32)
    idxs = jittor.array([0, 1, 0, 1, 0, 1], dtype=jittor.int64)
    nms_cfg = {'split_thr': 10000}
    class_agnostic = False
    print(batched_nms(boxes, scores, idxs, nms_cfg, class_agnostic))