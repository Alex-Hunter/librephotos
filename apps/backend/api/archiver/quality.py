"""
Quality-oriented scoring for choosing between near-duplicate copies.

Port of the AH-Photo scoring: pixel count (resolution) is the dominant
factor; for images the file size adds a smaller log term so a better-packed
copy wins among equal resolutions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

_SOURCE_SIZE_W = 0.1 / math.log2(1000.0)  # ~0.1 log2(size/1000)


@dataclass(frozen=True)
class MediaInfo:
    """The few fields the score needs, decoupled from the Photo model."""

    path: str
    size: int = 0
    width: int = 0
    height: int = 0


def quality_score(info: MediaInfo) -> float:
    """Higher is better: log2 of the pixel count plus a small size term."""
    pixels = max(1, (info.width or 0) * (info.height or 0))
    score = math.log2(pixels)
    if (info.size or 0) > 0:
        score += _SOURCE_SIZE_W * math.log2(max(info.size / 1000.0, 1.0))
    return round(score, 4)


def better(a: MediaInfo, b: MediaInfo) -> int:
    """>0 when ``a`` is better than ``b``, <0 when worse, 0 when equal."""
    score_a, score_b = quality_score(a), quality_score(b)
    return (score_a > score_b) - (score_a < score_b)
