"""
Archiver engine: scan a source directory and bring its images into the
LibrePhotos library reusing the AH-Photo approach.

For every image found in ``source_dir``:

1. unknown content -> moved into ``<scan>/<YYYY>/<MM>/`` under an archive name
   (``YYYY-MM-DD_HHMMSS_<label>_<origin>``) and indexed like a normal import;
2. exact duplicate (same md5) whose library copy is still on disk -> the
   incoming copy is moved to ``<scan>/duplicates/...`` and nothing is
   re-created. If the library copy is missing, the incoming copy RESTORES
   that photo instead;
3. near duplicate (pHash + dHash + color hash, all three within threshold) ->
   the worse copy lands in ``duplicates/``; when the incoming copy is clearly
   better, the old library copy is demoted and the incoming one takes its
   place (an upgrade).

Every byte is kept: the library holds the single best copy and the rest are
grouped under ``<scan>/duplicates/<YYYY>/<MM>/`` with deterministic names, so
nothing is ever deleted by the archiver.
"""

from __future__ import annotations

import datetime as datetime_mod
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from django import db
from django.db import transaction
from PIL import Image

from api import util
from api.archiver.hashes import hashes_close, hashes_of_image
from api.archiver.naming import build_archive_name, ensure_unique
from api.archiver.quality import MediaInfo, quality_score
from api.directory_watcher.file_handlers import handle_new_image
from api.models import File, LongRunningJob, Photo, Thumbnail, User
from api.models.file import calculate_hash

logger = util.logger

_VERSION = 1

_IMAGE_SUFFIXES = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
    ".heic",
    ".heif",
}


@dataclass
class ArchiveCounters:
    """Outcome counters; also serialised into the LongRunningJob result."""

    added: int = 0
    restored: int = 0
    exact_duplicates: int = 0
    similar_duplicates: int = 0
    upgrades: int = 0
    skipped: int = 0
    errors: int = 0
    reclaimed_bytes: int = 0
    demoted: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "added": self.added,
            "restored": self.restored,
            "exact_duplicates": self.exact_duplicates,
            "similar_duplicates": self.similar_duplicates,
            "upgrades": self.upgrades,
            "skipped": self.skipped,
            "errors": self.errors,
            "reclaimed_bytes": self.reclaimed_bytes,
            "demoted": self.demoted,
            "version": _VERSION,
        }


@dataclass
class _Candidate:
    """A library photo eligible for near-duplicate matching."""

    photo_id: str
    image_hash: str
    main_path: str
    phash: str | None
    dhash: str | None
    color_hash: str | None
    size: int
    width: int
    height: int


def _is_image(path: str) -> bool:
    return Path(path).suffix.lower() in _IMAGE_SUFFIXES


def _not_hidden(path: str) -> bool:
    return not os.path.basename(path).startswith(".")


def _walk_images(source_dir: str) -> list[str]:
    """Deterministic, recursive list of image files below ``source_dir``."""
    found: list[str] = []
    for root, dirs, files in os.walk(source_dir):
        dirs.sort()
        for name in sorted(files):
            if not _not_hidden(name) or not _is_image(name):
                continue
            found.append(os.path.join(root, name))
    return found


def _exif_captured(path: str) -> datetime_mod.datetime | None:
    try:
        from PIL import Image

        with Image.open(path) as img:
            exif = img.getexif()
        for tag in (
            36867,
            36868,
            306,
        ):  # DateTimeOriginal / DateTimeDigitized / DateTime
            value = exif.get(tag)
            if value:
                return datetime_mod.datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass
    return None


def _resolve_captured(path: str) -> datetime_mod.datetime:
    captured = _exif_captured(path)
    if captured is not None:
        return captured
    return datetime_mod.datetime.fromtimestamp(os.path.getmtime(path))


class _Archiver:
    """Stateful worker for one ``run_archiver`` invocation."""

    def __init__(self, user: User, source_dir: str, job: LongRunningJob):
        self.user = user
        self.source_dir = source_dir
        self.job = job
        self.scan_dir = user.scan_directory
        self.counters = ArchiveCounters()
        self.candidates: dict[str, _Candidate] = {}

    # ------------------------------------------------------------------ dirs
    def _library_dir(self, captured) -> str:
        return os.path.join(self.scan_dir, str(captured.year), f"{captured.month:02d}")

    def _duplicates_dir(self, captured) -> str:
        return os.path.join(
            self.scan_dir, "duplicates", str(captured.year), f"{captured.month:02d}"
        )

    def _move_to_duplicates(self, source: str, captured, label: str = "photo") -> None:
        target_dir = self._duplicates_dir(captured)
        os.makedirs(target_dir, exist_ok=True)
        target = ensure_unique(
            Path(target_dir)
            / build_archive_name(os.path.basename(source), captured, label)
        )
        self.counters.reclaimed_bytes += os.path.getsize(source)
        shutil.move(source, str(target))

    # ------------------------------------------------------------ candidates
    def _load_candidates(self) -> None:
        """Cache hash-carrying library photos, backfilling dHash/color lazily."""
        rows = (
            Photo.objects.select_related("main_file")
            .filter(
                owner=self.user,
                removed=False,
                in_trashcan=False,
                video=False,
            )
            .exclude(perceptual_hash=None)
        )
        updates: dict[str, tuple] = {}
        for photo in rows:
            main_file = photo.main_file
            if main_file is None or not os.path.exists(main_file.path):
                continue
            cand = _Candidate(
                photo_id=str(photo.id),
                image_hash=photo.image_hash,
                main_path=main_file.path,
                phash=photo.perceptual_hash,
                dhash=photo.dhash,
                color_hash=photo.color_hash,
                size=os.path.getsize(main_file.path),
                width=0,
                height=0,
            )
            if not cand.dhash or not cand.color_hash:
                phash, dhash, color_hash, width, height = hashes_of_image(
                    main_file.path
                )
                cand.dhash = dhash or None
                cand.color_hash = color_hash or None
                cand.width = width
                cand.height = height
                updates[photo.id] = (cand.dhash, cand.color_hash)
            if not cand.width or not cand.height:
                try:
                    with Image.open(main_file.path) as img:
                        cand.width, cand.height = img.size
                except Exception:
                    pass
            self.candidates[cand.image_hash] = cand
        for photo_id, (dhash, color_hash) in updates.items():
            Photo.objects.filter(pk=photo_id).update(dhash=dhash, color_hash=color_hash)

    def _add_candidate(
        self, photo: Photo, main_path: str, phash, dhash, color_hash, w, h
    ) -> None:
        self.candidates[photo.image_hash] = _Candidate(
            photo_id=str(photo.id),
            image_hash=photo.image_hash,
            main_path=main_path,
            phash=phash,
            dhash=dhash,
            color_hash=color_hash,
            size=os.path.getsize(main_path),
            width=w,
            height=h,
        )

    # -------------------------------------------------------------- ingest
    def _ingest_new(
        self, source: str, captured, phash, dhash, color_hash, w, h
    ) -> _Candidate | None:
        """Move ``source`` into the library and index it like a normal import."""
        target_dir = self._library_dir(captured)
        os.makedirs(target_dir, exist_ok=True)
        target = ensure_unique(
            Path(target_dir)
            / build_archive_name(os.path.basename(source), captured, "photo")
        )
        shutil.move(source, str(target))

        handle_new_image(self.user, str(target), str(self.job.job_id))
        md5 = calculate_hash(self.user, str(target))
        photo = (
            Photo.objects.filter(owner=self.user, image_hash=md5)
            .order_by("-added_on")
            .first()
        )
        if photo is None:
            return None
        photo.perceptual_hash = phash or photo.perceptual_hash
        photo.dhash = dhash
        photo.color_hash = color_hash
        photo.save(update_fields=["perceptual_hash", "dhash", "color_hash"])
        self._add_candidate(photo, str(target), phash, dhash, color_hash, w, h)
        return self.candidates[photo.image_hash]

    def _demote(self, best: _Candidate, captured) -> None:
        """Move the demoted library copy out and drop its DB rows."""
        with transaction.atomic():
            self._move_to_duplicates(best.main_path, captured)
            photo = Photo.objects.filter(pk=best.photo_id).first()
            main_file = photo.main_file if photo else None
            if photo is not None:
                photo.delete()
            if main_file is not None:
                main_file.delete()
            self.candidates.pop(best.image_hash, None)
            self.counters.demoted.append(
                {
                    "from": best.main_path,
                    "image_hash": best.image_hash,
                    "reclaimed": best.size,
                }
            )

    # ----------------------------------------------------------- decisions
    def _process_file(self, path: str) -> None:
        captured = _resolve_captured(path)
        md5 = calculate_hash(self.user, path)
        name = os.path.basename(path)

        # --- exact duplicate
        existing = (
            Photo.objects.select_related("main_file")
            .filter(owner=self.user, image_hash=md5, in_trashcan=False)
            .exclude(removed=True)
            .first()
        )
        if existing is not None and md5 not in self.candidates:
            main_file = existing.main_file
            missing = (
                main_file is None
                or getattr(main_file, "missing", False)
                or not os.path.isfile(main_file.path)
            )
            if missing:
                self.counters.restored += 1
                self._restore(existing, path, captured)
                return
        if existing is not None:
            self.counters.exact_duplicates += 1
            self._move_to_duplicates(path, captured)
            logger.info("archive: exact duplicate %s -> duplicates/", name)
            return

        # --- near duplicate (combined pHash + dHash + color)
        phash, dhash, color_hash, width, height = hashes_of_image(path)
        if phash is None and dhash is None:
            self.counters.skipped += 1
            logger.info("archive: undecodable %s, moved to library anyway", name)
            self._ingest_new(path, captured, phash, dhash, color_hash, width, height)
            return

        matches = [
            c
            for c in self.candidates.values()
            if hashes_close(phash, c.phash, dhash, c.dhash, color_hash, c.color_hash)
        ]
        if not matches:
            self.counters.added += 1
            logger.info("archive: new %s -> library", name)
            self._ingest_new(path, captured, phash, dhash, color_hash, width, height)
            return

        best = max(matches, key=lambda c: quality_score(self._info(c)))
        incoming_info = MediaInfo(path, os.path.getsize(path), width, height)
        my_score = quality_score(incoming_info)
        best_score = quality_score(self._info(best))

        if my_score > best_score:
            self.counters.upgrades += 1
            logger.info(
                "archive: upgrade %s (%s) over library copy %s (%s)",
                name,
                my_score,
                best.main_path,
                best_score,
            )
            self._demote(best, captured)
            self._ingest_new(path, captured, phash, dhash, color_hash, width, height)
            self.counters.reclaimed_bytes -= best.size
            return

        self.counters.similar_duplicates += 1
        self._move_to_duplicates(path, captured)
        logger.info(
            "archive: near duplicate %s (%s vs %s) -> duplicates/",
            name,
            my_score,
            best_score,
        )

    @staticmethod
    def _info(cand: _Candidate) -> MediaInfo:
        return MediaInfo(cand.main_path, cand.size, cand.width, cand.height)

    def _restore(self, photo: Photo, source: str, captured) -> None:
        """Return a missing library copy: re-target its File to ``source``."""
        target_dir = self._library_dir(captured)
        os.makedirs(target_dir, exist_ok=True)
        target = ensure_unique(
            Path(target_dir)
            / build_archive_name(os.path.basename(source), captured, "photo")
        )
        shutil.move(source, str(target))
        main_file = photo.main_file
        if main_file is None:
            main_file = File.create(str(target), self.user)
            photo.main_file = main_file
        else:
            main_file.path = str(target)
            main_file.missing = False
            main_file.save()
        Thumbnail.objects.filter(photo=photo).delete()
        handle_new_image(self.user, str(target), str(self.job.job_id), photo=photo)
        photo.refresh_from_db()
        if not photo.dhash:
            phash, dhash, color_hash, width, height = hashes_of_image(str(target))
            photo.dhash = dhash
            photo.color_hash = color_hash
            photo.save(update_fields=["dhash", "color_hash"])
        photo.refresh_from_db()
        self._add_candidate(
            photo,
            str(target),
            photo.perceptual_hash,
            photo.dhash,
            photo.color_hash,
            0,
            0,
        )

    def run(self) -> None:
        guard = os.path.join(self.scan_dir, "duplicates") + os.sep
        files = [f for f in _walk_images(self.source_dir) if not f.startswith(guard)]
        self._load_candidates()
        logger.info(
            "archive: started for user %s: %d files in %s",
            self.user.username,
            len(files),
            self.source_dir,
        )
        for index, path in enumerate(files):
            try:
                self._process_file(path)
            except Exception:
                self.counters.errors += 1
                logger.exception("archive: failed on %s", path)
            self.job.update_progress(index + 1, len(files), os.path.basename(path))
            if index % 25 == 0:
                self.job.set_result(self.counters.as_dict())
        self.job.complete(self.counters.as_dict())


def run_archiver(
    user_id: int,
    source_dir: str,
    options: dict | None = None,
    job_id: str | None = None,
) -> None:
    """
    Background entrypoint (django-q): process every image under
    ``source_dir`` for the given user. ``job_id``, when supplied, is reused as
    the LongRunningJob id so the API response and the DB job stay in sync.
    """
    del options
    db.close_old_connections()
    user = User.objects.get(pk=user_id)
    job = LongRunningJob.create_job(
        user=user,
        job_type=LongRunningJob.JOB_ARCHIVE_DIRECTORY,
        start_now=True,
        job_id=job_id,
    )
    archiver = _Archiver(user=user, source_dir=source_dir, job=job)
    try:
        archiver.run()
    except Exception:
        job.fail()
        logger.exception("archive: job %s failed", job.job_id)
    finally:
        db.close_old_connections()
