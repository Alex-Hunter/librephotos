"""
Combined perceptual hashing used by the archiver.

A photo counts as a near duplicate only when ALL THREE hashes are close at
once, mirroring the AH-Photo engine:

- ``phash`` (imagehash pHash, DCT based) is robust to re-encoding and resizing;
- ``dhash`` (difference hash) rejects unrelated scenes whose pHash is close;
- the color hash (mean RGB per 4x4 block) is the main filter against
  visually different images that happen to agree on both DCT-ish hashes.

This is deliberately stricter than a single pHash hamming distance, which
flags far more false positives on large libraries.
"""

from __future__ import annotations

import imagehash
from PIL import Image

PHASH_THRESHOLD = 14  # hamming for pHash
DHASH_THRESHOLD = 14  # hamming for dHash
COLOR_HASH_THRESHOLD = 40.0  # mean channel delta (0..255)
COLOR_GRID = 4


def _open_rgb(img: Image.Image) -> Image.Image:
    if img.mode not in ("RGB", "L", "RGBA"):
        img = img.convert("RGB")
    return img


def phash_of(img: Image.Image, hash_size: int = 8) -> str:
    """64-bit pHash of an already opened image."""
    return str(imagehash.phash(_open_rgb(img), hash_size=hash_size))


def dhash_of(img: Image.Image, hash_size: int = 8) -> str:
    """64-bit difference hash of an already opened image."""
    return str(imagehash.dhash(_open_rgb(img), hash_size=hash_size))


def color_hash_of(img: Image.Image, grid: int = COLOR_GRID) -> str:
    """Mean RGB of every ``grid`` x ``grid`` block as hex."""
    img = _open_rgb(img).resize((grid, grid), Image.Resampling.LANCZOS)
    pixels = img.load()
    values = bytes(
        pixels[x, y][i] for y in range(grid) for x in range(grid) for i in range(3)
    )
    return values.hex()


def phash_image(path: str, hash_size: int = 8) -> str | None:
    """64-bit pHash of ``path`` as a hex string, or None when unreadable."""
    try:
        with Image.open(path) as img:
            return phash_of(img, hash_size=hash_size)
    except Exception:
        return None


def dhash_image(path: str, hash_size: int = 8) -> str | None:
    """64-bit difference hash of ``path``, or None when unreadable."""
    try:
        with Image.open(path) as img:
            return dhash_of(img, hash_size=hash_size)
    except Exception:
        return None


def color_hash_image(path: str, grid: int = COLOR_GRID) -> str | None:
    """Mean RGB of every ``grid`` x ``grid`` block of ``path``, as hex."""
    try:
        with Image.open(path) as img:
            return color_hash_of(img, grid=grid)
    except Exception:
        return None


def hashes_of_image(path: str) -> tuple:
    """All three hashes plus size of ``path`` in a single disk read.

    Returns (phash, dhash, color_hash, width, height). Hash components are
    None when the file cannot be decoded; dimensions default to 0.
    """
    try:
        with Image.open(path) as img:
            img.load()
            width, height = img.size
            return (
                phash_of(img),
                dhash_of(img),
                color_hash_of(img),
                width,
                height,
            )
    except Exception:
        return (None, None, None, 0, 0)


def hamming_distance(a: str, b: str) -> int:
    """Number of differing bits between two hex-encoded hashes."""
    return (int(a, 16) ^ int(b, 16)).bit_count()


def color_distance(a: str, b: str, grid: int = COLOR_GRID) -> float:
    """Mean absolute channel delta across all blocks (0..255)."""
    av = bytes.fromhex(a)
    bv = bytes.fromhex(b)
    n = max(1, grid * grid * 3)
    return sum(abs(x - y) for x, y in zip(av, bv)) / n


def hashes_close(
    phash_a: str | None,
    phash_b: str | None,
    dhash_a: str | None,
    dhash_b: str | None,
    color_a: str | None,
    color_b: str | None,
    phash_threshold: int = PHASH_THRESHOLD,
    dhash_threshold: int = DHASH_THRESHOLD,
    color_threshold: float = COLOR_HASH_THRESHOLD,
) -> bool:
    """
    Combined near-duplicate test.

    Every pairwise check that both sides can provide must fall within its
    threshold; a missing value on either side is treated as a pass so the
    combined test still degrades gracefully on partially hashed libraries.
    """
    close_phash = (
        phash_a is None
        or phash_b is None
        or hamming_distance(phash_a, phash_b) <= phash_threshold
    )
    close_dhash = (
        dhash_a is None
        or dhash_b is None
        or hamming_distance(dhash_a, dhash_b) <= dhash_threshold
    )
    close_color = (
        color_a is None
        or color_b is None
        or color_distance(color_a, color_b) <= color_threshold
    )
    return close_phash and close_dhash and close_color
