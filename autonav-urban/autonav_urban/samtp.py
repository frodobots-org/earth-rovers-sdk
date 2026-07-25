"""SAM-TP model wrapper with device auto-detection.

The vendored `sam2/sam_tp.py` hardcodes CUDA via `build_sam2()`'s default.
This wrapper reuses the exact same underlying pipeline (build_sam2 + SAM2
image predictor) but explicitly picks MPS on Apple Silicon, CUDA on Linux,
and CPU as a last resort. Inference API matches the vendored SAM_TP so
callers can swap implementations.

We do NOT modify the vendored SAM2 code; this file is our escape hatch.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import torch
from PIL import Image


def refine_traversability_by_contrast(
    trav: np.ndarray,
    rgb: np.ndarray,
    drivable_thresh: float = 0.5,
    darkness_ratio: float = 0.65,
    min_reference_pixels: int = 500,
) -> np.ndarray:
    """Mark pixels-much-darker-than-the-ground as obstacles, even if SAM-TP said drivable.

    Why this exists: the paper's SAM-TP checkpoint was fine-tuned on
    campus imagery and does not generalize to Mini+ scenes with unusual
    obstacles (other rovers, chairs, low objects). It systematically
    labels dark objects sitting on light ground as "drivable" because it
    has no concept of "thing on the ground" — only "ground vs. above-ground".

    Fix: after SAM-TP predicts, use the pixels IT called drivable as a
    per-frame reference for "what the ground looks like right now" (its
    median luminance). Any pixel it called drivable that is dramatically
    darker than that reference is very likely an obstacle sitting on the
    ground → downgrade to trav=0.

    This is adaptive to lighting (works in shade, bright sun, overcast)
    because the reference is recomputed every frame. It does nothing when
    SAM-TP already flagged the pixel red.

    Args:
        trav: HxW float32 traversability in [0, 1] (SAM-TP output post-sigmoid).
        rgb:  HxWx3 uint8 front-camera frame.
        drivable_thresh: pixels with trav > this count as "reference ground".
        darkness_ratio: pixels with luminance < median_reference * this are
            downgraded to obstacle. 0.65 = 35% darker than the median.
        min_reference_pixels: if SAM-TP labeled fewer than this as drivable,
            skip refinement (not enough signal to compute a reference).

    Returns:
        HxW float32 traversability, same shape/dtype as input, with dark
        outliers set to 0.0.
    """
    t = np.asarray(trav, dtype=np.float32)
    if rgb is None or rgb.shape[:2] != t.shape:
        return t

    # ITU-R BT.601 grayscale (perceptual luminance).
    r = rgb[..., 0].astype(np.float32)
    g = rgb[..., 1].astype(np.float32)
    b = rgb[..., 2].astype(np.float32)
    lum = 0.299 * r + 0.587 * g + 0.114 * b     # [0, 255]

    ground_mask = t > float(drivable_thresh)
    if int(ground_mask.sum()) < int(min_reference_pixels):
        # Not enough drivable pixels to trust a reference — skip refinement.
        return t

    median_ground_lum = float(np.median(lum[ground_mask]))
    if median_ground_lum < 30.0:
        # Scene is very dark overall (dusk, indoor). "Darker than ground"
        # doesn't tell us much when everything is dark — skip.
        return t

    dark_thresh = median_ground_lum * float(darkness_ratio)
    obstacle_mask = ground_mask & (lum < dark_thresh)
    if not obstacle_mask.any():
        return t

    out = t.copy()
    out[obstacle_mask] = 0.0
    return out


def pick_device() -> str:
    """Return the best-available torch device string on this host."""
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


class SAMTPModel:
    """Drop-in replacement for the vendored `SAM_TP` class with device kwarg.

    Usage:
        model = SAMTPModel(cfg_path, ckpt_path, device="mps")
        out = model.run_sam2_inference(rgb_np)   # -> {"logits": HxW, "heatmap": HxWx3}
    """

    def __init__(
        self,
        cfg_path: str,
        ckpt_path: str,
        device: Optional[str] = None,
        score_thresh: float = 0.0,
        multimask: bool = False,
    ) -> None:
        # Deferred imports so autonav_urban/__init__.py's sys.path hook
        # can put the vendored sam2 package on the import path first.
        from sam2.build_sam import build_sam2
        from sam2.sam2_image_predictor import SAM2ImagePredictor

        self._device = device or pick_device()
        self._model = build_sam2(cfg_path, ckpt_path, device=self._device)
        self._score_thresh = float(score_thresh)
        self._multimask = bool(multimask)
        self._Predictor = SAM2ImagePredictor

    @property
    def device(self) -> str:
        return self._device

    def run_sam2_inference(self, rgb_np: np.ndarray) -> dict:
        """Predict per-pixel traversability logits from an RGB image.

        Returns {"logits": HxW float32, "heatmap": HxWx3 uint8 RGB}.
        Same contract as the vendored SAM_TP.run_sam2_inference().
        """
        pil = Image.fromarray(rgb_np)
        predictor = self._Predictor(sam_model=self._model, mask_threshold=self._score_thresh)
        predictor.reset_predictor()
        predictor.set_image(pil)

        w, h = pil.size
        # Three bottom-edge points matching the vendored SAM_TP prompt strategy:
        # left-bottom, right-bottom, center-bottom.
        pts = np.array(
            [(0, h - 1), (w - 1, h - 1), ((w - 1) // 2, h - 1)],
            dtype=np.float32,
        )
        labels = np.ones(len(pts), dtype=np.int32)

        masks, iou_predictions, _low_res = predictor.predict(
            point_coords=pts,
            point_labels=labels,
            multimask_output=self._multimask,
            return_logits=True,
            normalize_coords=False,
        )

        best_idx = int(iou_predictions.argmax()) if self._multimask else 0
        logits = np.asarray(masks[best_idx], dtype=np.float32)

        # Simple red↔blue heatmap so we don't need matplotlib here.
        finite = np.isfinite(logits)
        if not np.any(finite):
            heatmap = np.zeros(logits.shape + (3,), dtype=np.uint8)
        else:
            lo = float(logits[finite].min())
            hi = float(logits[finite].max())
            norm = (logits - lo) / max(1e-8, hi - lo)
            norm = np.clip(norm, 0.0, 1.0)
            heatmap = np.stack(
                [
                    (norm * 255).astype(np.uint8),                    # R -> high traversability
                    np.zeros_like(norm, dtype=np.uint8),
                    ((1.0 - norm) * 255).astype(np.uint8),            # B -> low traversability
                ],
                axis=-1,
            )

        return {"logits": logits, "heatmap": heatmap}
