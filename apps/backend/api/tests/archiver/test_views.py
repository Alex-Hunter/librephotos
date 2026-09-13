"""Archiver API view tests: queuing and status plumbing."""

import os
import tempfile
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from api.archiver.views import ArchiverRunView
from api.models import LongRunningJob
from api.tests.utils import create_test_user


class ArchiverViewTests(TestCase):
    def setUp(self):
        self.user = create_test_user()
        self.factory = APIRequestFactory()

    def _authed(self, request):
        force_authenticate(request, user=self.user)
        return request

    def test_post_without_source_path_400(self):
        request = self._authed(self.factory.post("/api/archiver/run/", {}))
        with patch("api.archiver.views.async_task") as async_task:
            response = ArchiverRunView.as_view()(request)
        self.assertEqual(response.status_code, 400)
        async_task.assert_not_called()

    def test_post_with_unknown_source_path_400(self):
        request = self._authed(
            self.factory.post("/api/archiver/run/", {"source_path": "/no/such/dir"})
        )
        response = ArchiverRunView.as_view()(request)
        self.assertEqual(response.status_code, 400)

    def test_post_queues_run(self):
        with tempfile.TemporaryDirectory() as source:
            request = self._authed(
                self.factory.post("/api/archiver/run/", {"source_path": source})
            )
            with patch("api.archiver.views.async_task") as async_task:
                response = ArchiverRunView.as_view()(request)
            self.assertEqual(response.status_code, 202)
            self.assertTrue(response.data["status"], "queued")
            self.assertTrue(response.data["job_id"])
            async_task.assert_called_once()
            args = async_task.call_args[0]
            self.assertEqual(args[1], self.user.id)
            self.assertEqual(args[2], source)

    def test_get_reports_last_job(self):
        request = self._authed(self.factory.get("/api/archiver/"))
        response = ArchiverRunView.as_view()(request)
        self.assertEqual(response.data["status"], "idle")

        os.makedirs("/tmp/lp-view-test-src", exist_ok=True)
        request = self._authed(
            self.factory.post(
                "/api/archiver/run/", {"source_path": "/tmp/lp-view-test-src"}
            )
        )
        with patch("api.archiver.views.async_task"):
            response = ArchiverRunView.as_view()(request)
        LongRunningJob.create_job(
            user=self.user,
            job_type=LongRunningJob.JOB_ARCHIVE_DIRECTORY,
            job_id=response.data["job_id"],
            start_now=True,
        )
        request = self._authed(self.factory.get("/api/archiver/"))
        response = ArchiverRunView.as_view()(request)
        self.assertTrue(response.data["job_id"])
