from rest_framework import serializers

from . import evidence, social_clip_requests
from .services import ready_clip_assets
from .models import DoctorSession, SocialMediaOpportunity
from .utils import encode_session_id, timestamp_to_seconds, timestamped_sharepoint_url

EMPTY_STATS = {"opportunity_count": 0, "format_counts": {}, "last_analyzed_at": None}


def _list(value) -> list:
    return value if isinstance(value, list) else []


def evidence_assets(session_id) -> dict:
    """
    The lecture's produced Positive Moments / OFSTED clips, keyed by BOTH of
    their identities: clip_key (session:start_cue:end_cue) and the exact source
    range. One query; the same completed-SharePoint filter the Positive Moments
    page uses.
    """
    return {(asset.clip_key, asset.source_start.strip(), asset.source_end.strip()): asset
            for asset in ready_clip_assets().filter(session_id=session_id)}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


class SocialLectureSerializer(serializers.ModelSerializer):
    session_key = serializers.SerializerMethodField()
    recording_available = serializers.SerializerMethodField()
    opportunity_count = serializers.SerializerMethodField()
    format_counts = serializers.SerializerMethodField()
    last_analyzed_at = serializers.SerializerMethodField()

    class Meta:
        model = DoctorSession
        fields = (
            "session_id", "session_key", "subject", "trainer", "date",
            "recording_available", "recording_url",
            "opportunity_count", "format_counts", "last_analyzed_at",
        )

    def _stats(self, obj):
        return self.context.get("stats", {}).get(obj.session_id, EMPTY_STATS)

    def get_session_key(self, obj):
        return encode_session_id(obj.session_id)

    def get_recording_available(self, obj):
        return bool(obj.recording_url)

    def get_opportunity_count(self, obj):
        return self._stats(obj)["opportunity_count"]

    def get_format_counts(self, obj):
        return self._stats(obj)["format_counts"]

    def get_last_analyzed_at(self, obj):
        value = self._stats(obj)["last_analyzed_at"]
        return serializers.DateTimeField().to_representation(value) if value else None


class SocialOpportunitySerializer(serializers.ModelSerializer):
    """
    One opportunity exactly as saved. `opportunity_data` is returned whole
    except for the clip bookkeeping n8n and Django add to it, which arrives as
    `clip` instead; the other fields are typed copies of what it holds, so the
    page does not have to guard every key. Nothing here is generated.
    """
    opportunity_data = serializers.SerializerMethodField()
    duration_seconds = serializers.SerializerMethodField()
    formats = serializers.SerializerMethodField()
    reel_segments = serializers.SerializerMethodField()
    reel_edit_mode = serializers.SerializerMethodField()
    reel_estimated_duration_seconds = serializers.SerializerMethodField()
    carousel_slide_outline = serializers.SerializerMethodField()
    also_in_evidence = serializers.SerializerMethodField()
    watch_url = serializers.SerializerMethodField()
    clip = serializers.SerializerMethodField()
    evidence_clip = serializers.SerializerMethodField()

    class Meta:
        model = SocialMediaOpportunity
        fields = (
            "opportunity_id", "opportunity_key", "start_cue", "end_cue",
            "start_time", "end_time", "duration_seconds", "speaker", "quote",
            "content_angle", "why_selected", "primary_format", "formats",
            "verification_status", "verification_reason", "discovery_source",
            "evidence_type", "analyzed_at", "updated_at", "opportunity_data",
            "reel_segments", "reel_edit_mode", "reel_estimated_duration_seconds",
            "carousel_slide_outline", "also_in_evidence", "watch_url", "clip", "evidence_clip",
        )

    def _data(self, obj) -> dict:
        return obj.opportunity_data if isinstance(obj.opportunity_data, dict) else {}

    def get_opportunity_data(self, obj):
        hidden = (social_clip_requests.RESULT_KEY, social_clip_requests.REQUEST_KEY)
        return {key: value for key, value in self._data(obj).items() if key not in hidden}

    def get_duration_seconds(self, obj):
        return float(obj.duration_seconds) if obj.duration_seconds is not None else None

    def get_formats(self, obj):
        return [item for item in _list(obj.formats) if isinstance(item, str)]

    def get_reel_segments(self, obj):
        """
        Cue ranges as saved. A segment's start/end time is filled in only from a
        timestamp the row already stores for that exact cue: the opportunity's
        own boundaries, or the dialogue of the evidence it came from.
        """
        data = self._data(obj)
        source = data.get("original_evidence")
        dialogue = list(_list(source.get("dialogue"))) if isinstance(source, dict) else []
        dialogue.append({"start_cue": obj.start_cue, "start": obj.start_time,
                         "end_cue": obj.end_cue, "end": obj.end_time})
        return evidence._reel_segments({
            "reel_segments": _list(data.get("reel_segments")), "dialogue": dialogue,
        }) or []

    def get_reel_edit_mode(self, obj):
        value = self._data(obj).get("reel_edit_mode")
        return value if isinstance(value, str) else None

    def get_reel_estimated_duration_seconds(self, obj):
        return _number(self._data(obj).get("reel_estimated_duration_seconds"))

    def get_carousel_slide_outline(self, obj):
        return [item for item in _list(self._data(obj).get("carousel_slide_outline"))
                if isinstance(item, str)]

    def get_also_in_evidence(self, obj):
        value = self._data(obj).get("also_in_evidence")
        return value if isinstance(value, bool) else None

    def get_watch_url(self, obj):
        """The stored recording, opened by the Stream player at this start time."""
        recording_url = (self.context.get("recording_url") or "").strip()
        seconds = timestamp_to_seconds(obj.start_time)
        if not recording_url or seconds is None:
            return None
        return timestamped_sharepoint_url(recording_url, seconds)

    def get_clip(self, obj):
        """The reel's clip state from PostgreSQL; None for non-reels."""
        lecture = self.context.get("lecture")
        return social_clip_requests.clip_state(obj, lecture) if lecture is not None else None

    def get_evidence_clip(self, obj):
        """
        The produced Positive Moments / OFSTED clip of the SAME moment, read-only.
        Matched only when both the cue key and the exact saved times agree, so a
        re-analysed lecture whose cues moved never borrows another moment's clip.
        It is the padded evidence clip, never presented as an exact reel.
        """
        key = (f"{obj.session_id}:{obj.start_cue}:{obj.end_cue}",
               (obj.start_time or "").strip(), (obj.end_time or "").strip())
        asset = self.context.get("evidence_assets", {}).get(key)
        if asset is None:
            return None
        start, end = timestamp_to_seconds(key[1]), timestamp_to_seconds(key[2])
        trim_start, trim_end = float(asset.trim_start_seconds), float(asset.trim_end_seconds)
        return {
            "url": asset.clip_url.strip(),
            "filename": asset.clip_filename,
            "duration_seconds": float(asset.duration_seconds),
            "context_before_seconds": round(start - trim_start, 3) if start is not None else None,
            "context_after_seconds": round(trim_end - end, 3) if end is not None else None,
            "evidence_type": obj.evidence_type,
        }


class SocialLectureDetailSerializer(SocialLectureSerializer):
    opportunities = serializers.SerializerMethodField()

    class Meta(SocialLectureSerializer.Meta):
        fields = SocialLectureSerializer.Meta.fields + (
            "meeting_id", "recording_filename", "recording_link_status", "opportunities",
        )

    def get_opportunities(self, obj):
        return SocialOpportunitySerializer(
            self.context["opportunities"], many=True,
            context={"recording_url": obj.recording_url, "lecture": obj,
                     "evidence_assets": evidence_assets(obj.session_id)},
        ).data
