"""
The deterministic QA input package.

Everything the model and the deterministic rules need, assembled from evidence
this platform already persisted and validated:

  Phase 1   lecture identity and schedule
  Phase 2B  selection, actual start/end, punctuality differences, combined VTT
  Phase 2C1 canonical document and cues
  Phase 2C3 canonical trainer (v2 attendance snapshot)
  Phase 2C4 engagement result

No Graph call, no live attendance read, no recomputation. The QA engine is a
consumer of earlier phases, never a second implementation of them.
"""
import hashlib

from app.qa.prompt import PROMPT_VERSION, STRUCTURED_SCHEMA_VERSION, prompt_sha256
from app.qa.structured_output import DEFAULT_PROVIDER_CONTRACT


QA_ENGINE_VERSION = "shadow_qa_v8_engine_v1"

# The approved roster rule. Pinned, never "latest by timestamp": the
# makeup-inclusive v1 evidence is deliberately still on record.
REQUIRED_ATTENDANCE_ROSTER_VERSION = "attendance_roster_v2_exclude_makeup"

# Traced in the legacy export: `KSB_Framework` appears only inside the agent
# prompt and is assigned by no upstream node, so it always rendered empty.
# Reproduced exactly; nothing is invented to fill it.
KSB_FRAMEWORK = ""


def qa_source_fingerprint(*, package: dict, model: str,
                          engine_version: str = QA_ENGINE_VERSION,
                          provider_contract_version: str = DEFAULT_PROVIDER_CONTRACT) -> str:
    """
    SHA-256 over every input that can change a QA result, newline-joined in a
    fixed order:

      engine version, prompt version + prompt hash (which already covers the
      system message, the structured schema version and hash, and the model),
      model identity, selection id, combined transcript fingerprint and hash,
      document id and parser fingerprint, schedule and actual timing, duration,
      canonical trainer speaker id, attendance snapshot id, engagement id and
      its source fingerprint.

    Anything material that changes produces new QA provenance instead of
    overwriting an older evaluation.
    """
    lines = [
        f"engine:{engine_version}",
        f"prompt:{PROMPT_VERSION}:{prompt_sha256(model=model)}",
        f"schema:{STRUCTURED_SCHEMA_VERSION}",
        # Phase 3C3D: WHO enforces that schema. Under json_object_v1 the schema
        # reached the model as prose only; under strict_json_schema_v1 the
        # provider refuses a response that violates it. Same schema text, a
        # materially different generation contract - so an attempt bought under
        # the old contract is never counted against the new one, and the three
        # exhausted G2 Keith generations stay immutable history.
        f"provider_contract:{provider_contract_version}",
        f"model:{model}",
        f"lecture:{package['lecture_id']}",
        f"selection:{package['selection_id']}",
        f"combined:{package['combined_source_fingerprint']}:{package['combined_content_sha256']}",
        f"document:{package['document_id']}:{package['document_source_fingerprint']}",
        f"scheduled:{package['scheduled_start']}|{package['scheduled_end']}",
        f"actual:{package['actual_start']}|{package['actual_end']}",
        f"timing:{package['start_difference_minutes']}|{package['end_difference_minutes']}",
        # Phase 3C3B: which definition of "started" produced that timing. Two
        # sources can yield the same minutes on one lecture and differ wildly
        # on the next, so the source belongs in the fingerprint, not just the
        # numbers it happened to produce.
        f"punctuality:{package.get('punctuality_source_version', 'legacy_call_bounds_v1')}",
        f"duration:{package['duration_minutes']}",
        f"trainer:{package['canonical_trainer_speaker_id']}",
        f"snapshot:{package['attendance_snapshot_id']}",
        f"engagement:{package['engagement_id']}:{package['engagement_source_fingerprint']}",
    ]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


# Phase 3C3E. The subset of the fingerprint that determines the MODEL CALL.
#
# Attendance and engagement reach the evaluation but never reach the provider:
# `build_user_message` sends the transcript, its ids, the subject, the schedule
# and timing, the duration and the (always empty) KSB framework - and nothing
# about learners. So when attendance arrives later, the model's answer to the
# question it was actually asked has not changed, and re-buying it would be
# paying twice for the same generation.
#
# This fingerprint is what licenses reuse. If it matches, the frozen output is
# a valid answer for the new package; if it differs by even one line, it is
# not, and the refresh refuses rather than reusing something stale.
MODEL_INPUT_FINGERPRINT_VERSION = "qa_model_input_v1"


def qa_model_input_fingerprint(*, package: dict, model: str,
                               engine_version: str = QA_ENGINE_VERSION,
                               provider_contract_version: str
                               = DEFAULT_PROVIDER_CONTRACT) -> str:
    """SHA-256 over exactly the inputs that shape the provider request."""
    lines = [
        f"version:{MODEL_INPUT_FINGERPRINT_VERSION}",
        f"engine:{engine_version}",
        f"prompt:{PROMPT_VERSION}:{prompt_sha256(model=model)}",
        f"schema:{STRUCTURED_SCHEMA_VERSION}",
        f"provider_contract:{provider_contract_version}",
        f"model:{model}",
        f"lecture:{package['lecture_id']}",
        f"subject:{package['subject']}",
        f"meeting:{package['meeting_id']}",
        f"transcript:{package['primary_provider_transcript_id']}",
        f"selection:{package['selection_id']}",
        f"combined:{package['combined_source_fingerprint']}:{package['combined_content_sha256']}",
        f"document:{package['document_id']}:{package['document_source_fingerprint']}",
        f"scheduled:{package['scheduled_start']}|{package['scheduled_end']}",
        f"actual:{package['actual_start']}|{package['actual_end']}",
        f"timing:{package['start_difference_minutes']}|{package['end_difference_minutes']}",
        f"punctuality:{package.get('punctuality_source_version', 'legacy_call_bounds_v1')}",
        f"duration:{package['duration_minutes']}",
        f"ksb:{KSB_FRAMEWORK}",
    ]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def preview(package: dict) -> dict:
    """
    A safe, printable description of one QA input.

    Counts, ids, versions and hash prefixes only: never transcript text, never
    a learner name, never a prompt dump.
    """
    return {
        "lecture_id": str(package["lecture_id"]),
        "subject": package["subject"],
        "document_id": str(package["document_id"]),
        "selection_id": str(package["selection_id"]),
        "primary_provider_transcript_id": package["primary_provider_transcript_id"],
        "meeting_id": package["meeting_id"],
        "duration_minutes": package["duration_minutes"],
        "delivery_status": package["delivery_status"],
        "transcript_bytes": package["combined_content_bytes"],
        "transcript_cue_count": package["cue_count"],
        "start_difference_minutes": package["start_difference_minutes"],
        "end_difference_minutes": package["end_difference_minutes"],
        "punctuality_source_version": package.get("punctuality_source_version"),
        "provider_contract_version": package.get("provider_contract_version"),
        "attended_count": package["attended_count"],
        "spoke_count": package["spoke_count"],
        "engagement_percentage": package["engagement_percentage"],
        "engagement_score": package["engagement_score"],
        "learner_engagement_status": package["learner_engagement_status"],
        "item7_override_applied": package["item7_override_applied"],
        "engagement_calculation_status": package["engagement_calculation_status"],
        "attendance_roster_version": package["attendance_roster_version"],
        # Length only: the trainer's name is never printed.
        "canonical_trainer_label_length": len(package["canonical_trainer"] or ""),
        "ksb_framework_supplied": bool(KSB_FRAMEWORK),
    }
