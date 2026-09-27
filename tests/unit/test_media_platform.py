"""
The Positive Moments media platform, offline: alignment, clip planning, the
Creatomate provider, SharePoint delivery, the webhook guard and the dashboard.

No network (tests/conftest.py), no real Creatomate credits, no real Graph.
"""
from __future__ import annotations

import email.message
import io
import json
import os
import urllib.request
import urllib.response
from datetime import datetime, timedelta, timezone

import pytest

from app.media import alignment as al
from app.media.clip_plan import (
    EVIDENCE_OUTSIDE_LINKED_RECORDING,
    PlanRefused,
    plan_clip,
    plan_fingerprint,
)
from app.media.dashboard import COUNTERS, row_filters
from app.media.delivery import (
    UPLOAD_CHUNK_UNIT,
    DeliveryError,
    SharePointMedia,
    output_filename,
)
from app.media.moments_media import media_state
from app.media.render.base import (
    FAILED,
    SUCCEEDED,
    RenderPolicy,
    RenderProviderError,
    RenderRequest,
    RenderStatus,
    estimate_credits,
    validate_output,
)
from app.media.render.creatomate import CreatomateMediaRenderProvider, build_render_script
from app.media.source import READY, WAITING_FOR_RECORDING, resolve_recording
from app.media.webhook import WebhookRefused, handle_creatomate_webhook, webhook_token
from tests.unit.recording_graph_fakes import FakeGraph, graph_error

UTC = timezone.utc
GRAPH_CREATED = datetime(2026, 9, 25, 8, 1, 0, tzinfo=UTC)
LEAD = 40                                    # file named 40 s before Graph
FILE_NAME = (f"AI in Project Control 2026-"
             f"{GRAPH_CREATED - timedelta(seconds=LEAD):%Y%m%d_%H%M%S}UTC-Meeting Recording.mp4")
WINDOW = 7936.0                              # Graph end - created, seconds


def recording(created=GRAPH_CREATED, window=WINDOW, corr="corr-1", rid="rec-1"):
    return al.RecordingRef(rid, created.isoformat(),
                           (created + timedelta(seconds=window)).isoformat() if window else None,
                           corr)


def part(offset_ms=0, created=GRAPH_CREATED, corr="corr-1", index=1, window=WINDOW):
    return al.TranscriptPart(index, offset_ms, created,
                             created + timedelta(seconds=window) if window else None, corr)


def aligned(duration=WINDOW, **kwargs):
    return al.align(file_name=FILE_NAME, file_duration_seconds=duration,
                    recordings=kwargs.pop("recordings", [recording()]),
                    parts=kwargs.pop("parts", [part()]))


# ---------------------------------------------------------------------------
# alignment
# ---------------------------------------------------------------------------

def test_a_file_whose_duration_matches_graph_starts_at_graph_created():
    result = aligned()
    assert result.status == al.ALIGNED and result.method == al.METHOD_GRAPH_CREATED
    assert result.media_offset_seconds == 0.0
    assert result.confidence == 0.99
    assert result.detail["pairing"] == "content_correlation_id"
    assert aligned().media_offset_seconds == result.media_offset_seconds     # deterministic


def test_a_file_that_includes_the_pre_roll_starts_at_its_file_name_time():
    result = aligned(duration=WINDOW + LEAD)
    assert result.status == al.ALIGNED and result.method == al.METHOD_FILE_NAME
    # canonical 0 == Graph created == 40 s into the file
    assert result.media_offset_seconds == -LEAD


def test_a_later_part_uses_its_own_canonical_offset():
    second = GRAPH_CREATED + timedelta(hours=4)
    parts = [part(0, GRAPH_CREATED, corr="corr-a", index=1),
             part(14_400_000, second, corr="corr-b", index=2)]
    name = (f"AI in Project Control 2026-{second - timedelta(seconds=LEAD):%Y%m%d_%H%M%S}"
            "UTC-Meeting Recording.mp4")
    result = al.align(file_name=name, file_duration_seconds=3600,
                      recordings=[recording(second, 3600, corr="corr-b", rid="rec-b")],
                      parts=parts)
    assert result.status == al.ALIGNED and result.media_offset_seconds == 14_400.0
    assert result.canonical_start_seconds == 14_400.0


def test_an_unexplained_duration_is_never_aligned():
    result = aligned(duration=WINDOW - 600)
    assert result.status == al.REVIEW_REQUIRED_ALIGNMENT
    assert result.code == "FILE_DURATION_CONTRADICTS_RECORDING"


def test_no_graph_recording_for_the_file_is_review():
    far = recording(GRAPH_CREATED + timedelta(minutes=30))
    assert aligned(recordings=[far]).code == "NO_GRAPH_RECORDING_FOR_FILE"


def test_no_transcript_part_for_the_recording_is_review():
    result = aligned(parts=[part(corr="other", created=GRAPH_CREATED + timedelta(minutes=5))])
    assert result.code == "NO_TRANSCRIPT_PART_FOR_RECORDING"


def test_a_missing_measurement_waits_instead_of_guessing():
    assert aligned(duration=None).status == al.WAITING_FOR_ALIGNMENT
    assert aligned(recordings=[]).status == al.WAITING_FOR_ALIGNMENT


def test_created_time_pairing_is_used_without_correlation_ids():
    result = aligned(recordings=[recording(corr=None)], parts=[part(corr=None)])
    assert result.status == al.ALIGNED and result.detail["pairing"] == "created_at"
    assert result.confidence == 0.95


def test_both_origins_fitting_with_a_real_lead_is_ambiguous():
    name = (f"AI in Project Control 2026-{GRAPH_CREATED - timedelta(seconds=3):%Y%m%d_%H%M%S}"
            "UTC-Meeting Recording.mp4")
    result = al.align(file_name=name, file_duration_seconds=WINDOW + 1.5,
                      recordings=[recording()], parts=[part()])
    assert result.code == "ALIGNMENT_ORIGIN_AMBIGUOUS"


# ---------------------------------------------------------------------------
# clip plan: +/- 60 s, clamped, evidence never cut
# ---------------------------------------------------------------------------

def test_sixty_seconds_of_context_either_side():
    evidence = (80 * 60 + 15) * 1000, (80 * 60 + 37) * 1000        # 01:20:15 -> 01:20:37
    plan = plan_clip(evidence_start_ms=evidence[0], evidence_end_ms=evidence[1],
                     alignment=aligned())
    assert (plan.requested_start_seconds, plan.requested_end_seconds) == (4755.0, 4897.0)
    assert (plan.actual_start_seconds, plan.actual_end_seconds) == (4755.0, 4897.0)
    assert (plan.padding_before_applied, plan.padding_after_applied) == (60.0, 60.0)
    assert plan.duration_seconds == 142.0


def test_the_offset_is_applied_to_the_media_range():
    plan = plan_clip(evidence_start_ms=100_000, evidence_end_ms=110_000,
                     alignment=aligned(duration=WINDOW + LEAD))
    assert (plan.media_evidence_start_seconds, plan.media_evidence_end_seconds) == (140.0, 150.0)
    assert (plan.actual_start_seconds, plan.actual_end_seconds) == (80.0, 210.0)


def test_padding_clamps_to_zero_and_to_the_file_end_without_cutting_evidence():
    early = plan_clip(evidence_start_ms=10_000, evidence_end_ms=20_000, alignment=aligned())
    assert early.actual_start_seconds == 0.0 and early.padding_before_applied == 10.0
    assert early.media_evidence_start_seconds == 10.0
    late = plan_clip(evidence_start_ms=int((WINDOW - 30) * 1000),
                     evidence_end_ms=int((WINDOW - 10) * 1000), alignment=aligned())
    assert late.actual_end_seconds == WINDOW and late.padding_after_applied == 10.0
    assert late.media_evidence_end_seconds == WINDOW - 10


def test_evidence_outside_the_linked_recording_is_refused():
    with pytest.raises(PlanRefused) as exc:
        plan_clip(evidence_start_ms=int((WINDOW + 100) * 1000),
                  evidence_end_ms=int((WINDOW + 120) * 1000), alignment=aligned())
    assert exc.value.code == EVIDENCE_OUTSIDE_LINKED_RECORDING


def test_an_unaligned_lecture_cannot_be_planned():
    with pytest.raises(PlanRefused) as exc:
        plan_clip(evidence_start_ms=1000, evidence_end_ms=2000,
                  alignment=aligned(duration=WINDOW - 600))
    assert exc.value.code == "FILE_DURATION_CONTRADICTS_RECORDING"


def test_the_plan_fingerprint_changes_with_source_offset_range_or_policy():
    alignment = aligned()
    plan = plan_clip(evidence_start_ms=100_000, evidence_end_ms=110_000, alignment=alignment)
    policy = RenderPolicy().as_dict()
    base = dict(moment_fingerprint="m", source_drive_id="d", source_item_id="i",
                alignment=alignment, plan=plan, render_policy=policy, provider="creatomate")
    one = plan_fingerprint(**base)
    assert one == plan_fingerprint(**base)
    assert one != plan_fingerprint(**{**base, "source_item_id": "other"})
    assert one != plan_fingerprint(**{**base, "render_policy": {**policy, "frame_rate": 30}})
    shifted = aligned(duration=WINDOW + LEAD)
    assert one != plan_fingerprint(**{**base, "alignment": shifted})


# ---------------------------------------------------------------------------
# rendering: Creatomate
# ---------------------------------------------------------------------------

TEMP_URL = "https://kbc.sharepoint.com/_layouts/15/download.aspx?tempauth=SECRET-TEMP"
KEY = "ck_live_SECRET_KEY"


def request(**overrides):
    values = dict(job_id="job-1", source_url=TEMP_URL, trim_start_seconds=4755.0,
                  trim_duration_seconds=142.0, policy=RenderPolicy(),
                  webhook_url="https://kbc.example/api/operations/media/webhooks/creatomate/?job=j")
    values.update(overrides)
    return RenderRequest(**values)


def test_the_renderscript_trims_the_source_with_the_default_policy():
    body = build_render_script(request())
    assert body["output_format"] == "mp4" and body["frame_rate"] == 25
    assert (body["max_width"], body["max_height"]) == (1280, 720)
    [element] = body["elements"]
    assert element["type"] == "video" and element["source"] == TEMP_URL
    assert (element["trim_start"], element["trim_duration"]) == (4755.0, 142.0)
    assert element["fit"] == "contain"
    assert body["webhook_url"].startswith("https://")
    assert not {"animations", "text", "captions"} & set(json.dumps(body).split('"'))


class FakeHttp:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": json.loads(body) if body else None})
        status, payload = self.replies.pop(0)
        return status, json.dumps(payload).encode()


def test_submit_posts_to_v2_renders_with_bearer_auth():
    http = FakeHttp([(202, {"id": "r-1", "status": "planned"})])
    provider = CreatomateMediaRenderProvider(api_key=KEY, http=http)
    status = provider.submit(request())
    assert (status.render_id, status.status) == ("r-1", "QUEUED")
    call = http.calls[0]
    assert (call["method"], call["url"]) == ("POST", "https://api.creatomate.com/v2/renders")
    assert call["headers"]["Authorization"] == f"Bearer {KEY}"
    assert call["body"]["elements"][0]["trim_duration"] == 142.0


def test_a_list_response_with_one_render_is_accepted():
    http = FakeHttp([(200, [{"id": "r-1", "status": "rendering"}])])
    assert CreatomateMediaRenderProvider(api_key=KEY, http=http).get_status("r-1").status == \
        "RENDERING"


@pytest.mark.parametrize("provider_status, state", [
    ("planned", "QUEUED"), ("waiting", "QUEUED"), ("transcribing", "RENDERING"),
    ("rendering", "RENDERING"), ("succeeded", SUCCEEDED), ("failed", FAILED)])
def test_every_official_status_maps(provider_status, state):
    http = FakeHttp([(200, {"id": "r", "status": provider_status,
                            "url": "https://cdn.creatomate.com/renders/r.mp4"})])
    assert CreatomateMediaRenderProvider(api_key=KEY, http=http).get_status("r").status == state


def test_provider_errors_never_carry_the_key_or_source_url():
    http = FakeHttp([(401, {"message": f"bad key {KEY}"})])
    provider = CreatomateMediaRenderProvider(api_key=KEY, http=http)
    with pytest.raises(RenderProviderError) as exc:
        provider.submit(request())
    text = f"{exc.value} {exc.value.code} {provider!r} {request()!r}"
    assert KEY not in text and "SECRET-TEMP" not in text
    assert exc.value.code == "CREATOMATE_AUTH_REJECTED" and exc.value.retryable is False
    throttled = CreatomateMediaRenderProvider(api_key=KEY, http=FakeHttp([(429, {})]))
    with pytest.raises(RenderProviderError) as exc:
        throttled.get_status("r")
    assert exc.value.retryable is True


def test_a_missing_key_refuses_to_build():
    with pytest.raises(RenderProviderError):
        CreatomateMediaRenderProvider(api_key="")


def test_credits_follow_the_published_pixel_rule():
    policy = RenderPolicy()
    assert estimate_credits(10, policy) == pytest.approx(2.304)
    assert estimate_credits(1, policy) == 1.0                    # minimum one credit
    assert estimate_credits(142, policy) == pytest.approx(32.717, abs=0.001)


def test_a_succeeded_render_is_validated_against_the_plan():
    good = RenderStatus("r", SUCCEEDED, "succeeded", "https://cdn.creatomate.com/r.mp4",
                        142.04, 50_000_000, 1280, 720)
    assert validate_output(good, expected_duration_seconds=142.0, policy=RenderPolicy()) == []
    short = RenderStatus("r", SUCCEEDED, "succeeded", "https://cdn.creatomate.com/r.mp4", 30.0)
    assert "RENDER_DURATION_MISMATCH" in validate_output(
        short, expected_duration_seconds=142.0, policy=RenderPolicy())


def test_no_second_render_provider_is_implemented_yet():
    import pathlib
    names = {p.stem for p in pathlib.Path("app/media/render").glob("*.py")}
    assert names == {"__init__", "base", "creatomate"}


# ---------------------------------------------------------------------------
# SharePoint delivery: bounded memory, chunked upload, never overwrite
# ---------------------------------------------------------------------------

class Response(io.BytesIO):
    def __init__(self, body=b"", status=200):
        super().__init__(body)
        self.status = status
        self.reads = []

    def read(self, size=-1):
        self.reads.append(size)
        return super().read(size)


class FakeOpen:
    def __init__(self, download=b"", upload_status=202, final_item=None):
        self.download, self.final_item = download, final_item or {}
        self.calls = []
        self.response = None

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), len(body) if body else 0))
        if method == "GET":
            self.response = Response(self.download)
            return self.response
        start, end_total = headers["Content-Range"].split(" ")[1].split("-")
        end, total = end_total.split("/")
        if int(end) + 1 == int(total):
            return Response(json.dumps(self.final_item).encode(), 201)
        return Response(b"{}", 202)


def test_the_render_is_streamed_to_one_temp_file_in_bounded_reads(tmp_path):
    payload = os.urandom(3 * 1024 * 1024 + 7)
    opener = FakeOpen(download=payload)
    media = SharePointMedia(FakeGraph(), open_url=opener)
    path = media.download_to_temp("https://cdn.creatomate.com/r.mp4", expected_size=len(payload),
                                  directory=str(tmp_path))
    try:
        assert os.path.getsize(path) == len(payload)
        assert max(opener.response.reads) == 1024 * 1024        # never read(-1)
    finally:
        os.remove(path)


def test_a_size_mismatch_removes_the_temp_file(tmp_path):
    media = SharePointMedia(FakeGraph(), open_url=FakeOpen(download=b"x" * 100))
    with pytest.raises(DeliveryError):
        media.download_to_temp("https://cdn/r.mp4", expected_size=101, directory=str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def upload_graph(folder="folder-1", name="clip.mp4", conflict=None, existing=None):
    posts = {f"/drives/drive-1/items/{folder}:/{name}:/createUploadSession":
             {"uploadUrl": "https://kbc.sharepoint.com/upload?session=SECRET-UPLOAD"}}
    errors = {"/drives/drive-1/items/folder-1:/clip.mp4:/createUploadSession": conflict} \
        if conflict else {}
    json_ = {f"/drives/drive-1/items/{folder}:/{name}?$select=id,name,size,webUrl": existing} \
        if existing else {}
    return FakeGraph(posts=posts, errors=errors, json=json_)


def test_upload_uses_320_kib_multiple_chunks_without_auth_headers(tmp_path):
    size = 3 * UPLOAD_CHUNK_UNIT + 11
    path = tmp_path / "clip.bin"
    path.write_bytes(os.urandom(size))
    opener = FakeOpen(final_item={"id": "item-9", "size": size,
                                  "webUrl": "https://kbc.sharepoint.com/sites/x/clip.mp4"})
    media = SharePointMedia(upload_graph(), open_url=opener,
                            upload_chunk_bytes=UPLOAD_CHUNK_UNIT)
    delivered = media.upload(str(path), drive_id="drive-1", folder_item_id="folder-1",
                             filename="clip.mp4")
    assert delivered.item_id == "item-9" and delivered.adopted_existing is False
    puts = [call for call in opener.calls if call[0] == "PUT"]
    assert [call[3] for call in puts] == [UPLOAD_CHUNK_UNIT] * 3 + [11]
    assert all("Authorization" not in call[2] for call in puts)
    assert puts[0][2]["Content-Range"] == f"bytes 0-{UPLOAD_CHUNK_UNIT - 1}/{size}"


def test_chunk_sizes_must_be_multiples_of_320_kib():
    with pytest.raises(ValueError):
        SharePointMedia(FakeGraph(), upload_chunk_bytes=1000)


def test_an_existing_different_file_is_never_overwritten(tmp_path):
    path = tmp_path / "clip.bin"
    path.write_bytes(b"x" * 500)
    graph = upload_graph(conflict=graph_error(409, "nameAlreadyExists"),
                         existing={"id": "old", "size": 999,
                                   "webUrl": "https://kbc.sharepoint.com/old.mp4"})
    with pytest.raises(DeliveryError) as exc:
        SharePointMedia(graph).upload(str(path), drive_id="drive-1",
                                      folder_item_id="folder-1", filename="clip.mp4")
    assert exc.value.code == "DESTINATION_NAME_CONFLICT" and exc.value.retryable is False


def test_the_same_file_already_uploaded_is_adopted(tmp_path):
    path = tmp_path / "clip.bin"
    path.write_bytes(b"x" * 500)
    graph = upload_graph(conflict=graph_error(409, "nameAlreadyExists"),
                         existing={"id": "same", "size": 500,
                                   "webUrl": "https://kbc.sharepoint.com/same.mp4"})
    delivered = SharePointMedia(graph).upload(str(path), drive_id="drive-1",
                                              folder_item_id="folder-1", filename="clip.mp4")
    assert delivered.adopted_existing is True and delivered.item_id == "same"


SOURCE_PATH = "/drives/d/items/i?$select=id,name,size,video,file,content.downloadUrl"
CONTENT_PATH = "/drives/d/items/i/content"
REDIRECT_URL = "https://kbc.sharepoint.com/_layouts/15/download.aspx?tempauth=SECRET-REDIRECT"


class SourceGraph(FakeGraph):
    """FakeGraph plus the no-redirect `/content` probe."""

    def __init__(self, item=None, *, redirect=(302, REDIRECT_URL), errors=None):
        super().__init__(json={SOURCE_PATH: item} if item is not None else {},
                         errors=errors)
        self.redirect = redirect

    def redirect_location(self, path):
        self.paths.append(("GET-NO-REDIRECT", path))
        self._raise_if(path)
        return self.redirect


def source_item(**extra):
    return {"id": "i", "name": "x.mp4", "size": 10, "video": {"duration": 7_976_062}, **extra}


def test_a_fresh_source_url_is_read_for_the_exact_item_and_kept_out_of_repr():
    graph = SourceGraph(source_item(**{"@microsoft.graph.downloadUrl": TEMP_URL}))
    source = SharePointMedia(graph).source_file("d", "i")
    assert source.download_url == TEMP_URL and source.duration_seconds == 7976.062
    assert "SECRET-TEMP" not in repr(source)
    # The annotation is requested the way Graph honours it, and /content is not needed.
    assert graph.paths == [("GET", SOURCE_PATH)]


def test_a_missing_annotation_falls_back_to_the_content_redirect_location():
    graph = SourceGraph(source_item())                   # the production shape, 2026-09-27
    source = SharePointMedia(graph).source_file("d", "i")
    assert source.download_url == REDIRECT_URL
    assert graph.paths == [("GET", SOURCE_PATH), ("GET-NO-REDIRECT", CONTENT_PATH)]
    assert "SECRET-REDIRECT" not in repr(source)


def test_a_different_drive_item_fails_closed_before_any_url_is_used():
    graph = SourceGraph(source_item(id="other", **{"@microsoft.graph.downloadUrl": TEMP_URL}))
    with pytest.raises(DeliveryError) as caught:
        SharePointMedia(graph).source_file("d", "i")
    assert caught.value.code == "SOURCE_GRAPH_ITEM_MISMATCH"
    assert caught.value.retryable is False
    assert ("GET-NO-REDIRECT", CONTENT_PATH) not in graph.paths


@pytest.mark.parametrize("redirect, code", [
    ((200, None), "SOURCE_GRAPH_DOWNLOAD_URL_UNAVAILABLE"),       # no annotation, no redirect
    ((302, None), "SOURCE_GRAPH_CONTENT_REDIRECT_MISSING"),
    ((302, "http://insecure.example/x"), "SOURCE_GRAPH_CONTENT_REDIRECT_MISSING"),
])
def test_no_usable_download_location_is_a_safe_retryable_error(redirect, code):
    with pytest.raises(DeliveryError) as caught:
        SharePointMedia(SourceGraph(source_item(), redirect=redirect)).source_file("d", "i")
    assert caught.value.code == code and caught.value.retryable is True
    assert "://" not in str(caught.value) and "insecure.example" not in str(caught.value)


@pytest.mark.parametrize("status, code", [
    (403, "SOURCE_GRAPH_PERMISSION_DENIED"), (401, "SOURCE_GRAPH_PERMISSION_DENIED"),
    (404, "SOURCE_GRAPH_ITEM_NOT_FOUND"),
])
@pytest.mark.parametrize("failing_path", [SOURCE_PATH, CONTENT_PATH])
def test_permanent_graph_refusals_are_final_not_timed_retries(status, code, failing_path):
    graph = SourceGraph(source_item(), errors={failing_path: graph_error(status)})
    with pytest.raises(DeliveryError) as caught:
        SharePointMedia(graph).source_file("d", "i")
    assert caught.value.code == code and caught.value.retryable is False


def test_a_transient_graph_failure_stays_retryable():
    graph = SourceGraph(source_item(), errors={SOURCE_PATH: graph_error(503, "serviceNotAvailable")})
    with pytest.raises(DeliveryError) as caught:
        SharePointMedia(graph).source_file("d", "i")
    assert caught.value.code == "SOURCE_ITEM_HTTP_503" and caught.value.retryable is True


# -- the real Graph client, through the real urllib opener, no network ------------

class _Body(io.BytesIO):
    def __init__(self, data=b""):
        super().__init__(data)
        self.bytes_read = 0

    def read(self, *args):
        chunk = super().read(*args)
        self.bytes_read += len(chunk)
        return chunk


class FakeGraphHost(urllib.request.BaseHandler):
    """Answers https requests in-process; records every URL it is asked for."""

    handler_order = 100                     # ahead of urllib's real HTTPSHandler

    def __init__(self, status, headers=None, body=b""):
        self.status, self.headers, self.body = status, headers or {}, _Body(body)
        self.requests = []

    def https_open(self, request):
        self.requests.append(request.full_url)
        message = email.message.Message()
        for name, value in self.headers.items():
            message[name] = value
        response = urllib.response.addinfourl(self.body, message, request.full_url, self.status)
        response.msg = "fake"
        return response


def graph_client(host=None, *, json_body=None):
    from app.graph.client import Phase1GraphClient, _NoRedirect
    from app.graph.transport import GraphResponse
    client = Phase1GraphClient(tenant_id="t", client_id="c", client_secret="s",
                               scope="https://graph.microsoft.com/.default",
                               base_url="https://graph.test/v1.0")
    client._access_token = lambda: "TOKEN"
    if host is not None:
        client._build_no_redirect_opener = lambda: urllib.request.build_opener(_NoRedirect, host)
    if json_body is not None:
        client._open = lambda request, service: GraphResponse(
            json.dumps(json_body).encode(), "application/json", 200)
    return client


def test_the_graph_client_keeps_the_download_annotation():
    client = graph_client(json_body=source_item(**{"@microsoft.graph.downloadUrl": TEMP_URL}))
    assert client.get_json(SOURCE_PATH)["@microsoft.graph.downloadUrl"] == TEMP_URL


def test_content_redirect_is_read_but_never_followed_and_no_body_is_read():
    big = b"\0" * (4 * 1024 * 1024)                 # stands in for a multi-GB recording
    host = FakeGraphHost(302, {"Location": REDIRECT_URL}, big)
    status, location = graph_client(host).redirect_location(CONTENT_PATH)
    assert (status, location) == (302, REDIRECT_URL)
    assert host.requests == ["https://graph.test/v1.0/drives/d/items/i/content"]  # not followed
    assert host.body.bytes_read == 0


def test_a_content_endpoint_that_streams_the_file_is_not_read():
    host = FakeGraphHost(200, {"Content-Type": "video/mp4"}, b"\0" * (4 * 1024 * 1024))
    assert graph_client(host).redirect_location(CONTENT_PATH) == (200, None)
    assert host.body.bytes_read == 0


def test_a_content_refusal_is_a_sanitised_graph_error():
    from app.graph.transport import GraphError
    host = FakeGraphHost(403, {"Content-Type": "application/json"},
                         b'{"error": {"code": "accessDenied", "message": "denied"}}')
    with pytest.raises(GraphError) as caught:
        graph_client(host).redirect_location(CONTENT_PATH)
    assert (caught.value.status, caught.value.code) == (403, "accessDenied")
    assert "TOKEN" not in str(caught.value)


def test_the_no_redirect_probe_refuses_absolute_urls():
    with pytest.raises(ValueError):
        graph_client().redirect_location(REDIRECT_URL)


def test_output_names_are_deterministic_and_collision_resistant():
    name = output_filename(session_date="2026-09-25", subject="AI in Project Control 2026",
                           lecture_id="1a2b3c4d-0000-0000-0000-000000000000", moment_index=1,
                           plan_fingerprint="9f8e7d6c5b4a")
    assert name == "20260925_AI-in-Project-Control-2026_1a2b3c4d_positive-01_9f8e7d6c.mp4"


# ---------------------------------------------------------------------------
# recording source: consume Recording Link, never match again
# ---------------------------------------------------------------------------

def stage_state(state="COMPLETE", **extra):
    return {"stages": {"RECORDING_LINK": {"state": state, **extra}},
            "executable_stage": extra.pop("executable", None)}


LEGACY = {"recording_url": "https://kbc.sharepoint.com/:v:/r", "recording_drive_id": "d",
          "recording_item_id": "i", "recording_filename": FILE_NAME,
          "recording_link_status": "organization_view_link_created_exact_match"}


def test_the_v2_resolver_result_is_reused_with_its_evidence():
    coded = {"status": "WRITTEN", "reason": "organization_view_link_created_exact_match",
             "recording_item_id": "i", "last_attempted_at": GRAPH_CREATED,
             "metadata": {"resolution_policy": "recording_match_v2_full_recording_resolution",
                          "graph": {"recordings": [{"recording_id": "rec-1",
                                                    "created_at": GRAPH_CREATED.isoformat(),
                                                    "end_at": None,
                                                    "content_correlation_id": "c"}]},
                          "resolution": {"rule": "full_recording_selected_over_short_fragment",
                                         "selected": {"duration_seconds": 7976.062,
                                                      "duration_source": "video_facet"}}}}
    source = resolve_recording(state=stage_state(), legacy=LEGACY, coded_link=coded)
    assert source.state == READY and (source.drive_id, source.item_id) == ("d", "i")
    assert source.rule == "full_recording_selected_over_short_fragment"
    assert source.duration_seconds == 7976.062
    assert source.recordings[0].recording_id == "rec-1"


@pytest.mark.parametrize("stage, coded, status", [
    ({"state": "REVIEW_REQUIRED", "action": "MANUAL_REVIEW_REQUIRED",
      "reason": "AMBIGUOUS_RECORDING_FILES"}, {"status": "AMBIGUOUS_RECORDING_FILES"},
     "AMBIGUOUS_RECORDING_FILES"),
    ({"state": "REVIEW_REQUIRED", "reason": "MULTIPART_RECORDING"},
     {"status": "MULTIPART_RECORDING"}, "MULTIPART_RECORDING"),
    ({"state": "WAITING", "action": "WAIT_FOR_RECORDING"},
     {"status": "GRAPH_LOOKUP_FAILED"}, "GRAPH_LOOKUP_FAILED"),
    ({"state": "MISSING", "action": "WAIT_FOR_RECORDING", "reason": None}, None, "MISSING"),
])
def test_an_unresolved_recording_waits_with_its_real_status(stage, coded, status):
    state = {"stages": {"RECORDING_LINK": stage}}
    source = resolve_recording(state=state, legacy=None, coded_link=coded)
    assert source.state == WAITING_FOR_RECORDING and source.status == status


def test_a_recording_blocked_behind_an_earlier_stage_says_so():
    state = {"stages": {"RECORDING_LINK": {"state": "MISSING", "action": "LINK_RECORDING"}},
             "executable_stage": "LEGACY_QA_SYNC"}
    source = resolve_recording(state=state, legacy=None, coded_link=None)
    assert source.status == "BLOCKED_BY_EARLIER_STAGE"
    assert source.reason == "waiting for LEGACY_QA_SYNC"


def test_a_link_without_durable_identity_is_not_rediscovered_from_its_url():
    no_ids = {**LEGACY, "recording_item_id": None}
    source = resolve_recording(state=stage_state(), legacy=no_ids, coded_link=None)
    assert source.status == "RECORDING_IDENTITY_UNAVAILABLE"
    import inspect, app.media.source as module
    assert "urlsplit" not in inspect.getsource(module) and "urlparse" not in inspect.getsource(module)


# ---------------------------------------------------------------------------
# the webhook: nothing in it is trusted
# ---------------------------------------------------------------------------

class WebhookRepo:
    def __init__(self, job):
        self._job = job
        self.transitions, self.events = [], []

    def job(self, connection, job_id, for_update=False):
        return self._job if str(job_id) == str(self._job["job_id"]) else None

    def transition(self, connection, job_id, *, expected, status, **fields):
        if self._job["status"] not in ((expected,) if isinstance(expected, str) else expected):
            return False
        self._job = {**self._job, "status": status}
        self.transitions.append(status)
        return True

    def event(self, *args):
        self.events.append(args)


class VerifyingProvider:
    def __init__(self, status):
        self.status, self.calls = status, 0

    def get_status(self, render_id):
        self.calls += 1
        return self.status


JOB_ID = "11111111-2222-3333-4444-555555555555"
SECRET = "webhook-secret"


def webhook_job(**overrides):
    return {"job_id": JOB_ID, "status": "RENDERING", "provider_render_id": "r-1",
            "attempt_count": 0, "max_attempts": 6, "actual_media_start_seconds": 0,
            "actual_media_end_seconds": 142, "render_policy": RenderPolicy().as_dict(),
            "submitted_at": datetime.now(UTC), **overrides}


def call(repo, provider, **overrides):
    values = dict(job_id=JOB_ID, token=webhook_token(SECRET, JOB_ID),
                  payload={"id": "r-1", "status": "succeeded"})
    values.update(overrides)
    return handle_creatomate_webhook(None, media_repository=repo, provider=provider,
                                     secret=SECRET, **values)


SUCCESS = RenderStatus("r-1", SUCCEEDED, "succeeded", "https://cdn.creatomate.com/r.mp4",
                       142.0, 1000, 1280, 720)


def test_a_verified_success_queues_the_sharepoint_transfer_only():
    repo, provider = WebhookRepo(webhook_job()), VerifyingProvider(SUCCESS)
    assert call(repo, provider)["result"] == "UPLOAD_PENDING"
    assert repo.transitions == ["UPLOAD_PENDING"] and provider.calls == 1


def test_a_duplicate_webhook_is_a_no_op():
    repo, provider = WebhookRepo(webhook_job()), VerifyingProvider(SUCCESS)
    call(repo, provider)
    again = call(repo, provider)
    assert again["result"] == "ALREADY_PROCESSED" and repo.transitions == ["UPLOAD_PENDING"]
    assert provider.calls == 1


@pytest.mark.parametrize("overrides, code", [
    ({"token": "forged"}, "WEBHOOK_TOKEN_INVALID"),
    ({"token": None}, "WEBHOOK_TOKEN_INVALID"),
    ({"payload": {"id": "someone-elses-render", "status": "succeeded"}},
     "WEBHOOK_RENDER_MISMATCH"),
])
def test_a_spoofed_webhook_cannot_complete_a_job(overrides, code):
    repo, provider = WebhookRepo(webhook_job()), VerifyingProvider(SUCCESS)
    with pytest.raises(WebhookRefused) as exc:
        call(repo, provider, **overrides)
    assert exc.value.code == code
    assert repo.transitions == [] and provider.calls == 0


def test_a_payload_claiming_success_is_overruled_by_the_verified_status():
    rendering = RenderStatus("r-1", "RENDERING", "rendering")
    repo, provider = WebhookRepo(webhook_job()), VerifyingProvider(rendering)
    assert call(repo, provider)["result"] == "RENDERING"
    assert repo._job["status"] == "RENDERING"


def test_an_unknown_job_is_refused():
    other = "99999999-2222-3333-4444-555555555555"
    with pytest.raises(WebhookRefused) as exc:
        call(WebhookRepo(webhook_job()), VerifyingProvider(SUCCESS), job_id=other,
             token=webhook_token(SECRET, other))
    assert exc.value.code == "WEBHOOK_JOB_UNKNOWN"


# ---------------------------------------------------------------------------
# dashboard: every counter is clickable
# ---------------------------------------------------------------------------

def test_counter_filters_select_exactly_the_matching_lectures():
    rows = {
        "ready": ({"transcript_state": "READY", "recording_state": "READY",
                   "analysis_state": "MOMENTS_FOUND"}, {"READY_TO_RENDER": {"count": 2}}),
        "waiting": ({"transcript_state": "READY", "recording_state": "WAITING_FOR_RECORDING",
                     "analysis_state": "NOT_ANALYZED"}, {}),
        "done": ({"transcript_state": "READY", "recording_state": "READY",
                  "analysis_state": "MOMENTS_FOUND"}, {"COMPLETED": {"count": 3}}),
        "broken": ({"transcript_state": "READY", "recording_state": "READY",
                    "analysis_state": "FAILED", "needs_review": False},
                   {"FAILED_FINAL": {"count": 1}}),
        "none": ({"transcript_state": "READY", "recording_state": "READY",
                  "analysis_state": "NO_POSITIVE_MOMENTS"}, {}),
        "fresh": ({}, {}),
    }
    filters = {name: row_filters(row, jobs, analyzing=False) for name, (row, jobs) in rows.items()}
    select = lambda key: {name for name, keys in filters.items() if key in keys}  # noqa: E731
    assert select("all") == set(rows)
    assert select("ready_to_render") == {"ready"}
    assert select("waiting_for_recording") == {"waiting"}
    assert select("not_analyzed") == {"waiting"}
    assert select("clips_completed") == {"done"}
    assert select("failed") == {"broken"} and "broken" in select("needs_review")
    assert select("no_positive_moments") == {"none"}
    assert select("not_previewed") == {"fresh"}
    assert {key for key, _ in COUNTERS} >= {key for keys in filters.values() for key in keys}


def test_media_state_reports_the_most_urgent_clip_state():
    assert media_state({"COMPLETED": {"count": 2}, "FAILED_RETRYABLE": {"count": 1}}) == "FAILED"
    assert media_state({"COMPLETED": {"count": 2}}) == "COMPLETED"
    assert media_state({"READY_TO_RENDER": {"count": 1}, "COMPLETED": {"count": 1}}) == \
        "READY_TO_RENDER"
    assert media_state({}) == "NO_MEDIA"


# ---------------------------------------------------------------------------
# configuration and secrets
# ---------------------------------------------------------------------------

def test_media_settings_default_safely_and_reuse_the_existing_destination():
    from app.config.settings import Settings
    settings = Settings(database_url="", aptem_database_url="", graph_tenant_id="",
                        graph_client_id="", graph_client_secret="", graph_scope="",
                        graph_base_url="", calendar_user_upn="")
    drive, folder = settings.media_destination()
    destination = json.loads(open("automation/positive_clips/destination.json").read())
    assert (drive, folder) == (destination["destination_drive_id"],
                               destination["destination_folder_item_id"])
    readiness = settings.media_readiness()
    assert readiness["render_provider_configured"] is False
    assert readiness["sharepoint_destination_configured"] is True
    assert "creatomate_api_key" not in json.dumps(readiness)
    assert settings.media_settings_problems() == []


def test_the_creatomate_key_is_never_a_frontend_variable():
    import pathlib
    for path in list(pathlib.Path("frontend/src").rglob("*.ts")) + list(
            pathlib.Path("frontend/src").rglob("*.vue")) + [pathlib.Path("frontend/.env.example")]:
        text = path.read_text(encoding="utf-8")
        for forbidden in ("CREATOMATE_API_KEY", "VITE_CREATOMATE", "api.creatomate.com"):
            assert forbidden not in text, (path, forbidden)


def test_no_secret_or_temporary_url_can_reach_the_production_bundle():
    """Scans the built bundle when one exists (CI builds it before this gate)."""
    import pathlib
    bundles = list(pathlib.Path("frontend/dist/assets").glob("*.js"))
    if not bundles:
        pytest.skip("frontend not built in this checkout")
    for bundle in bundles:
        text = bundle.read_text(encoding="utf-8", errors="ignore")
        for forbidden in ("CREATOMATE_API_KEY", "api.creatomate.com", "ck_live",
                          "@microsoft.graph.downloadUrl", "tempauth", "MICROSOFT_GRAPH_CLIENT",
                          "QA_MODEL_API_KEY", "MEDIA_WEBHOOK_SECRET"):
            assert forbidden not in text, (bundle.name, forbidden)
