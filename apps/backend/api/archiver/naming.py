"""
Archive naming: ``YYYY-MM-DD_HHMMSS_<label>_<origin>``.

Port of the AH-Photo naming rules:

- an original name that already carries a full date+time anywhere in it is
  returned as-is (normalised) instead of prefixing it again;
- otherwise a ``YYYY-MM-DD_HHMMSS_<label>_<sanitised original>`` prefix is
  prepended;
- colliding targets in the destination folder get ``_1``, ``_2``... suffixes.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

_MAX_NAME_LEN = 160
_SAFE_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# Full labels: YYYY-MM-DD[ ... ]HH:MM[:SS] with varied separators.
_DELIM = r"[-:_.\s/]*"
_NB = r"(?<![\d])"
_NA = r"(?![\d])"
_FULL_DATETIME_RE = re.compile(
    _NB
    + _DELIM.join(
        [
            r"(?P<year>(?:19|20|21)\d\d)",
            r"(?P<month>(?:0[1-9]|1[0-2]))",
            r"(?P<day>(?:0[1-9]|[12]\d|3[01]))",
            r"(?P<hour>(?:[01]\d|2[0-3]))",
            r"(?P<minute>[0-5]\d)",
        ]
    )
    + _DELIM
    + r"(?P<sec>[0-5]\d)?"
)


def sanitize(name: str, max_len: int = _MAX_NAME_LEN) -> str:
    """Dangerous-characters stripped, length capped, never empty."""
    name = _SAFE_RE.sub("_", name).strip(" .")
    if len(name) > max_len:
        stem, ext = Path(name).stem, Path(name).suffix
        keep = max_len - len(ext)
        name = stem[: max(20, keep)] + ext
    return name or "untitled"


def name_has_full_datetime(name: str) -> bool:
    """True when ``name`` embeds a plausible full date+time label."""
    for match in _FULL_DATETIME_RE.finditer(name):
        try:
            datetime(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
                int(match.group("hour")),
                int(match.group("minute")),
                int(match.group("sec") or 0),
            )
            return True
        except ValueError:
            continue
    return False


def build_archive_name(
    original_name: str,
    captured: datetime,
    label: str = "photo",
    keep_existing: bool = True,
) -> str:
    """Archive name for a file.

    When the original file name already embeds a full date+time
    (``keep_existing``), the normalised original name is returned so the
    prefix is not duplicated. Otherwise ``YYYY-MM-DD_HHMMSS_<label>_<origin>``
    is built.
    """
    cleaned = sanitize(original_name)
    if keep_existing and name_has_full_datetime(original_name):
        return cleaned
    return f"{captured:%Y-%m-%d}_{captured:%H%M%S}_{label}_{cleaned}"


def ensure_unique(path: Path | str) -> Path:
    """Return ``path`` with a ``_1``, ``_2``... suffix when it already exists."""
    path = Path(path)
    if not path.exists():
        return path
    parent, name = path.parent, path.name
    stem, ext = Path(name).stem, Path(name).suffix
    index = 1
    while True:
        candidate = parent / f"{stem}_{index}{ext}"
        if not candidate.exists():
            return candidate
        index += 1
