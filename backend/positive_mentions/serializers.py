from rest_framework import serializers

from .models import DoctorSession
from .services import ready_clip_assets
from .utils import encode_session_id, normalize_clips


def _same_source_range(clip, asset):
    return (
        str(clip.get("start") or "").strip() == asset.source_start.strip()
        and str(clip.get("end") or "").strip() == asset.source_end.strip()
    )


def _cue_key(session_id, clip):
    start_cue = clip.get("start_cue")
    end_cue = clip.get("end_cue")
    if start_cue is None or end_cue is None:
        return None
    return f"{session_id}:{start_cue}:{end_cue}"


def _asset_payload(asset):
    return {
        "id": asset.clip_key,
        "clip_index": asset.clip_index,
        "filename": asset.clip_filename,
        "url": asset.clip_url.strip(),
        "status": asset.trim_status,
        "source_start": asset.source_start,
        "source_end": asset.source_end,
        "duration_seconds": float(asset.duration_seconds),
        "created_at": asset.created_at,
        # No dedicated uploaded_at column exists. updated_at is written when
        # Save Clip Asset persists the successful completed upload.
        "uploaded_at": asset.updated_at,
    }


def attach_ready_clip_assets(session_id, clips):
    assets = list(
        ready_clip_assets()
        .filter(session_id=session_id)
        .order_by("clip_index", "clip_key")
    )
    used_keys = set()

    for clip in clips:
        matched = None
        original_index = clip.get("original_index")
        expected_index = original_index + 1 if isinstance(original_index, int) else None
        expected_key = _cue_key(session_id, clip)

        # clip_index is explicitly one-based against the original JSON array.
        # Cue key and exact source range are guards against attaching a wrong clip.
        index_matches = [
            asset for asset in assets
            if asset.clip_key not in used_keys
            and expected_index is not None
            and asset.clip_index == expected_index
            and _same_source_range(clip, asset)
            and (expected_key is None or asset.clip_key == expected_key)
        ]
        if len(index_matches) == 1:
            matched = index_matches[0]

        # Fallback for legacy rows with no usable clip_index parity.
        if matched is None and expected_key is not None:
            cue_matches = [
                asset for asset in assets
                if asset.clip_key not in used_keys
                and asset.clip_key == expected_key
                and _same_source_range(clip, asset)
            ]
            if len(cue_matches) == 1:
                matched = cue_matches[0]

        # Last fallback is a unique exact stored source range.
        if matched is None:
            range_matches = [
                asset for asset in assets
                if asset.clip_key not in used_keys and _same_source_range(clip, asset)
            ]
            if len(range_matches) == 1:
                matched = range_matches[0]

        if matched is None:
            clip["clip_asset"] = None
        else:
            used_keys.add(matched.clip_key)
            clip["clip_asset"] = _asset_payload(matched)

    return clips


class LectureListSerializer(serializers.ModelSerializer):
    session_key = serializers.SerializerMethodField()
    recording_available = serializers.SerializerMethodField()
    has_ready_clips = serializers.BooleanField(read_only=True)
    ready_clips_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = DoctorSession
        fields = (
            "session_id", "session_key", "subject", "trainer", "date",
            "clips_status", "positive_clips_count", "has_positive_clips",
            "recording_available", "recording_url", "recording_link_status",
            "has_ready_clips", "ready_clips_count",
        )

    def get_session_key(self, obj):
        return encode_session_id(obj.session_id)

    def get_recording_available(self, obj):
        return bool(obj.recording_url)


class LectureDetailSerializer(LectureListSerializer):
    clips = serializers.SerializerMethodField()

    class Meta(LectureListSerializer.Meta):
        fields = LectureListSerializer.Meta.fields + (
            "meeting_id", "clips_analyzed_at", "clips_analysis_completeness",
            "clips_error", "recording_filename", "recording_link_updated_at", "clips",
        )

    def get_clips(self, obj):
        return attach_ready_clip_assets(
            obj.session_id,
            normalize_clips(obj.positive_clips),
        )
