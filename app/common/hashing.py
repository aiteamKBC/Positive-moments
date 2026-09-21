import hashlib
import uuid


LECTURE_NAMESPACE = uuid.UUID("0bf767fa-e7dd-4b11-b313-7e51e1da6f2d")
TRANSCRIPT_ARTIFACT_NAMESPACE = uuid.UUID("4f2a1c83-9d6e-4a70-8b52-1c0f3d7e9a41")
TRANSCRIPT_SELECTION_NAMESPACE = uuid.UUID("9e3b52d1-6c84-4f0a-bd27-5a1e8f4c63b9")


def lecture_identity(*, calendar_user_upn: str, calendar_event_id: str, i_cal_uid: str | None) -> uuid.UUID:
    """Return a stable calendar-occurrence identity independent of QA artifacts."""
    owner = calendar_user_upn.strip().casefold()
    stable_event = (i_cal_uid or "").strip()
    if stable_event:
        identity = f"microsoft_graph_calendar\0{owner}\0ical:{stable_event}"
    elif calendar_event_id.strip():
        identity = f"microsoft_graph_calendar\0{owner}\0event:{calendar_event_id.strip()}"
    else:
        raise ValueError("calendar_event_id or i_cal_uid is required")
    return uuid.uuid5(LECTURE_NAMESPACE, identity)


def provider_transcript_identity(*, provider: str, provider_transcript_id: str) -> uuid.UUID:
    """
    Physical identity of ONE raw provider transcript artifact.

    Deliberately excludes the lecture. A Teams recurring meeting exposes the
    same transcript to every occurrence that can see it, so putting the lecture
    in the physical key would store the same provider bytes once per lecture.
    The lecture relationship lives in lecture_transcript_candidates instead.
    """
    transcript = provider_transcript_id.strip()
    if not transcript:
        raise ValueError("provider_transcript_id is required")
    identity = f"{provider.strip().casefold()}\0transcript:{transcript}"
    return uuid.uuid5(TRANSCRIPT_ARTIFACT_NAMESPACE, identity)


def legacy_lecture_scoped_artifact_identity(*, provider: str, provider_transcript_id: str,
                                            lecture_id) -> uuid.UUID:
    """
    SUPERSEDED Phase 2A identity that scoped an artifact to the lecture it was
    discovered through.

    Retained only so the Phase 2B normalization can map historical rows onto
    their provider-scoped identity. New code must use
    `provider_transcript_identity`.
    """
    transcript = provider_transcript_id.strip()
    if not transcript:
        raise ValueError("provider_transcript_id is required")
    identity = f"{provider.strip().casefold()}\0transcript:{transcript}\0lecture:{lecture_id}"
    return uuid.uuid5(TRANSCRIPT_ARTIFACT_NAMESPACE, identity)


def selection_identity(*, lecture_id, selection_version: str) -> uuid.UUID:
    """One current selection per lecture per algorithm version."""
    return uuid.uuid5(
        TRANSCRIPT_SELECTION_NAMESPACE,
        f"lecture:{lecture_id}\0version:{selection_version.strip()}",
    )


def content_sha256(raw: bytes) -> str:
    """Hex SHA-256 of raw provider bytes, exactly as received."""
    return hashlib.sha256(raw).hexdigest()


# --- Phase 2C3: attendance resolution and deterministic roles ---------------
ATTENDANCE_SNAPSHOT_NAMESPACE = uuid.UUID("2d9c4b71-8f35-4e62-9a0d-6b17c5e83f4a")
SPEAKER_RESOLUTION_NAMESPACE = uuid.UUID("7a1e6d90-c254-4b83-91f7-3e0b8c2a5d16")
SPEAKER_ROLE_NAMESPACE = uuid.UUID("bf35082c-41a7-4d96-8e51-9c60d7f2a4b8")


def attendance_snapshot_identity(*, lecture_id, attendance_resolution_version: str,
                                 source_fingerprint: str) -> uuid.UUID:
    """
    Identity of ONE frozen attendance roster for one lecture.

    The roster fingerprint is part of the key, so a changed attendance source
    produces a NEW snapshot instead of overwriting the roster an earlier
    resolution was actually made against.
    """
    if not str(source_fingerprint or "").strip():
        raise ValueError("source_fingerprint is required")
    return uuid.uuid5(
        ATTENDANCE_SNAPSHOT_NAMESPACE,
        "lecture:{}\0version:{}\0fingerprint:{}".format(
            lecture_id, attendance_resolution_version.strip(), source_fingerprint.strip()),
    )


def attendance_member_identity(*, snapshot_id, dedup_key: str) -> uuid.UUID:
    """One member inside one snapshot, keyed by the legacy name|email dedup key."""
    return uuid.uuid5(ATTENDANCE_SNAPSHOT_NAMESPACE,
                      "snapshot:{}\0member:{}".format(snapshot_id, dedup_key))


def speaker_resolution_identity(*, speaker_id, attendance_snapshot_id,
                                resolver_version: str) -> uuid.UUID:
    """
    One person-resolution decision.

    Keyed by snapshot AND resolver version, so a new matching algorithm or a
    changed roster adds a decision rather than rewriting an older one.
    """
    return uuid.uuid5(
        SPEAKER_RESOLUTION_NAMESPACE,
        "speaker:{}\0snapshot:{}\0resolver:{}".format(
            speaker_id, attendance_snapshot_id, resolver_version.strip()),
    )


def speaker_role_identity(*, speaker_id, role_algorithm_version: str,
                          resolver_version: str, attendance_snapshot_id) -> uuid.UUID:
    """
    One role decision.

    The resolver version and snapshot are part of the key even though the
    trainer rule does not consult them, because the LEARNER rule does. Without
    them, re-resolving with a new matching algorithm would silently rewrite
    previously stored role evidence under the same role-algorithm version.
    """
    return uuid.uuid5(
        SPEAKER_ROLE_NAMESPACE,
        "speaker:{}\0role_version:{}\0resolver:{}\0snapshot:{}".format(
            speaker_id, role_algorithm_version.strip(), resolver_version.strip(),
            attendance_snapshot_id),
    )


# --- Phase 2C4: deterministic engagement ------------------------------------
ENGAGEMENT_NAMESPACE = uuid.UUID("5e8a2f47-d619-4c3b-a07e-8b24f6c1d935")


def engagement_identity(*, document_id, attendance_snapshot_id, resolver_version: str,
                        role_algorithm_version: str, engagement_algorithm_version: str,
                        source_fingerprint: str) -> uuid.UUID:
    """
    One engagement result.

    Every input version and the input fingerprint are in the key, so a changed
    snapshot, resolver, role algorithm, engagement algorithm or underlying
    evidence adds a new result instead of rewriting a historical one, while an
    unchanged rerun lands on the same row.
    """
    if not str(source_fingerprint or "").strip():
        raise ValueError("source_fingerprint is required")
    return uuid.uuid5(
        ENGAGEMENT_NAMESPACE,
        "document:{}\0snapshot:{}\0resolver:{}\0role:{}\0engagement:{}\0fingerprint:{}".format(
            document_id, attendance_snapshot_id, resolver_version.strip(),
            role_algorithm_version.strip(), engagement_algorithm_version.strip(),
            source_fingerprint.strip()),
    )


def engagement_participant_identity(*, engagement_id, member_id) -> uuid.UUID:
    """One snapshot member inside one engagement result."""
    return uuid.uuid5(ENGAGEMENT_NAMESPACE,
                      "engagement:{}\0member:{}".format(engagement_id, member_id))
