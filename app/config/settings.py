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
