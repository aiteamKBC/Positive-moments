from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from app.qa.prompt import LEGACY_QA_MODEL


ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    database_url: str
    aptem_database_url: str
    graph_tenant_id: str
    graph_client_id: str
    graph_client_secret: str
    graph_scope: str
    graph_base_url: str
    calendar_user_upn: str
    # Optional: only an --execute shadow QA run needs these. Defaulted so
    # adding the QA engine does not change how Settings is constructed
    # anywhere else.
    qa_model_api_key: str = ""
    qa_model_name: str = LEGACY_QA_MODEL
    qa_model_base_url: str = "https://api.openai.com/v1"
    # RECORDING_LINK. "observe" (default) keeps the stage exactly as it was:
    # report the gap, call nothing, write nothing. "write" lets the scheduler
    # and backfill evaluate and link recordings through app/recordings.
    recording_link_mode: str = "observe"
    # App-only /search/query requires a region (e.g. "GBR"). Empty = the
    # tenant-search discovery source is not attempted.
    recording_link_search_region: str = ""
    # Prefer an organization-scoped view link (createLink) over the raw
    # driveItem webUrl, as the n8n branch always has.
    recording_link_create_org_links: bool = True
    # Full-recording resolution (app/recordings/resolution.py). Ratios of the
    # lecture's own scheduled length; defaults are the policy's own and need
    # no configuration. Invalid combinations refuse to start.
    recording_full_min_expected_duration_ratio: float = 0.50
    recording_short_fragment_max_expected_ratio: float = 0.25
    recording_size_dominance_ratio: float = 10.0
    # -- Positive Moments media platform ------------------------------------
    # The ONLY new secret. Backend-only: never sent to the browser, never
    # stored, never logged. Empty = rendering is unavailable (analysis and
    # planning still work).
    creatomate_api_key: str = ""
    media_render_provider: str = "creatomate"
    media_render_max_width: int = 1280
    media_render_max_height: int = 720
    media_render_fps: int = 25
    positive_moment_padding_before_seconds: float = 60.0
    positive_moment_padding_after_seconds: float = 60.0
    media_runner_concurrency: int = 1
    # SharePoint destination. Empty -> automation/positive_clips/destination.json,
    # the Positive Clips folder already in use.
    media_sharepoint_drive_id: str = ""
    media_positive_moments_folder_item_id: str = ""
    # Public base URL of this site for the Creatomate webhook, e.g.
    # https://positive-moments.example. Empty = no webhook; the runner polls.
    media_webhook_base_url: str = ""
    # HMAC key for webhook URLs. Empty -> DJANGO_SECRET_KEY.
    media_webhook_secret: str = ""
    # Model for Positive Moment analysis. Empty -> QA_MODEL_NAME.
    positive_moments_model_name: str = ""

    @property
    def recording_links_writable(self) -> bool:
        return self.recording_link_mode == "write"

    @classmethod
    def from_environment(cls) -> "Settings":
        load_dotenv(ROOT / "backend" / ".env", override=False)
        settings = cls(
            database_url=os.getenv("DATABASE_URL", "").strip(),
            aptem_database_url=os.getenv("APTEM_DATABASE_URL", "").strip(),
            graph_tenant_id=os.getenv("MICROSOFT_GRAPH_TENANT_ID", "").strip(),
            graph_client_id=os.getenv("MICROSOFT_GRAPH_CLIENT_ID", "").strip(),
            graph_client_secret=os.getenv("MICROSOFT_GRAPH_CLIENT_SECRET", "").strip(),
            graph_scope=os.getenv("MICROSOFT_GRAPH_SCOPE", "https://graph.microsoft.com/.default").strip(),
            graph_base_url=os.getenv("MICROSOFT_GRAPH_BASE_URL", "https://graph.microsoft.com/v1.0").strip(),
            calendar_user_upn=os.getenv("KBC_LECTURE_CALENDAR_USER_UPN", "").strip(),
            qa_model_api_key=os.getenv("QA_MODEL_API_KEY", "").strip(),
            # Defaults to the legacy model. Overriding it changes the prompt
            # hash and therefore the QA provenance, so parity is never
            # silently lost.
            qa_model_name=os.getenv("QA_MODEL_NAME", LEGACY_QA_MODEL).strip(),
            qa_model_base_url=os.getenv(
                "QA_MODEL_BASE_URL", "https://api.openai.com/v1").strip(),
            recording_link_mode=_recording_link_mode(os.getenv("RECORDING_LINK_MODE", "")),
            recording_link_search_region=os.getenv(
                "RECORDING_LINK_SEARCH_REGION", "").strip().upper(),
            recording_link_create_org_links=os.getenv(
                "RECORDING_LINK_CREATE_ORG_LINKS", "true").strip().lower()
                not in ("0", "false", "no", "off"),
            recording_full_min_expected_duration_ratio=_ratio(
                "RECORDING_FULL_MIN_EXPECTED_DURATION_RATIO", 0.50),
            recording_short_fragment_max_expected_ratio=_ratio(
                "RECORDING_SHORT_FRAGMENT_MAX_EXPECTED_RATIO", 0.25),
            recording_size_dominance_ratio=_ratio("RECORDING_SIZE_DOMINANCE_RATIO", 10.0),
            creatomate_api_key=os.getenv("CREATOMATE_API_KEY", "").strip(),
            media_render_provider=(os.getenv("MEDIA_RENDER_PROVIDER", "").strip().lower()
                                   or "creatomate"),
            media_render_max_width=int(_ratio("MEDIA_RENDER_MAX_WIDTH", 1280)),
            media_render_max_height=int(_ratio("MEDIA_RENDER_MAX_HEIGHT", 720)),
            media_render_fps=int(_ratio("MEDIA_RENDER_FPS", 25)),
            positive_moment_padding_before_seconds=_non_negative(
                "POSITIVE_MOMENT_PADDING_BEFORE_SECONDS", 60.0),
            positive_moment_padding_after_seconds=_non_negative(
                "POSITIVE_MOMENT_PADDING_AFTER_SECONDS", 60.0),
            media_runner_concurrency=int(_ratio("MEDIA_RUNNER_CONCURRENCY", 1)),
            media_sharepoint_drive_id=os.getenv("MEDIA_SHAREPOINT_DRIVE_ID", "").strip(),
            media_positive_moments_folder_item_id=os.getenv(
                "MEDIA_POSITIVE_MOMENTS_FOLDER_ITEM_ID", "").strip(),
            media_webhook_base_url=os.getenv("MEDIA_WEBHOOK_BASE_URL", "").strip().rstrip("/"),
            media_webhook_secret=(os.getenv("MEDIA_WEBHOOK_SECRET", "").strip()
                                  or os.getenv("DJANGO_SECRET_KEY", "").strip()),
            positive_moments_model_name=os.getenv("POSITIVE_MOMENTS_MODEL_NAME", "").strip(),
        )
        settings.recording_resolution_policy()      # an invalid combination refuses here
        settings.media_settings_problems(raise_on_error=True)
        return settings

    def recording_resolution_policy(self):
        """The full-recording resolution policy these settings describe (validated)."""
        from app.recordings.resolution import ResolutionPolicy
        return ResolutionPolicy(
            full_min_expected_ratio=self.recording_full_min_expected_duration_ratio,
            fragment_max_expected_ratio=self.recording_short_fragment_max_expected_ratio,
            size_dominance_ratio=self.recording_size_dominance_ratio)

    def media_destination(self) -> tuple[str, str]:
        """(drive id, folder item id) for delivered clips; env wins over the repo file."""
        drive = self.media_sharepoint_drive_id
        folder = self.media_positive_moments_folder_item_id
        if drive and folder:
            return drive, folder
        import json
        path = ROOT / "automation" / "positive_clips" / "destination.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        return (drive or str(data.get("destination_drive_id") or ""),
                folder or str(data.get("destination_folder_item_id") or ""))

    def media_settings_problems(self, *, raise_on_error=False) -> list[str]:
        problems = []
        if self.media_render_provider != "creatomate":
            problems.append("MEDIA_RENDER_PROVIDER must be creatomate in this release")
        if self.media_runner_concurrency != 1:
            problems.append("MEDIA_RUNNER_CONCURRENCY must be 1 in this release")
        if self.media_webhook_base_url and not self.media_webhook_base_url.startswith("https://"):
            problems.append("MEDIA_WEBHOOK_BASE_URL must be https")
        if problems and raise_on_error:
            raise ValueError("; ".join(problems))
        return problems

    def media_readiness(self) -> dict:
        """What the media platform can do with this configuration. No values."""
        drive, folder = self.media_destination()
        return {"render_provider": self.media_render_provider,
                "render_provider_configured": bool(self.creatomate_api_key),
                "sharepoint_destination_configured": bool(drive and folder),
                "webhook_configured": bool(self.media_webhook_base_url
                                           and self.media_webhook_secret),
                "analysis_model_configured": bool(self.qa_model_api_key),
                "problems": self.media_settings_problems()}

    def require_database(self) -> None:
        if not self.database_url:
            raise ValueError("DATABASE_URL is required")

    def require_aptem_database(self) -> None:
        if not self.aptem_database_url:
            raise ValueError("APTEM_DATABASE_URL is required for the read-only Aptem source role")

    def require_qa_model(self) -> None:
        """Real shadow QA calls need a key; preview mode does not."""
        self.require_database()
        if not self.qa_model_api_key:
            raise ValueError(
                "QA_MODEL_API_KEY is required to execute shadow QA model calls. "
                "Preview mode (--preview) needs no credentials.")

    def require_discovery(self) -> None:
        self.require_database()
        missing = [name for name, value in (
            ("APTEM_DATABASE_URL", self.aptem_database_url),
            ("MICROSOFT_GRAPH_TENANT_ID", self.graph_tenant_id),
            ("MICROSOFT_GRAPH_CLIENT_ID", self.graph_client_id),
            ("MICROSOFT_GRAPH_CLIENT_SECRET", self.graph_client_secret),
            ("MICROSOFT_GRAPH_SCOPE", self.graph_scope),
            ("MICROSOFT_GRAPH_BASE_URL", self.graph_base_url),
            ("KBC_LECTURE_CALENDAR_USER_UPN", self.calendar_user_upn),
        ) if not value]
        if missing:
            raise ValueError("missing discovery configuration: " + ", ".join(missing))
        if self.aptem_database_url == self.database_url:
            raise ValueError("APTEM_DATABASE_URL must identify the separate Aptem source database")
        if self.graph_scope != "https://graph.microsoft.com/.default":
            raise ValueError("MICROSOFT_GRAPH_SCOPE must be https://graph.microsoft.com/.default")


RECORDING_LINK_MODES = ("observe", "write")


def _recording_link_mode(value: str) -> str:
    """Only an exact "write" arms the stage; empty means observe; anything else is a typo."""
    mode = (value or "").strip().lower() or "observe"
    if mode not in RECORDING_LINK_MODES:
        raise ValueError(f"RECORDING_LINK_MODE must be one of {RECORDING_LINK_MODES}")
    return mode


def _ratio(name: str, default: float) -> float:
    """An optional positive number; empty means the documented default."""
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        number = float(value)
    except ValueError:
        raise ValueError(f"{name} must be a number") from None
    if not number > 0:
        raise ValueError(f"{name} must be greater than 0")
    return number


def _non_negative(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        number = float(value)
    except ValueError:
        raise ValueError(f"{name} must be a number") from None
    if number < 0:
        raise ValueError(f"{name} must not be negative")
    return number
