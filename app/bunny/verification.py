"""
Bunny encoded-video verification: the pilot's rules, as one pure function.

Used by the single-video pilot (app/bunny/pilot.py) and the continuous
publisher (app/publisher/bunny_jobs.py), so both verify a finished encode
identically:

  * Bunny's measured length is the source recording's measured duration
    within tolerance (a truncated fetch fails here);
  * an H.264 output exists;
  * every configured resolution up to the source height is available;
  * the library encodes on the Free tier (never Premium / JIT);
  * Bunny reported no transcoding error.
"""
from __future__ import annotations

FREE_TIERS = (0, "0", "Free", "free")
REQUIRED = ("length_within_tolerance", "h264_present", "expected_resolutions_present", "encoding_tier_free")


def media_checks(video: dict, resolutions: dict, storage: dict | None, *, source_duration_seconds: float,
                 source_height: int | None, encoding_tier, duration_tolerance_seconds: float) -> tuple[dict, list]:
    """(checks, failed): `failed` names the required checks that did not pass."""
    available = [r.strip() for r in str(video.get("availableResolutions") or "").split(",") if r.strip()]
    configured = list((resolutions or {}).get("configuredResolutions") or [])
    expected = [r for r in configured if r.endswith("p") and r[:-1].isdigit()
                and (not source_height or int(r[:-1]) <= int(source_height))]
    messages = video.get("transcodingMessages") or []
    checks = {
        "length_seconds": video.get("length"), "source_duration_seconds": source_duration_seconds,
        "length_within_tolerance": video.get("length") is not None
            and abs(float(video["length"]) - float(source_duration_seconds)) <= duration_tolerance_seconds,
        "output_codecs": video.get("outputCodecs"),
        "h264_present": "x264" in str(video.get("outputCodecs") or "").lower(),
        "available_resolutions": available, "configured_resolutions": configured,
        "expected_resolutions_present": bool(expected) and set(expected) <= set(available),
        "encoding_tier": encoding_tier, "encoding_tier_free": encoding_tier in FREE_TIERS,
        "jit_encoding_enabled": video.get("jitEncodingEnabled"),
        "transcoding_errors": sum(1 for m in messages if m.get("level") == 3),
        "transcoding_warnings": sum(1 for m in messages if m.get("level") == 2),
        "captions_present_before_publication": [c.get("srclang") for c in (video.get("captions") or [])],
        "smart_generate_status": video.get("smartGenerateStatus"),
        "width": video.get("width"), "height": video.get("height"),
        "storage_size_bytes": video.get("storageSize"),
        "storage_breakdown": {k: storage.get(k) for k in ("originals", "thumbnails", "previews",
                                                          "mp4Fallback", "miscellaneous")} if storage else None,
    }
    failed = [name for name in REQUIRED if not checks[name]]
    if checks["transcoding_errors"]:
        failed.append("transcoding_errors")
    return checks, failed
