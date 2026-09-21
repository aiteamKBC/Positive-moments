"""
Effective attendance roster and its provenance fingerprint.

`public.kbc_attendance` is a live, externally owned table. If an old speaker
resolution simply re-read it, a later ingestion change would silently
reinterpret historical matching evidence. So the roster that was actually used
is frozen into a snapshot, keyed by a deterministic fingerprint of its own
contents: a changed roster produces a NEW snapshot rather than an edit.

Bot filtering and deduplication are ported from the legacy `Aggregate
Attendees` node. No pattern was invented and none was removed.
"""
import hashlib
import re
import unicodedata


# Historical rule, kept reproducible: the legacy contract with no makeup rule.
ATTENDANCE_ROSTER_V1 = "attendance_roster_legacy_v1"
# Approved KBC rule: engagement measures participation in the ORIGINAL lecture,
# so a row the source explicitly marks as makeup is not part of that roster.
ATTENDANCE_ROSTER_V2 = "attendance_roster_v2_exclude_makeup"
# The version new runs use.
ATTENDANCE_RESOLUTION_VERSION = ATTENDANCE_ROSTER_V2
BOT_FILTER_VERSION = "legacy_aggregate_attendees_v6"

# The authoritative marker. public.kbc_attendance constrains attendance_status
# to ('original', 'makeup', 'absent') or NULL, so an exact comparison is safe.
# Nothing else - activity, timestamps, speech, names, learner-id repeats - is
# ever treated as evidence of makeup.
MAKEUP_STATUS = "makeup"

ROSTER_RULES = {
    ATTENDANCE_ROSTER_V1: {"exclude_makeup": False},
    ATTENDANCE_ROSTER_V2: {"exclude_makeup": True},
}


def roster_rule(version: str) -> dict:
    """The rule a roster version stands for. Unknown versions are refused."""
    try:
        return ROSTER_RULES[version]
    except KeyError:
        raise ValueError(f"unknown attendance roster version: {version}") from None


def is_makeup(row) -> bool:
    """True only when the source record itself says makeup."""
    return row.get("attendance_status") == MAKEUP_STATUS

# Exactly the legacy `isBot` pattern list, in legacy order. Substring test,
# case-insensitive, against the name OR the email. Nothing added.
BOT_PATTERNS = (
    "notetaker", "otter.ai", "otter ai", "read.ai", "read ai",
    "fireflies", "fathom", "tldv", "meeting bot", "meeting notes",
    "transcript bot", "recording bot", "zoom ai", "copilot",
    "teamsmaestro", "teams maestro",
)

_WHITESPACE = re.compile(r"\s+")


def is_bot(name: str | None, email: str | None) -> bool:
    """Legacy `isBot(name, email)`: substring hit on either field, lowercased."""
    lowered_name = str(name or "").lower()
    lowered_email = str(email or "").lower()
    return any(pattern in lowered_name or pattern in lowered_email
               for pattern in BOT_PATTERNS)


def dedup_key(name: str | None, email: str | None) -> str:
    """
    Legacy dedup key semantics: lowercased trimmed name, then the email.

    Legacy built `name.toLowerCase().trim() + "|" + email.toLowerCase().trim()`.
    The one deliberate deviation is that the email half is stored as its
    SHA-256 rather than in clear: the key is persisted on every snapshot
    member, and equality - the only property dedup needs - is identical either
    way, so there is no reason to keep contact data in a derived table.

    A missing email deterministically becomes the empty string, exactly as the
    legacy `String(j.Email || '')` did. Two rows with the same name and no
    email therefore merge; two rows with the same name and DIFFERENT emails
    stay separate, which is the behaviour that keeps distinct people apart.
    """
    return f"{str(name or '').lower().strip()}|{email_fingerprint(email) or ''}"


def normalize_person_name(value: str | None) -> str:
    """Conservative form for exact-normalized matching: NFKC, collapse, trim, casefold."""
    folded = unicodedata.normalize("NFKC", str(value or ""))
    return _WHITESPACE.sub(" ", folded).strip().casefold()


def normalize_email(value: str | None) -> str:
    return str(value or "").strip().casefold()


def email_fingerprint(value: str | None) -> str | None:
    """
    SHA-256 of the normalized email, or None when absent.

    The address itself is never persisted: dedup and provenance only need
    equality and change-detection, and a hash gives both without storing
    personal contact data in a derived table.
    """
    normalized = normalize_email(value)
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def build_roster(source_rows, *, version: str = ATTENDANCE_ROSTER_V1) -> dict:
    """
    Apply a versioned roster rule to raw attendance rows and return the
    effective roster plus the counts needed for the audit.

    Rows arrive already filtered by date, module and Attendance = 1 in SQL,
    matching the legacy subflow contract. Then, in order:

      v2 only: drop rows explicitly marked makeup
      drop blank names, drop bots, dedup first-occurrence-wins (legacy order)

    v1 ignores attendance_status completely, so historical v1 snapshots stay
    reproducible from the same source rows.
    """
    rule = roster_rule(version)
    members: dict[str, dict] = {}
    dropped_makeup = 0
    dropped_no_name = 0
    dropped_bot = 0

    for row in source_rows:
        if rule["exclude_makeup"] and is_makeup(row):
            dropped_makeup += 1
            continue
        name = row.get("full_name")
        if not str(name or "").strip():
            dropped_no_name += 1
            continue
        if is_bot(name, row.get("email")):
            dropped_bot += 1
            continue
        key = dedup_key(name, row.get("email"))
        if key in members:
            continue
        members[key] = {
            "dedup_key": key,
            # kbc_attendance."ID": a per-learner identifier that repeats across
            # dates (the row key is a separate column). Taken from the source
            # row as-is, never inferred from a name.
            "external_person_id": (str(row["learner_id"])
                                   if row.get("learner_id") is not None else None),
            # Required verbatim: the legacy comparator operates on it.
            "display_name_raw": str(name),
            "display_name_normalized": normalize_person_name(name),
            "email_sha256": email_fingerprint(row.get("email")),
            "attendance_flag": row.get("attendance_flag"),
        }

    present_rows = len(source_rows) - dropped_makeup - dropped_no_name
    learner_ids = [member["external_person_id"] for member in members.values()
                   if member["external_person_id"]]
    return {
        "roster_version": version,
        "members": list(members.values()),
        "source_row_count": len(source_rows),
        # "Present" here means: reached the bot filter with a usable name and,
        # under v2, not marked makeup.
        "present_row_count": present_rows,
        "excluded_makeup_count": dropped_makeup,
        "excluded_bot_count": dropped_bot,
        "excluded_no_name_count": dropped_no_name,
        # Diagnostic only. Legacy dedup is name|email, so the same learner id
        # under two different names stays two members; this makes that visible.
        "duplicate_learner_id_count": len(learner_ids) - len(set(learner_ids)),
        # How many rows the dedup step collapsed away.
        "deduplicated_count": max(present_rows - dropped_bot - len(members), 0),
        "effective_member_count": len(members),
    }


def roster_fingerprint(*, session_date, module_normalized: str, members,
                       resolution_version: str = ATTENDANCE_RESOLUTION_VERSION) -> str:
    """
    Deterministic SHA-256 of the exact effective roster.

    Ingredients, in this order, newline-joined:

      1. the attendance resolution version
      2. the lecture business date (ISO)
      3. the normalized module used for the query
      4. one line per effective member, each being
         `external_person_id | normalized name | email hash`,
         SORTED by that triple

    Database row order is deliberately excluded - the member lines are sorted
    before hashing - so an unchanged roster always yields the same fingerprint
    regardless of how PostgreSQL returned it. Any change to a member's external
    id, name or email changes a line and therefore the fingerprint, which
    forces a new snapshot instead of a silent reinterpretation.
    """
    lines = sorted(
        "{}|{}|{}".format(member.get("external_person_id") or "",
                          member.get("display_name_normalized") or "",
                          member.get("email_sha256") or "")
        for member in members
    )
    payload = "\n".join([
        f"version:{resolution_version.strip()}",
        f"date:{session_date.isoformat() if hasattr(session_date, 'isoformat') else session_date}",
        f"module:{module_normalized}",
        *lines,
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
