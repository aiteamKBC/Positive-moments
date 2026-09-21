import json
import logging
import sys
from datetime import datetime, timezone


SENSITIVE_KEYS = {"access_token", "authorization", "client_secret", "database_url", "password"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"timestamp": datetime.now(timezone.utc).isoformat(), "level": record.levelname, "message": record.getMessage()}
        fields = getattr(record, "fields", {})
        if isinstance(fields, dict):
            payload.update({key: value for key, value in fields.items() if key.casefold() not in SENSITIVE_KEYS})
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

