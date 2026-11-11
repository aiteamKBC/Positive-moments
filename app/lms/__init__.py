"""
Kent LMS integration for the Continuous Bunny Publisher (phase P0-A).

The LMS database is read-only from here. The authoritative per-lecture LMS
identity is curriculum.live_session_occurrences.id; a lecture is joined to it
by the Teams meeting key plus the exact UTC start (rule meeting_key_start_v2,
docs/architecture/lms_matching_gap_investigation.md section 6.2).
"""
