"""Archiver: copy+CLEANUP of image directories.

Scans a user-supplied source directory and brings the files into the library
(``user.scan_directory``) reusing the AH-Photo approach:

- files are renamed ``YYYY-MM-DD_HHMMSS_<label>_<origin>`` and grouped under
  ``<scan>/<YYYY>/<MM>/``;
- an exact copy (same md5) is moved to ``<scan>/duplicates/...``;
- a near duplicate (pHash + dHash + color hash, all three close) keeps the
  better copy in the library and moves the worse one to ``duplicates/``.
"""

from api.archiver.engine import ArchiveCounters, run_archiver

__all__ = ["ArchiveCounters", "run_archiver"]
