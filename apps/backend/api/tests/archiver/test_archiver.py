"""
Archiver tests: naming rules, combined near-dup hashing, and the end-to-end
copy+CULL flow inside a real (SQLite) Django database.
"""

import math
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from django.test import TestCase
from PIL import Image

from api.archiver import engine, naming
from api.archiver.hashes import hashes_close, hashes_of_image
from api.models import LongRunningJob, Photo
from api.tests.utils import create_test_user

ARCHIVER_ROOT = "api.archiver." + engine.__name__


def _scene(size=(160, 120)):
    """Smooth multi-frequency scene that stays hash-close across rescales."""
    width, height = size
    cx, cy = width / 2, height / 2
    pixels = []
    for y in range(height):
        for x in range(width):
            dx = (x - cx) / max(width, 1) * 2.0
            dy = (y - cy) / max(height, 1) * 2.0
            r = math.sqrt(dx * dx + dy * dy)
            red = int(128 + 120 * math.cos(r * 6.0))
            green = int(110 + 110 * math.cos(r * 5.0 + x / width * 3.0))
            blue = int(90 + 90 * math.cos(r * 4.0 + y / height * 3.0))
            pixels.append((red, green, blue))
    img = Image.new("RGB", size)
    img.putdata(pixels)
    return img


def _jpeg(path: str, image, quality: int = 92) -> str:
    image.save(path, "JPEG", quality=quality)
    return path


class NamingTests(TestCase):
    def test_prefix_applied_when_no_full_datetime_in_name(self):
        name = naming.build_archive_name(
            "IMG_0033.JPG",
            datetime(2024, 3, 5, 8, 9, 10),
            label="photo",
        )
        self.assertEqual(name, "2024-03-05_080910_photo_IMG_0033.JPG")

    def test_full_datetime_name_kept_unprefixed(self):
        name = naming.build_archive_name(
            "2023-11-02 14:05:06_IMG.JPG",
            datetime(2024, 3, 5, 8, 9, 10),
            label="photo",
        )
        self.assertFalse(name.startswith("2024-03-05_"), name)

    def test_sanitize_strips_dangerous_chars(self):
        self.assertEqual(naming.sanitize('a/b\\c:d*?"<>|'), "a_b_c_d______")

    def test_ensure_unique_appends_counter(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.jpg")
            Path(path).write_text("x")
            self.assertEqual(naming.ensure_unique(path), Path(tmp) / "x_1.jpg")


class CombinedHashingTests(TestCase):
    def test_identical_files_are_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = _jpeg(os.path.join(tmp, "a.jpg"), _scene())
            b = _jpeg(os.path.join(tmp, "b.jpg"), _scene())
            ph_a, dh_a, co_a, _, _ = hashes_of_image(a)
            ph_b, dh_b, co_b, _, _ = hashes_of_image(b)
            self.assertTrue(hashes_close(ph_a, ph_b, dh_a, dh_b, co_a, co_b))

    def test_different_scene_is_not_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = _jpeg(os.path.join(tmp, "a.jpg"), _scene())
            b = _jpeg(os.path.join(tmp, "b.jpg"), _scene((320, 240)))
            dark_b = Image.new("RGB", (320, 240), (5, 5, 5))
            c = _jpeg(os.path.join(tmp, "c.jpg"), dark_b)
            ph_a, dh_a, co_a, _, _ = hashes_of_image(a)
            ph_c, dh_c, co_c, _, _ = hashes_of_image(c)
            self.assertFalse(hashes_close(ph_a, ph_c, dh_a, dh_c, co_a, co_c))
            # An upscaled copy of the scene is still a near duplicate.
            ph_b, dh_b, co_b, _, _ = hashes_of_image(b)
            self.assertTrue(hashes_close(ph_a, ph_b, dh_a, dh_b, co_a, co_b))


class _EngineBase(TestCase):
    def setUp(self):
        self.user = create_test_user()
        self.tmpdir = tempfile.mkdtemp(prefix="lp-archiver-")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.user.scan_directory = os.path.join(self.tmpdir, "scan")
        os.makedirs(self.user.scan_directory)
        self.user.save()
        self.source = os.path.join(self.tmpdir, "incoming")
        os.makedirs(self.source)

    def run_archiver(self):
        job = LongRunningJob.create_job(
            user=self.user,
            job_type=LongRunningJob.JOB_ARCHIVE_DIRECTORY,
            start_now=True,
        )
        engine._Archiver(self.user, self.source, job).run()
        return job

    def fresh_source_file(self, name, image, quality):
        path = os.path.join(self.source, name)
        return _jpeg(path, image, quality=quality)


class FlowTests(_EngineBase):
    def test_new_file_added_and_named(self):
        self.fresh_source_file("DSC_0001.jpg", _scene(), 92)
        job = self.run_archiver()
        stats = job.result
        self.assertEqual(stats["added"], 1)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(Photo.objects.filter(owner=self.user).count(), 1)
        photo = Photo.objects.get(owner=self.user)
        self.assertIsNotNone(photo.dhash)
        self.assertIsNotNone(photo.color_hash)
        self.assertGreaterEqual(photo.main_file.path, self.user.scan_directory)
        self.assertIn("DSC_0001", Path(photo.main_file.path).name)

    def test_exact_copy_moved_to_duplicates(self):
        image = _scene()
        original = os.path.join(self.source, "IMG_0001.jpg")
        _jpeg(original, image)
        shutil.copyfile(original, os.path.join(self.source, "IMG_0001_copy.jpg"))
        job = self.run_archiver()
        stats = job.result
        self.assertEqual(stats["added"], 1)
        self.assertEqual(stats["exact_duplicates"], 1)
        self.assertEqual(Photo.objects.filter(owner=self.user).count(), 1)
        duplicates_dir = os.path.join(self.user.scan_directory, "duplicates")
        self.assertTrue(os.listdir(duplicates_dir))

    def test_better_copy_upgrades(self):
        original = os.path.join(self.source, "lo.jpg")
        _jpeg(original, _scene((80, 60)), quality=60)
        job = self.run_archiver()
        self.assertEqual(job.result["added"], 1)

        # A clearly bigger copy of the same scene arrives next run.
        hi = os.path.join(self.source, "hi.jpg")
        _jpeg(hi, _scene((320, 240)), quality=95)
        job = self.run_archiver()
        stats = job.result
        self.assertEqual(stats["upgrades"], 1)
        self.assertEqual(stats["added"], 0)
        self.assertEqual(Photo.objects.filter(owner=self.user).count(), 1)
        photo = Photo.objects.get(owner=self.user)
        self.assertIn("hi", Path(photo.main_file.path).name)
        upgraded_path = os.path.join(self.user.scan_directory, "duplicates")
        self.assertTrue(os.listdir(upgraded_path))

    def test_worse_copy_lands_in_duplicates(self):
        hi = os.path.join(self.source, "hi.jpg")
        _jpeg(hi, _scene((320, 240)), quality=95)
        job = self.run_archiver()
        self.assertEqual(job.result["added"], 1)

        lo = os.path.join(self.source, "lo.jpg")
        _jpeg(lo, _scene((80, 60)), quality=50)
        job = self.run_archiver()
        stats = job.result
        self.assertEqual(stats["similar_duplicates"], 1)
        self.assertEqual(Photo.objects.filter(owner=self.user).count(), 1)
        self.assertTrue(
            os.listdir(os.path.join(self.user.scan_directory, "duplicates"))
        )

    def test_missing_library_copy_is_restored(self):
        source_path = os.path.join(self.source, "orig.jpg")
        _jpeg(source_path, _scene())
        job = self.run_archiver()
        photo = Photo.objects.get(owner=self.user)
        main_path = photo.main_file.path
        os.remove(main_path)
        photo.main_file.missing = True
        photo.main_file.save()

        # Feed an identical byte copy from a fresh source directory.
        _jpeg(os.path.join(self.source, "backup.jpg"), _scene())
        job = self.run_archiver()
        stats = job.result
        self.assertEqual(stats["restored"], 1)
        photo.refresh_from_db()
        self.assertTrue(os.path.exists(photo.main_file.path))
        self.assertFalse(photo.main_file.missing)
