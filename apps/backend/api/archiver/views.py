"""
Archiver API: copy+cull a source directory into the library.

POST /api/archiver/run/
    Body: {"source_path": "/absolute/path/to/dir"}
    Validates the path and queues the archiver as a django-q task. Returns
    202 with the queued status.

GET  /api/archiver/
    Returns the most recent run for the user with its outcome counters.
"""

import os
import uuid

from django_q.tasks import async_task
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from api.archiver.engine import run_archiver
from api.models import LongRunningJob


class ArchiverRunView(APIView):
    """Queue (POST) or report (GET) an archiver run."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        request={"application/json": {"type": "object", "properties": {}}},
        parameters=[
            OpenApiParameter(
                "source_path",
                str,
                OpenApiParameter.QUERY,
                description="Absolute path to the directory to copy+CULL",
            ),
        ],
        responses={202: {"type": "object"}},
    )
    def post(self, request):
        source_path = request.data.get("source_path")
        if not source_path:
            return Response(
                {"status": "error", "message": "source_path is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not os.path.isdir(source_path):
            return Response(
                {
                    "status": "error",
                    "message": f"source_path '{source_path}' is not a directory",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        job_id = uuid.uuid4()
        async_task(run_archiver, request.user.id, source_path, None, str(job_id))
        return Response(
            {
                "status": "queued",
                "job_id": str(job_id),
                "message": "Archiver started; poll GET /api/archiver/ for progress",
                "source_path": source_path,
            },
            status=status.HTTP_202_ACCEPTED,
        )

    @extend_schema(responses={200: {"type": "object"}})
    def get(self, request):
        job = (
            LongRunningJob.objects.filter(
                started_by=request.user,
                job_type=LongRunningJob.JOB_ARCHIVE_DIRECTORY,
            )
            .order_by("-started_at")
            .first()
        )
        if job is None:
            return Response({"status": "idle"})
        return Response(
            {
                "job_id": job.job_id,
                "started_at": job.started_at,
                "finished": job.finished,
                "failed": job.failed,
                "result": job.result,
            }
        )
