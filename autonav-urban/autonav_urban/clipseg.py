"""CLIPSeg wrapper: text-prompted per-pixel obstacle detector.

Runs alongside SAM-TP in the perception loop. SAM-TP has no concept of
"what things are" — it just learned "ground vs. above-ground" on the
paper's dataset. CLIPSeg on the other hand takes text prompts like
"a robot" or "grass" and produces per-pixel masks of exactly those
things, zero-shot. Fusing the two gives us:

  drivable_final = drivable_samtp × (1 − α × obstacle_clipseg)

so any pixel CLIPSeg calls an obstacle with high confidence kills the
traversability there even when SAM-TP said "drivable."

Uses HuggingFace `CIDAS/clipseg-rd64-refined` (~180 MB one-time download,
cached to ~/.cache/huggingface). MPS-aware — falls through to CUDA/CPU
if MPS isn't available.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import torch
from PIL import Image

logger = logging.getLogger("autonav_urban.clipseg")


def pick_device() -> str:
    """Return the best-available torch device string on this host."""
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


class CLIPSegModel:
    """Text-prompted image segmenter.

    Usage:
        model = CLIPSegModel(prompts=["a robot", "grass", "a person"])
        obstacle_mask = model.predict(rgb_np)   # HxW float32 in [0, 1]
                                                # max across all prompts

    Design:
    - Loaded once at server boot (via warmup); one dummy inference to compile
      MPS kernels; then reused for every perception tick.
    - `predict()` accepts an RGB uint8 array, resizes to 352×352 (CLIPSeg's
      native input), runs a single batched forward pass over all prompts,
      applies sigmoid, takes the pointwise MAX across prompts (so any prompt
      firing = obstacle), then resizes the mask back to input HxW.
    """

    _MODEL_ID = "CIDAS/clipseg-rd64-refined"

    def __init__(
        self,
        prompts: list[str],
        device: Optional[str] = None,
        confidence_thresh: float = 0.3,
    ) -> None:
        if not prompts:
            raise ValueError("CLIPSegModel needs at least one prompt")
        # Deferred import so this file is safe to import even when
        # transformers isn't installed (caller can catch ImportError).
        from transformers import CLIPSegProcessor, CLIPSegForImageSegmentation

        self._device = device or pick_device()
        self._prompts = list(prompts)
        self._confidence_thresh = float(confidence_thresh)

        logger.info("Loading CLIPSeg from %s on %s ...", self._MODEL_ID, self._device)
        self._processor = CLIPSegProcessor.from_pretrained(self._MODEL_ID)
        model = CLIPSegForImageSegmentation.from_pretrained(self._MODEL_ID)
        model.eval()
        # `to(mps)` will silently place ops that don't support MPS on CPU
        # because our __init__.py sets PYTORCH_ENABLE_MPS_FALLBACK=1.
        self._model = model.to(self._device)

    @property
    def device(self) -> str:
        return self._device

    @property
    def prompts(self) -> list[str]:
        return list(self._prompts)

    @torch.no_grad()
    def predict(self, rgb_np: np.ndarray) -> np.ndarray:
        """Return the per-pixel obstacle probability mask [0, 1].

        The mask is the pointwise MAX of individual per-prompt sigmoids —
        any prompt firing counts as an obstacle at that pixel. Values
        below `confidence_thresh` are zeroed to avoid noisy low-confidence
        activations creeping into the fusion.
        """
        if rgb_np is None or rgb_np.size == 0:
            return np.zeros((0, 0), dtype=np.float32)
        h_in, w_in = int(rgb_np.shape[0]), int(rgb_np.shape[1])
        pil = Image.fromarray(rgb_np)

        # CLIPSeg processor pattern: N prompts + N copies of the image.
        n = len(self._prompts)
        inputs = self._processor(
            text=self._prompts,
            images=[pil] * n,
            return_tensors="pt",
            padding=True,
        )
        inputs = {k: v.to(self._device) for k, v in inputs.items()}
        outputs = self._model(**inputs)
        # outputs.logits has shape (N, H_out, W_out). H_out=W_out=352 typically.
        logits = outputs.logits
        if logits.dim() == 3:
            logits = logits.unsqueeze(1)      # (N, 1, H, W)
        probs = torch.sigmoid(logits).squeeze(1)   # (N, H, W)
        # Pointwise MAX across prompts — any prompt saying "obstacle here" wins.
        mask = probs.max(dim=0).values             # (H, W)

        # Threshold low-confidence noise floor.
        if self._confidence_thresh > 0.0:
            mask = torch.where(mask >= self._confidence_thresh, mask, torch.zeros_like(mask))

        # Resize back to the caller's input HxW using bilinear interpolation
        # (bicubic is not on MPS; bilinear is). Do the resize on CPU to avoid
        # MPS bilinear-4D edge cases and because it's cheap.
        mask = mask.detach().to("cpu").unsqueeze(0).unsqueeze(0)  # (1, 1, H, W)
        mask = torch.nn.functional.interpolate(
            mask, size=(h_in, w_in), mode="bilinear", align_corners=False,
        ).squeeze()
        return mask.numpy().astype(np.float32)

    def heatmap(self, mask: np.ndarray) -> np.ndarray:
        """Return an RGB uint8 heatmap of the obstacle mask.

        Blue → not-obstacle (low prob), red → obstacle (high prob). Same
        color coding as SAM-TP raw so the dashboard reads consistently.
        """
        m = np.clip(np.asarray(mask, dtype=np.float32), 0.0, 1.0)
        r = (m * 255).astype(np.uint8)
        g = np.zeros_like(r)
        b = ((1.0 - m) * 255).astype(np.uint8)
        return np.stack([r, g, b], axis=-1)
