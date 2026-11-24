"""
Manual transcript upload: the HTTP contract.

Every platform call is patched - these tests never open a platform connection and never reach
Graph or a model. They pin the HTTP layer: authentication on every route, the preview on the
READ-ONLY connection, storing and processing on the writing connection with a commit, the signed-in
user recorded as the uploader, the oversize refusal before anything is read, and every refusal
mapped to a status code rather than a 500. The behaviour itself is covered by
tests/unit/test_manual_transcript_upload.py and tests/integration/test_manual_transcript_upload.py.
"""
from contextlib import contextmanager
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from app.orchestration.locks import LectureBusy
from app.transcripts import manual_upload as mu
from operations import transcript_upload_views as views

LECTURE = "59929792-c621-5790-8b7c-12067f27db8e"
VTT = b"WEBVTT\n\n00:00:01.000 --> 00:05:00.000\n<v Trainer>Hello.</v>\n"
OPENED = []     # (kind, connection) for every platform connection a request opened


class _Base(TestCase):
    def setUp(self):
        user = get_user_model().objects.create(username="ops.uploader")
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=user).key}")
        OPENED.clear()
        self.service = mock.MagicMock()
        # Plain data by default: a MagicMock reaching the JSON renderer never finishes encoding.
        # Migration 030 is applied unless a test says otherwise.
        self.service.jobs.available.return_value = True
        self.service.recent_jobs.return_value = []
        self.service.queue_processing.return_value = {
            "queued": False, "created": False, "reason": "NOTHING_TO_PROCESS",
            "reason_message": "Nothing to process.", "job": None}
        for target, value in (("_service", lambda: self.service),
                              ("reading", self._opener("reading")),
                              ("writing", self._opener("writing"))):
            patcher = mock.patch.object(views, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _opener(kind):
        @contextmanager
        def opened():
            handle = mock.MagicMock(name=kind)
            OPENED.append((kind, handle))
            yield handle
        return opened

    def url(self, name):
        return reverse(name, kwargs={"lecture_id": LECTURE})

    def upload(self, name, data=None, content=VTT, filename="Ray MSP.vtt"):
        body = {"file": SimpleUploadedFile(filename, content, content_type="text/vtt"), **(data or {})}
        return self.client.post(self.url(name), body, format="multipart")


ROUTES = (
    ("get", "operations-transcript-upload-status"),
    ("post", "operations-transcript-upload-preview"),
    ("post", "operations-transcript-upload"),
    ("post", "operations-transcript-upload-process"),
)


class AuthenticationTests(TestCase):
    def test_every_upload_route_requires_authentication(self):
        anonymous = APIClient()
        with mock.patch.object(views, "_service") as service:
            for method, name in ROUTES:
                response = getattr(anonymous, method)(reverse(name, kwargs={"lecture_id": LECTURE}))
                self.assertIn(response.status_code, (401, 403), name)
            service.assert_not_called()

    def test_a_bad_token_is_refused(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Token not-a-real-token")
        self.assertEqual(client.get(reverse("operations-transcript-upload-status",
                                            kwargs={"lecture_id": LECTURE})).status_code, 401)

    def test_the_lecture_is_in_the_url_and_must_be_a_uuid(self):
        from django.urls import NoReverseMatch
        with self.assertRaises(NoReverseMatch):
            reverse("operations-transcript-upload", kwargs={"lecture_id": "not-a-uuid"})


class StatusAndPreviewTests(_Base):
    def test_status_is_read_on_the_read_only_connection(self):
        self.service.status.return_value = {"upload_allowed": True}
        response = self.client.get(self.url("operations-transcript-upload-status"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([kind for kind, _ in OPENED], ["reading"])
        self.assertEqual(str(self.service.status.call_args.args[1]), LECTURE)

    def test_the_preview_validates_on_the_read_only_connection_and_writes_nothing(self):
        self.service.uploads.preview.return_value = {"ok": True, "errors": []}
        response = self.upload("operations-transcript-upload-preview", {"artifact_id": "A1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([kind for kind, _ in OPENED], ["reading"])
        kwargs = self.service.uploads.preview.call_args.kwargs
        self.assertEqual((kwargs["filename"], kwargs["raw"], kwargs["artifact_id"]),
                         ("Ray MSP.vtt", VTT, "A1"))
        self.service.uploads.commit.assert_not_called()
        OPENED[0][1].commit.assert_not_called()

    def test_a_request_without_a_file_is_refused(self):
        response = self.client.post(self.url("operations-transcript-upload-preview"), {}, format="multipart")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], mu.EMPTY_FILE)

    def test_an_oversize_file_is_refused_before_the_platform_is_asked(self):
        with mock.patch.object(views, "MAX_UPLOAD_BYTES", 10):
            response = self.upload("operations-transcript-upload-preview")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], mu.FILE_TOO_LARGE)
        self.service.uploads.preview.assert_not_called()
        self.assertEqual(OPENED, [])

    def test_the_status_route_does_not_store(self):
        self.assertEqual(self.client.post(self.url("operations-transcript-upload-status")).status_code, 405)
        self.assertEqual(self.client.get(self.url("operations-transcript-upload")).status_code, 405)


class StoreTests(_Base):
    def test_a_confirmed_upload_is_stored_with_the_signed_in_user_and_committed(self):
        self.service.uploads.commit.return_value = {"status": "STORED", "stored": True}
        response = self.upload("operations-transcript-upload",
                               {"artifact_id": "A1", "expected_sha256": "ab" * 32})
        self.assertEqual(response.status_code, 201)
        ((kind, handle),) = OPENED
        self.assertEqual(kind, "writing")
        handle.commit.assert_called_once()
        kwargs = self.service.uploads.commit.call_args.kwargs
        self.assertEqual(kwargs["uploaded_by"], "ops.uploader")
        self.assertEqual((kwargs["artifact_id"], kwargs["expected_sha256"], kwargs["raw"]),
                         ("A1", "ab" * 32, VTT))

    def test_the_store_queues_processing_in_the_same_transaction(self):
        self.service.uploads.commit.return_value = {"status": "STORED", "stored": True}
        self.service.queue_processing.return_value = {"queued": True, "created": True, "job": {"job_id": "J1"}}
        response = self.upload("operations-transcript-upload", {"artifact_id": "A1", "expected_sha256": "x"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["queue"]["job"]["job_id"], "J1")
        ((_, handle),) = OPENED
        self.assertIs(self.service.queue_processing.call_args.args[0], handle)
        handle.commit.assert_called_once()
        self.service.process.assert_not_called()

    def test_without_migration_030_the_store_refuses_before_writing_anything(self):
        self.service.jobs.available.return_value = False
        response = self.upload("operations-transcript-upload", {"artifact_id": "A1", "expected_sha256": "x"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "QUEUE_UNAVAILABLE")
        self.assertIn("was not stored", response.json()["problems"][0]["message"])
        self.service.uploads.commit.assert_not_called()
        self.service.queue_processing.assert_not_called()
        OPENED[0][1].commit.assert_not_called()

    def test_a_refused_store_queues_nothing(self):
        self.service.uploads.commit.side_effect = mu.UploadRefused([mu.problem(mu.NOT_A_VTT_FILE)])
        self.upload("operations-transcript-upload", {"artifact_id": "A1"})
        self.service.queue_processing.assert_not_called()

    def test_an_identical_re_upload_is_a_200_not_a_201(self):
        self.service.uploads.commit.return_value = {"status": "ALREADY_STORED", "stored": False}
        response = self.upload("operations-transcript-upload", {"artifact_id": "A1", "expected_sha256": "x"})
        self.assertEqual(response.status_code, 200)

    def test_refusals_map_to_status_codes_and_are_never_committed(self):
        cases = (
            (mu.NOT_A_VTT_FILE, 400),
            (mu.LONGER_THAN_TEAMS_TRANSCRIPT, 400),
            (mu.PART_NOT_IN_THIS_LECTURE, 400),
            (mu.TRANSCRIPT_ALREADY_STORED, 409),
            (mu.CONTENT_BELONGS_TO_ANOTHER_TRANSCRIPT, 409),
            (mu.FILE_CHANGED_SINCE_PREVIEW, 409),
            (mu.LECTURE_NOT_FOUND, 404),
        )
        for code, expected in cases:
            with self.subTest(code=code):
                OPENED.clear()
                self.service.uploads.commit.side_effect = mu.UploadRefused([mu.problem(code)])
                response = self.upload("operations-transcript-upload", {"artifact_id": "A1"})
                self.assertEqual(response.status_code, expected)
                self.assertEqual(response.json()["code"], code)
                self.assertEqual(response.json()["problems"][0]["message"], mu.MESSAGES[code])
                OPENED[0][1].commit.assert_not_called()

    def test_a_busy_lecture_is_a_conflict(self):
        self.service.uploads.commit.side_effect = LectureBusy(LECTURE)
        response = self.upload("operations-transcript-upload", {"artifact_id": "A1"})
        self.assertEqual((response.status_code, response.json()["code"]), (409, "LECTURE_LOCKED"))


class ProcessTests(_Base):
    """POST process/ QUEUES the lecture for the background worker; it never runs the pipeline."""

    JOB = {"job_id": "J1", "lecture_id": LECTURE, "status": "QUEUED", "attempt_count": 0}

    def post(self):
        return self.client.post(self.url("operations-transcript-upload-process"), {}, format="json")

    def test_processing_is_queued_on_the_writing_connection_and_committed(self):
        self.service.queue_processing.return_value = {"queued": True, "created": True, "job": self.JOB}
        response = self.post()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["job"]["status"], "QUEUED")
        ((kind, handle),) = OPENED
        self.assertEqual(kind, "writing")
        handle.commit.assert_called_once()
        self.assertEqual(self.service.queue_processing.call_args.kwargs["requested_by"], "ops.uploader")
        # Nothing runs in the request: the paid QA is the worker's.
        self.service.process.assert_not_called()

    def test_a_repeat_returns_the_same_job(self):
        self.service.queue_processing.return_value = {"queued": True, "created": False,
                                                      "reason": "ALREADY_QUEUED", "job": self.JOB}
        response = self.post()
        self.assertEqual((response.status_code, response.json()["job"]["job_id"]), (202, "J1"))

    def test_nothing_to_queue_says_why_and_commits_nothing(self):
        self.service.queue_processing.return_value = {
            "queued": False, "created": False, "reason": "WAITING_FOR_TRANSCRIPT_PARTS",
            "reason_message": "Other transcript parts are still missing.", "job": None}
        response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "WAITING_FOR_TRANSCRIPT_PARTS")
        OPENED[0][1].commit.assert_not_called()
        self.service.process.assert_not_called()


class RecoveryListTests(_Base):
    """GET transcript-recovery/: the Operations dashboard's cross-date list. Read-only."""

    def recovery(self, **params):
        return self.client.get(reverse("operations-transcript-recovery"), params)

    def test_it_requires_authentication(self):
        anonymous = APIClient()
        self.assertIn(anonymous.get(reverse("operations-transcript-recovery")).status_code, (401, 403))
        self.service.uploads.recovery_queue.assert_not_called()

    def test_it_reads_every_date_on_the_read_only_connection(self):
        self.service.uploads.recovery_queue.return_value = {"lectures": [], "count": 0}
        response = self.recovery()
        self.assertEqual(response.status_code, 200)
        self.assertEqual([kind for kind, _ in OPENED], ["reading"])
        OPENED[0][1].commit.assert_not_called()
        kwargs = self.service.uploads.recovery_queue.call_args.kwargs
        self.assertEqual((kwargs["date_from"], kwargs["date_to"], kwargs["limit"]),
                         (None, None, mu.RECOVERY_QUEUE_LIMIT))

    def test_it_can_be_bounded_to_one_day(self):
        from datetime import date
        self.service.uploads.recovery_queue.return_value = {"lectures": []}
        self.assertEqual(self.recovery(**{"from": "2026-11-13", "to": "2026-11-13"}).status_code, 200)
        kwargs = self.service.uploads.recovery_queue.call_args.kwargs
        self.assertEqual((kwargs["date_from"], kwargs["date_to"]), (date(2026, 11, 13), date(2026, 11, 13)))

    def test_bad_parameters_are_refused_before_the_platform_is_asked(self):
        for params in ({"from": "13/11/2026"}, {"limit": "x"}, {"limit": "0"},
                       {"limit": str(mu.RECOVERY_QUEUE_LIMIT + 1)},
                       {"from": "2026-11-14", "to": "2026-11-13"}):
            self.assertEqual(self.recovery(**params).status_code, 400, params)
        self.service.uploads.recovery_queue.assert_not_called()
        self.assertEqual(OPENED, [])

    def test_it_lists_the_recent_processing_jobs(self):
        self.service.uploads.recovery_queue.return_value = {"lectures": []}
        self.service.jobs.available.return_value = True
        self.service.recent_jobs.return_value = [{"job_id": "J1", "status": "RUNNING"}]
        body = self.recovery().json()
        self.assertEqual((body["jobs"][0]["status"], body["queue_installed"]), ("RUNNING", True))

    def test_before_migration_030_there_are_no_jobs(self):
        self.service.uploads.recovery_queue.return_value = {"lectures": []}
        self.service.jobs.available.return_value = False
        body = self.recovery().json()
        self.assertEqual((body["jobs"], body["queue_installed"]), ([], False))
        self.service.recent_jobs.assert_not_called()

    def test_it_takes_no_writes(self):
        self.assertEqual(self.client.post(reverse("operations-transcript-recovery")).status_code, 405)
