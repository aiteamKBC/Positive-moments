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

    @property
    def recording_links_writable(self) -> bool:
        return self.recording_link_mode == "write"

    @classmethod
    def from_environment(cls) -> "Settings":
        load_dotenv(ROOT / "backend" / ".env", override=False)
        return cls(
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
        )

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
