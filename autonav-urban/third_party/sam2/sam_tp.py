# sam2_inference_service.py

import torch
import numpy as np
from PIL import Image
import matplotlib.cm as cm

from .build_sam import build_sam2
from .sam2_image_predictor import SAM2ImagePredictor

class SAM_TP:
    def __init__(self, sam2_cfg_path, sam2_checkpoint_path, score_thresh=0.0, multimask=False):
        """
        Loads SAM2 model into memory exactly once.
        """
        print("start")
        print(sam2_cfg_path)
        print(sam2_checkpoint_path)
        self.sam2_model = build_sam2(sam2_cfg_path, sam2_checkpoint_path)
        print("end")

        self.score_thresh = score_thresh
        self.multimask_output = multimask

    def run_sam2_inference(self, input_image_np: np.ndarray):
        """
        Runs SAM2 on a single RGB image (H,W,3).

        Returns:
            heatmap_array (H,W,3) RGB color-coded from the mask scores
            score_map (H,W) float array with raw or post-sigmoid scores
        """
        pil_image = Image.fromarray(input_image_np)
        predictor = SAM2ImagePredictor(
            sam_model=self.sam2_model,
            mask_threshold=self.score_thresh
        )
        predictor.reset_predictor()
        predictor.set_image(pil_image)

        width, height = pil_image.size
        bottom_left  = (0,        height - 1)
        bottom_right = (width-1,  height - 1)
        bottom_mid   = ((width-1)//2, height - 1)

        point_coords = np.array([bottom_left, bottom_right, bottom_mid], dtype=np.float32)
        point_labels = np.ones(len(point_coords), dtype=np.int32)  # 1=foreground

        masks, iou_predictions, low_res_logits = predictor.predict(
            point_coords=point_coords,
            point_labels=point_labels,
            multimask_output=self.multimask_output,
            return_logits=True,
            normalize_coords=False
        )
        # Choose best
        best_mask_idx = int(iou_predictions.argmax()) if self.multimask_output else 0
        best_mask_logit = masks[best_mask_idx]  # shape(H, W) float

        # This is the raw "score map" we can interpret as cost or traversability
        score_map = best_mask_logit

        # Create color heatmap from the same array
        mini = score_map.min()
        maxi = score_map.max()
        eps = 1e-8
        normalized = (score_map - mini) / (maxi - mini + eps)
        heatmap_array = (cm.get_cmap('jet')(normalized) * 255).astype(np.uint8)  # (H,W,4)
        heatmap_array = heatmap_array[..., :3]  # drop alpha => (H,W,3) in RGB

        return {
            "heatmap": heatmap_array,
            "logits": score_map
        }
