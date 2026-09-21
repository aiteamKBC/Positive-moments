"""
The versioned LMS learner snapshot.

`public.kbc_users_data` is externally owned and live. The legacy workflow read
it at QA time, so a roster that changes later would silently change an old QA
output if it were simply re-read. The roster actually used is therefore frozen
into a snapshot keyed by a fingerprint of its own contents, exactly as Phase
2C3 does for attendance.

The query semantics are ported from the legacy `Get LMS Students` node. The
table is read with SELECT only - never written, altered or triggered.
"""
import hashlib
import re


LMS_SNAPSHOT_VERSION = "legacy_qa_v8_active_lms_roster_v1"

# Legacy `LIMIT 500` sits inside the filtered CTE, i.e. it caps the matched
# rows ordered by FullName BEFORE the distinct (ID, FullName) pairs are taken.
LEGACY_ROW_CAP = 500
ACTIVE_PROGRAM_STATUS = "active"

_WHITESPACE = re.compile(r"\s+")


def normalize_module(value) -> str:
    """
    Legacy module normalization, in this order:

        replace('&amp;', '&') -> regexp_replace('\\s+', ' ') -> trim -> lower

    Applied identically to the lecture module and to kbc_users_data."Group".
    """
    replaced = str(value or "").replace("&amp;", "&")
    return _WHITESPACE.sub(" ", replaced).strip().lower()


def lms_fingerprint(*, module_normalized: str, members,
                    snapshot_version: str = LMS_SNAPSHOT_VERSION) -> str:
    """
    SHA-256 of the effective roster: version, normalized module, then one
    sorted line per member (`external_learner_id|full_name`).

    Members are sorted before hashing, so database row order cannot change the
    fingerprint. Any added, removed or renamed learner changes it, which forces
    new snapshot provenance instead of rewriting an old QA output.
    """
    lines = sorted(f"{member['external_learner_id']}|{member['full_name']}"
                   for member in members)
    payload = "\n".join([f"version:{snapshot_version.strip()}",
                         f"module:{module_normalized}", *lines])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def legacy_students_payload(members) -> dict:
    """
    The legacy `lms_students` shape:

        {"students": [{"ID": <int>, "FullName": <text>}, ...]}

    ordered by FullName, as the legacy `jsonb_agg(... ORDER BY full_name)` did.
    """
    ordered = sorted(members, key=lambda member: (member["full_name"],
                                                  member["external_learner_id"]))
    return {"students": [{"ID": member["external_learner_id"],
                          "FullName": member["full_name"]} for member in ordered]}


def compare_with_legacy(members, legacy_students, legacy_count) -> dict:
    """
    Read-only comparison against the historical `qa_doctors_sessions` values.

    The legacy rows were written days ago against a live table, so a
    difference is drift, not necessarily a defect. Reported by counts and ids
    only - never by name.
    """
    if legacy_students is None and legacy_count is None:
        return {"status": "LEGACY_LMS_DATA_MISSING", "new_count": len(members),
                "legacy_count": None, "only_new_ids": None, "only_legacy_ids": None}
    legacy_rows = (legacy_students or {}).get("students") or []
    legacy_ids = {str(row.get("ID")) for row in legacy_rows if row.get("ID") is not None}
    new_ids = {str(member["external_learner_id"]) for member in members}
    only_new = sorted(new_ids - legacy_ids)
    only_legacy = sorted(legacy_ids - new_ids)
    matched = not only_new and not only_legacy and len(members) == (legacy_count or 0)
    return {
        "status": "LMS_ROSTER_MATCH" if matched else "LMS_ROSTER_DRIFT",
        "new_count": len(members), "legacy_count": legacy_count,
        "only_new_id_count": len(only_new), "only_legacy_id_count": len(only_legacy),
        # Learner ids, not names.
        "only_new_ids": only_new[:20], "only_legacy_ids": only_legacy[:20],
    }
