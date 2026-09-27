"""
The rendering boundary.

Positive Moment analysis, clip planning, job history, SharePoint delivery and
the UI never talk to a vendor. They build a RenderRequest and read a
RenderStatus. A later FFmpegMediaRenderProvider or DescriptMediaRenderProvider
implements the same three methods; nothing above this module changes.

The render POLICY (size, frame rate, format) lives here, in one place, so
visual enhancements later are a policy change, not a code hunt. This release
is accurate evidence clipping only: no captions, animation, branding,
transitions or effects.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol

RENDER_POLICY_VERSION = "positive_moment_render_policy_v1"

# Provider-neutral render states.
QUEUED = "QUEUED"
RENDERING = "RENDERING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"

# Duration of the output may differ from the request by a frame or two.
DURATION_TOLERANCE_SECONDS = 1.5


@dataclass(frozen=True)
class RenderPolicy:
    output_format: str = "mp4"
    max_width: int = 1280
    max_height: int = 720
    frame_rate: int = 25
    version: str = RENDER_POLICY_VERSION

    def as_dict(self) -> dict:
        return asdict(self)


def estimate_credits(duration_seconds: float, policy: RenderPolicy) -> float:
    """
    ESTIMATE of provider credits for one clip.

    Creatomate's published rule: one credit per 100 million output pixels
    (width x height x frame rate x seconds), minimum one credit per render.
    """
    pixels = policy.max_width * policy.max_height * policy.frame_rate * max(duration_seconds, 0)
    return round(max(1.0, pixels / 100_000_000), 3)


@dataclass(frozen=True)
class RenderRequest:
    job_id: str
    source_url: str              # fresh, temporary; never persisted or logged
    trim_start_seconds: float
    trim_duration_seconds: float
    policy: RenderPolicy
    webhook_url: str | None = None

    def __repr__(self) -> str:   # never print the temporary URL
        return (f"RenderRequest(job_id={self.job_id!r}, trim_start={self.trim_start_seconds},"
                f" trim_duration={self.trim_duration_seconds})")


@dataclass(frozen=True)
class RenderStatus:
    render_id: str
    status: str                  # QUEUED / RENDERING / SUCCEEDED / FAILED
    provider_status: str
    output_url: str | None = None   # temporary provider URL; never persisted
    duration_seconds: float | None = None
    size_bytes: int | None = None
    width: int | None = None
    height: int | None = None
    error_message: str | None = None

    def __repr__(self) -> str:
        return (f"RenderStatus(render_id={self.render_id!r}, status={self.status!r}, "
                f"duration={self.duration_seconds}, size={self.size_bytes})")


class RenderProviderError(Exception):
    """A provider failure. Carries a safe code and HTTP status, never a key or URL."""

    def __init__(self, code: str, message: str, *, http_status=None, retryable=True):
        self.code = code
        self.http_status = http_status
        self.retryable = retryable
        super().__init__(message)


class MediaRenderProvider(Protocol):
    name: str

    def submit(self, request: RenderRequest) -> RenderStatus: ...

    def get_status(self, render_id: str) -> RenderStatus: ...

    def validate_result(self, status: RenderStatus, *, expected_duration_seconds: float,
                        policy: RenderPolicy) -> list: ...


def validate_output(status: RenderStatus, *, expected_duration_seconds: float,
                    policy: RenderPolicy) -> list:
    """Problems with a SUCCEEDED render; empty means it is what was asked for."""
    problems = []
    if status.status != SUCCEEDED:
        problems.append("RENDER_NOT_SUCCEEDED")
    if not status.output_url or not str(status.output_url).startswith("https://"):
        problems.append("RENDER_OUTPUT_URL_MISSING")
    if status.duration_seconds is not None and abs(
            status.duration_seconds - expected_duration_seconds) > DURATION_TOLERANCE_SECONDS:
        problems.append("RENDER_DURATION_MISMATCH")
    if status.width and status.width > policy.max_width:
        problems.append("RENDER_WIDTH_EXCEEDS_POLICY")
    if status.height and status.height > policy.max_height:
        problems.append("RENDER_HEIGHT_EXCEEDS_POLICY")
    if status.size_bytes is not None and status.size_bytes <= 0:
        problems.append("RENDER_EMPTY_FILE")
    return problems
