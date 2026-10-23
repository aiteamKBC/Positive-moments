"""
Local, persistent pilot state for the Bunny integration - a STAND-IN for
migration 024's tables until that migration is approved, never a replacement.

Sections mirror the proposed tables one-to-one so promotion is a copy:
  videos            -> bunny_videos             (key: bunny_video_record_id)
  captions          -> bunny_caption_publications (key: publication_id)
  current_captions  -> bunny_caption_current    (key: "lib|video|lang")
plus `runs` (preflight/run summaries) and `events` (append-only audit).

Every save is atomic (temp file + fsync + os.replace), so a crash leaves the
previous complete state. A value that looks like a URL is refused outright:
the temporary Graph download URL, the CDN and API endpoints must never land on
disk. Keys and tokens are never passed in.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "kbc_bunny_pilot_manifest_v1"
_URLISH = re.compile(r"(https?://|tempauth|sig=|accesskey\s*[:=])", re.IGNORECASE)


class ManifestError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _assert_clean(value, where: str = "manifest") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_clean(key, where)
            _assert_clean(item, f"{where}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_clean(item, f"{where}[{index}]")
    elif isinstance(value, str) and _URLISH.search(value):
        raise ManifestError(f"refusing to persist a URL/credential-like value at {where}")


class PilotManifest:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"schema": SCHEMA, "created_at": _now(), "videos": {}, "captions": {},
                    "current_captions": {}, "runs": [], "events": []}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("schema") != SCHEMA:
            raise ManifestError(f"unexpected manifest schema {data.get('schema')!r}")
        return data

    def save(self) -> None:
        self.data["updated_at"] = _now()
        _assert_clean(self.data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(prefix=".manifest-", dir=str(self.path.parent))
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as sink:
                json.dump(self.data, sink, indent=2, sort_keys=True, default=str)
                sink.flush()
                os.fsync(sink.fileno())
            os.replace(temp, self.path)
        except BaseException:
            if os.path.exists(temp):
                os.unlink(temp)
            raise

    # -- videos -------------------------------------------------------------------
    def video(self, record_id) -> dict | None:
        return self.data["videos"].get(str(record_id))

    def videos(self) -> list[dict]:
        return list(self.data["videos"].values())

    def put_video(self, record: dict, *, event: str) -> dict:
        record = {**record, "updated_at": _now()}
        record.setdefault("created_at", record["updated_at"])
        _assert_clean(record, "video")
        self.data["videos"][str(record["bunny_video_record_id"])] = record
        self.event(event, video=str(record["bunny_video_record_id"]),
                   import_status=record.get("import_status"),
                   encoding_status=record.get("encoding_status"))
        self.save()
        return record

    # -- captions -----------------------------------------------------------------
    def caption(self, publication_id) -> dict | None:
        return self.data["captions"].get(str(publication_id))

    def put_caption(self, record: dict, *, event: str) -> dict:
        record = {**record, "updated_at": _now()}
        record.setdefault("created_at", record["updated_at"])
        _assert_clean(record, "caption")
        self.data["captions"][str(record["publication_id"])] = record
        self.event(event, publication=str(record["publication_id"]),
                   publication_status=record.get("publication_status"))
        self.save()
        return record

    @staticmethod
    def current_key(library_id, video_id, language) -> str:
        return f"{int(library_id)}|{str(video_id).lower()}|{language}"

    def current_caption(self, library_id, video_id, language) -> dict | None:
        return self.data["current_captions"].get(self.current_key(library_id, video_id, language))

    def set_current_caption(self, library_id, video_id, language, publication_id) -> None:
        publication = self.caption(publication_id)
        if not publication or publication.get("publication_status") not in ("PUBLISHED", "VERIFIED"):
            raise ManifestError("only a PUBLISHED/VERIFIED publication can become current")
        if (int(publication["bunny_library_id"]), str(publication["bunny_video_id"]).lower(),
                publication["language_code"]) != (int(library_id), str(video_id).lower(), language):
            raise ManifestError("current pointer must name a publication of the same video and language")
        self.data["current_captions"][self.current_key(library_id, video_id, language)] = {
            "bunny_library_id": int(library_id), "bunny_video_id": str(video_id).lower(),
            "language_code": language, "publication_id": str(publication_id),
            "made_current_at": _now()}
        self.event("CAPTION_MADE_CURRENT", publication=str(publication_id))
        self.save()

    # -- audit --------------------------------------------------------------------
    def event(self, name: str, **detail) -> None:
        entry = {"at": _now(), "event": name, **detail}
        _assert_clean(entry, "event")
        self.data["events"].append(entry)

    def record_run(self, summary: dict) -> None:
        _assert_clean(summary, "run")
        self.data["runs"].append({"at": _now(), **summary})
        self.save()
