import math
import os
from collections import OrderedDict
from pathlib import Path
from itertools import permutations
import math

import cv2
import numpy as np
from numpy.core.fromnumeric import amin

import craft_text_detector.file_utils as file_utils
import craft_text_detector.torch_utils as torch_utils

CRAFT_GDRIVE_URL = "https://drive.google.com/uc?id=1bupFXqT-VU6Jjeul13XP7yx2Sg5IHr4J"
REFINENET_GDRIVE_URL = (
    "https://drive.google.com/uc?id=1xcE9qpJXp4ofINwXWVhhQIh9S8Z7cuGj"
)


# unwarp corodinates
def warpCoord(Minv, pt):
    out = np.matmul(Minv, (pt[0], pt[1], 1))
    return np.array([out[0] / out[2], out[1] / out[2]])


def copyStateDict(state_dict):
    if list(state_dict.keys())[0].startswith("module"):
        start_idx = 1
    else:
        start_idx = 0
    new_state_dict = OrderedDict()
    for k, v in state_dict.items():
        name = ".".join(k.split(".")[start_idx:])
        new_state_dict[name] = v
    return new_state_dict


def load_craftnet_model(cuda: bool = False):
    '''
    Loads and returns the craftnet model with the state value as defined in the
    state dictionary
    Args:   
        cuda (bool): if True, to use GPU for compute else cpu
    Returns
        A pytorch model object
    '''
    # get craft net path
    home_path = str(Path.home())
    weight_path = os.path.join(
        home_path, ".craft_text_detector", "weights", "craft_mlt_25k.pth"
    )
    # load craft net
    from craft_text_detector.models.craftnet import CraftNet

    craft_net = CraftNet()  # initialize

    # check if weights are already downloaded, if not download
    url = CRAFT_GDRIVE_URL
    if os.path.isfile(weight_path) is not True:
        print("Craft text detector weight will be downloaded to {}".format(weight_path))

        file_utils.download(url=url, save_path=weight_path)

    # arange device
    if cuda:
        craft_net.load_state_dict(copyStateDict(torch_utils.load(weight_path)))

        craft_net = craft_net.cuda()
        craft_net = torch_utils.DataParallel(craft_net)
        torch_utils.cudnn_benchmark = False
    else:
        craft_net.load_state_dict(
            copyStateDict(torch_utils.load(weight_path, map_location="cpu"))
        )
    craft_net.eval()
    return craft_net


def load_refinenet_model(cuda: bool = False):
    '''
    Load a refine net model, the refine net is used to make better
    heatmaps based on the outputs and intermediate features of craft.
    It is also trained using the same semisupervised objective function.
    Args:   
        cuda (bool): if True, to use GPU for compute else cpu
    Returns
        A pytorch model object
    '''
    # get refine net path
    home_path = str(Path.home())
    weight_path = os.path.join(
        home_path, ".craft_text_detector", "weights", "craft_refiner_CTW1500.pth"
    )
    # load refine net
    from craft_text_detector.models.refinenet import RefineNet

    refine_net = RefineNet()  # initialize

    # check if weights are already downloaded, if not download
    url = REFINENET_GDRIVE_URL
    if os.path.isfile(weight_path) is not True:
        print("Craft text refiner weight will be downloaded to {}".format(weight_path))

        file_utils.download(url=url, save_path=weight_path)

    # arange device
    if cuda:
        refine_net.load_state_dict(copyStateDict(torch_utils.load(weight_path)))

        refine_net = refine_net.cuda()
        refine_net = torch_utils.DataParallel(refine_net)
        torch_utils.cudnn_benchmark = False
    else:
        refine_net.load_state_dict(
            copyStateDict(torch_utils.load(weight_path, map_location="cpu"))
        )
    refine_net.eval()
    return refine_net


def getDetBoxes_core(textmap, linkmap, text_threshold, link_threshold, low_text):
    '''
    Args:
        text_map: a 2d np float array containing the probability distribution of a
            pixel being a character region
        linkmap: a 2d np float array containing the probability distribution of a
            pixel being an inter character spacing 
        text_threshold: text confidence threshold
        link_threshold: link confidence threshold
        low_text: text low-bound score
    Output:
        det: a list of predicted boxes
        labels: a list of keys of each signle line word segmentation
        mapper: a map between the detected boxes and all possible labels in the heatmaps
        num_characters: estimate of number of characters in each single line text crop
        avg_character_sizes: average size of characters in the text crops
        word_id: next text crop adjacency vector, -1 when for a given crop index has no consecutive
            multiline text element, equal to the next line text crop index when it has one.
        
    '''
    def compare_boxes(box1, box2, slope_thresh = 0.1, min_edge_distance = 10):
        '''
        Given 2 4X2 numpy array containing [[x,y]..] points
        Returns True if they are next to each other else False
        Args:
            box1 (np.ndarray): a 4X2 nummy float32 array containing the 4 coordinates in 
                TL->TR->BR->BL sequence in (x,y) order, This box is assumed to be the one on top.
            box2 (np.ndarray): a 4X2 nummy float32 array containing the 4 coordinates in 
                TL->TR->BR->BL sequence in (x,y) order, This box is assumed to be the one at the bottom.
            slope_thresh (float): minimum difference between any 2 edges to be considered parallell
            min_edge_distance (float): minimum distance between the mid points of any 2 edges to be
                considered adjacent/overlapping/"intersecting"
            
        '''
        boxcentroid1 = np.mean(box1, axis=0)
        boxcentroid2 = np.mean(box2, axis=0)
        
        # check if box1 is above box2 spatially
        if boxcentroid1[1] < boxcentroid2[1]:
            for i, j in permutations(range(4), 2):
                for k,l in permutations(range(4), 2):
                    # compute slope and mid points of any 2 edges
                    #  and compare their differences 
                    slope1 = (box1[i, 1]-box1[j, 1])/(box2[i, 0]-box2[j, 0]\
                        if (box2[i, 0]-box2[j, 0]) != 0 else 0.001)
                    slope2 = (box1[k, 1]-box1[l, 1])/(box2[k, 0]-box2[l, 0]\
                        if (box2[k, 0]-box2[l, 0]) != 0 else 0.001)
                    
                    linecentroid1 = (box1[i,:] + box1[j,:])/2
                    linecentroid2 = (box2[j,:] + box2[k,:])/2
                        
                    if abs(slope1 - slope2) < slope_thresh and\
                        np.sum(abs((linecentroid1 - linecentroid2))) < min_edge_distance:
                        return True
        else:
            return False
    
    def d(cord1, cord2):
        return np.linalg.norm(cord1-cord2)

    def sort_box(box):
        '''
        Returns the bounding box sorted with BL->BR->TR->TL sequence
        given a 2d 4X2 list of vertices of a cyclic bbox
        Args (np.ndarray): a 4X2 np float32 array with the coordinated 
            of the text quadrilateral in cyclic order
        Returns:
            a 4X2 np float32 array with the coordinates sorted in
            TL->TR->BR->BL sequence
        '''
        aymin = np.argmin(box[:,1])
        tl=0 
        if d(box[aymin,:], box[(aymin+1)%4,:]) > d(box[(aymin+2)%4, :], box[(aymin+1)%4,:]):
            # aymin is top left
            tl = np.argmin(box[:,1])
        else:
            tl = (np.argmin(box[:,1]) -1)%4
            # axmin is top right
        box = box[tl:,:].tolist() + box[:tl,:].tolist()
        return np.array(box).astype(np.float32)

    # prepare data
    linkmap = linkmap.copy()
    textmap = textmap.copy()
    img_h, img_w = textmap.shape

    """ labeling method """
    ret, text_score = cv2.threshold(textmap, low_text, 1, 0)
    ret, link_score = cv2.threshold(linkmap, link_threshold, 1, 0)

    text_score_comb = np.clip(text_score + link_score, 0, 1)
    nLabels, labels, stats, centroids = cv2.connectedComponentsWithStats(
        text_score_comb.astype(np.uint8), connectivity=4)

    det = []
    mapper = []
    num_characters = []
    avg_character_sizes = []
    for k in range(1, nLabels):
        # size filtering
        size = stats[k, cv2.CC_STAT_AREA]
        if size < 10:
            continue

        # thresholding
        if np.max(textmap[labels == k]) < text_threshold:
            continue

        # make segmentation map
        segmap = np.zeros(textmap.shape, dtype=np.uint8)
        segmap[labels == k] = 255

        # remove link area
        segmap[np.logical_and(link_score == 1, text_score == 0)] = 0
        
        # count number of characters in word segementation
        word_chars, word_label, word_stats, word_centroid = cv2.connectedComponentsWithStats(
            segmap.astype(np.uint8), connectivity=4)
        num_characters.append(word_chars)

        avg_character_size = np.mean(word_stats[:, 4])
        avg_character_sizes.append(avg_character_size)
        
        x, y = stats[k, cv2.CC_STAT_LEFT], stats[k, cv2.CC_STAT_TOP]
        w, h = stats[k, cv2.CC_STAT_WIDTH], stats[k, cv2.CC_STAT_HEIGHT]
        niter = int(math.sqrt(size * min(w, h) / (w * h)) * 2)
        sx, ex, sy, ey = (x - niter, x + w + niter + 1, y - niter, y + h + niter + 1)
        # boundary check
        if sx < 0:
            sx = 0
        if sy < 0:
            sy = 0
        if ex >= img_w:
            ex = img_w
        if ey >= img_h:
            ey = img_h
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1 + niter, 1 + niter))
        segmap[sy:ey, sx:ex] = cv2.dilate(segmap[sy:ey, sx:ex], kernel)

        # make box
        np_temp = np.roll(np.array(np.where(segmap != 0)), 1, axis=0)
        np_contours = np_temp.transpose().reshape(-1, 2)
        rectangle = cv2.minAreaRect(np_contours)
        box = cv2.boxPoints(rectangle)

        # align diamond-shape
        w, h = np.linalg.norm(box[0] - box[1]), np.linalg.norm(box[1] - box[2])
        box_ratio = max(w, h) / (min(w, h) + 1e-5)
        if abs(1 - box_ratio) <= 0.1:
            l, r = min(np_contours[:, 0]), max(np_contours[:, 0])
            t, b = min(np_contours[:, 1]), max(np_contours[:, 1])
            box = np.array([[l, t], [r, t], [r, b], [l, b]], dtype=np.float32)

        # make clock-wise order
        startidx = box.sum(axis=1).argmin()
        box = np.roll(box, 4 - startidx, 0)
        box = np.array(box)

        det.append(sort_box(box))
        mapper.append(k)

    word_id = [-1]*len(det)
    for id1, (box1, size1) in enumerate(zip(det, avg_character_sizes)):
        for id2, (box2, size2) in enumerate(zip(det, avg_character_sizes)):
            if id1 == id2:
                pass
            else:
                if compare_boxes(box1, box2):
                    word_id[id1] = id2
    return det, labels, mapper, num_characters, avg_character_sizes, word_id


def getPoly_core(boxes, labels, mapper, linkmap):
    # configs
    num_cp = 5
    max_len_ratio = 0.7
    expand_ratio = 1.45
    max_r = 2.0
    step_r = 0.2

    polys = []
    for k, box in enumerate(boxes):
        # size filter for small instance
        w, h = (
            int(np.linalg.norm(box[0] - box[1]) + 1),
            int(np.linalg.norm(box[1] - box[2]) + 1),
        )
        if w < 10 or h < 10:
            polys.append(None)
            continue

        # warp image
        tar = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
        M = cv2.getPerspectiveTransform(box, tar)
        word_label = cv2.warpPerspective(labels, M, (w, h), flags=cv2.INTER_NEAREST)
        try:
            Minv = np.linalg.inv(M)
        except:
            polys.append(None)
            continue

        # binarization for selected label
        cur_label = mapper[k]
        word_label[word_label != cur_label] = 0
        word_label[word_label > 0] = 1

        """ Polygon generation """
        # find top/bottom contours
        cp = []
        max_len = -1
        for i in range(w):
            region = np.where(word_label[:, i] != 0)[0]
            if len(region) < 2:
                continue
            cp.append((i, region[0], region[-1]))
            length = region[-1] - region[0] + 1
            if length > max_len:
                max_len = length

        # pass if max_len is similar to h
        if h * max_len_ratio < max_len:
            polys.append(None)
            continue

        # get pivot points with fixed length
        tot_seg = num_cp * 2 + 1
        seg_w = w / tot_seg  # segment width
        pp = [None] * num_cp  # init pivot points
        cp_section = [[0, 0]] * tot_seg
        seg_height = [0] * num_cp
        seg_num = 0
        num_sec = 0
        prev_h = -1
        for i in range(0, len(cp)):
            (x, sy, ey) = cp[i]
            if (seg_num + 1) * seg_w <= x and seg_num <= tot_seg:
                # average previous segment
                if num_sec == 0:
                    break
                cp_section[seg_num] = [
                    cp_section[seg_num][0] / num_sec,
                    cp_section[seg_num][1] / num_sec,
                ]
                num_sec = 0

                # reset variables
                seg_num += 1
                prev_h = -1

            # accumulate center points
            cy = (sy + ey) * 0.5
            cur_h = ey - sy + 1
            cp_section[seg_num] = [
                cp_section[seg_num][0] + x,
                cp_section[seg_num][1] + cy,
            ]
            num_sec += 1

            if seg_num % 2 == 0:
                continue  # No polygon area

            if prev_h < cur_h:
                pp[int((seg_num - 1) / 2)] = (x, cy)
                seg_height[int((seg_num - 1) / 2)] = cur_h
                prev_h = cur_h

        # processing last segment
        if num_sec != 0:
            cp_section[-1] = [cp_section[-1][0] / num_sec, cp_section[-1][1] / num_sec]

        # pass if num of pivots is not sufficient or segment widh
        # is smaller than character height
        if None in pp or seg_w < np.max(seg_height) * 0.25:
            polys.append(None)
            continue

        # calc median maximum of pivot points
        half_char_h = np.median(seg_height) * expand_ratio / 2

        # calc gradiant and apply to make horizontal pivots
        new_pp = []
        for i, (x, cy) in enumerate(pp):
            dx = cp_section[i * 2 + 2][0] - cp_section[i * 2][0]
            dy = cp_section[i * 2 + 2][1] - cp_section[i * 2][1]
            if dx == 0:  # gradient if zero
                new_pp.append([x, cy - half_char_h, x, cy + half_char_h])
                continue
            rad = -math.atan2(dy, dx)
            c, s = half_char_h * math.cos(rad), half_char_h * math.sin(rad)
            new_pp.append([x - s, cy - c, x + s, cy + c])

        # get edge points to cover character heatmaps
        isSppFound, isEppFound = False, False
        grad_s = (pp[1][1] - pp[0][1]) / (pp[1][0] - pp[0][0]) + (
            pp[2][1] - pp[1][1]
        ) / (pp[2][0] - pp[1][0])
        grad_e = (pp[-2][1] - pp[-1][1]) / (pp[-2][0] - pp[-1][0]) + (
            pp[-3][1] - pp[-2][1]
        ) / (pp[-3][0] - pp[-2][0])
        for r in np.arange(0.5, max_r, step_r):
            dx = 2 * half_char_h * r
            if not isSppFound:
                line_img = np.zeros(word_label.shape, dtype=np.uint8)
                dy = grad_s * dx
                p = np.array(new_pp[0]) - np.array([dx, dy, dx, dy])
                cv2.line(
                    line_img,
                    (int(p[0]), int(p[1])),
                    (int(p[2]), int(p[3])),
                    1,
                    thickness=1,
                )
                if (
                    np.sum(np.logical_and(word_label, line_img)) == 0
                    or r + 2 * step_r >= max_r
                ):
                    spp = p
                    isSppFound = True
            if not isEppFound:
                line_img = np.zeros(word_label.shape, dtype=np.uint8)
                dy = grad_e * dx
                p = np.array(new_pp[-1]) + np.array([dx, dy, dx, dy])
                cv2.line(
                    line_img,
                    (int(p[0]), int(p[1])),
                    (int(p[2]), int(p[3])),
                    1,
                    thickness=1,
                )
                if (
                    np.sum(np.logical_and(word_label, line_img)) == 0
                    or r + 2 * step_r >= max_r
                ):
                    epp = p
                    isEppFound = True
            if isSppFound and isEppFound:
                break

        # pass if boundary of polygon is not found
        if not (isSppFound and isEppFound):
            polys.append(None)
            continue

        # make final polygon
        poly = []
        poly.append(warpCoord(Minv, (spp[0], spp[1])))
        for p in new_pp:
            poly.append(warpCoord(Minv, (p[0], p[1])))
        poly.append(warpCoord(Minv, (epp[0], epp[1])))
        poly.append(warpCoord(Minv, (epp[2], epp[3])))
        for p in reversed(new_pp):
            poly.append(warpCoord(Minv, (p[2], p[3])))
        poly.append(warpCoord(Minv, (spp[2], spp[3])))

        # add to final result
        polys.append(np.array(poly))

    return polys


def getDetBoxes(textmap, linkmap, text_threshold, link_threshold, low_text, poly=False):
    boxes, labels, mapper, num_characters, avg_character_size, word_id = getDetBoxes_core(
        textmap, linkmap, text_threshold, link_threshold, low_text
    )
    '''
    Returns the detection boxes and predictions of CRAFT as a dictionary
    Args:
        text_map: a 2d np float array containing the probability distribution of a
            pixel being a character region
        linkmap: a 2d np float array containing the probability distribution of a
            pixel being an inter character spacing 
        text_threshold: text confidence threshold
        link_threshold: link confidence threshold
        low_text: text low-bound score
        poly (bool): whether to return the polyline seg or not  
    '''
    if poly:
        polys = getPoly_core(boxes, labels, mapper, linkmap)
    else:
        polys = [None] * len(boxes)
    return_dict = {"boxes": boxes, "labels": labels, "mapper": mapper, "num_characters": num_characters,\
    "average_character_size": avg_character_size,"word_id": word_id, "polys": polys}
    return return_dict


def adjustResultCoordinates(polys, ratio_w, ratio_h, ratio_net=2):
    if len(polys) > 0:
        polys = np.array(polys)
        for k in range(len(polys)):
            if polys[k] is not None:
                polys[k] *= (ratio_w * ratio_net, ratio_h * ratio_net)
    return polys
