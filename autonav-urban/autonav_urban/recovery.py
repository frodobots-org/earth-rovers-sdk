"""VLM-based off-road detection + 360-degree look-around recovery.

Phase 10. Phase 1: signature stubs. Reuses the LLM provider abstraction
from the existing autonav_service.py once we port that forward.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class OffRoadVote:
    status: str          # "ON" | "OFF" | "UNKNOWN"
    confidence: float
    ts: float


@dataclass
class OffRoadClassifier:
    """FIFO buffer of recent VLM classifications with majority-vote trigger."""

    provider: str = "gemini"
    model: Optional[str] = None
    buffer_size: int = 5
    votes_needed: int = 2
    votes: deque = field(default_factory=lambda: deque(maxlen=5))

    async def check_frame(self, frame_b64: str) -> OffRoadVote:
        """Query the VLM on one front-camera frame. Phase 10 implementation."""
        raise NotImplementedError("Phase 10: wire up provider dispatch")

    def is_off_road(self) -> bool:
        """True if the last `votes_needed` votes in the last 3 samples are OFF."""
        recent = list(self.votes)[-3:]
        off_count = sum(1 for v in recent if v.status == "OFF")
        return off_count >= self.votes_needed


async def perform_look_around(headings_deg: list[float]) -> list[tuple[float, str]]:
    """Turn to each heading (using main.py::_perform_turn), snap a frame, return list.

    Phase 10 implementation.
    """
    raise NotImplementedError("Phase 10: reuse _perform_turn() from main.py")


async def pick_best_heading_via_vlm(
    candidates: list[tuple[float, str]],
    provider: str,
    model: Optional[str],
) -> float:
    """Ask the VLM which frame in `candidates` looks most like sidewalk.

    Phase 10 implementation.
    """
    raise NotImplementedError("Phase 10: VLM prompt + heading selection")
