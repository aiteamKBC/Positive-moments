"""
Phase 3C3E: stamp the model-input fingerprint onto evaluations frozen before it
existed.

A deterministic refresh may only reuse a frozen model answer when it can PROVE
the provider was asked exactly the same question. That proof is the model-input
fingerprint, introduced in this phase - so every evaluation written before it
is unreusable, and a lecture whose attendance arrives would be blocked at
`NO_REUSABLE_MODEL_OUTPUT` for no better reason than the order the phases
happened in.

The fingerprint is derivable, but only carefully. It is computed from the
CURRENT input package, and stamped only after checking, field by field, that
the evaluation really was built on that same package: same document, same
selection, same transcript, same meeting, same duration, same Item 2 timing,
same prompt hash, same model. Attendance arrival cannot change any of those, so
a full match means the model-input half of the package is provably identical.

Any mismatch means the model was asked something else, and the row is skipped
and counted as NOT_DERIVABLE. Nothing is guessed, and a skipped row simply
keeps the behaviour it has today: a refresh refuses rather than reusing.

No provider call, no Graph call, no transcript read, and no legacy table.
"""
import json
import logging

from app.common.errors import DATABASE_ERROR, PlatformError
from app.qa.inputs import (
    MODEL_INPUT_FINGERPRINT_VERSION,
    qa_model_input_fingerprint,
)
from app.qa.prompt import prompt_sha256


UNSTAMPED = """
SELECT e.evaluation_id, e.lecture_id, e.document_id, e.selection_id,
       e.meeting_id, e.primary_provider_transcript_id, e.duration_minutes,
       e.start_difference_minutes, e.end_difference_minutes,
       e.prompt_sha256, e.model_name, e.qa_engine_version,
       coalesce(e.metadata ->> 'provider_contract_version', 'json_object_v1'),
       coalesce(e.metadata ->> 'punctuality_source_version', 'legacy_call_bounds_v1'),
       -- Phase 3C2.3B: a lecture can hold two canonical documents. The package
       -- must be rebuilt under the parser the evaluation actually used, or the
       -- document_id check refuses it - correctly, but for the wrong reason.
       d.parser_version
  FROM public.lecture_qa_evaluations e
  JOIN public.lecture_transcript_documents d ON d.document_id = e.document_id
 WHERE e.qa_status = 'COMPLETED'
   AND e.ai_called = true
   AND e.ai_raw_output IS NOT NULL
   AND e.metadata ->> 'model_input_fingerprint' IS NULL
 ORDER BY e.updated_at, e.evaluation_id
"""

STAMP = """
UPDATE public.lecture_qa_evaluations
   SET metadata = metadata || %s::jsonb
 WHERE evaluation_id = %s
   AND metadata ->> 'model_input_fingerprint' IS NULL
"""

# The evaluation columns that must agree with the recomputed package before the
# fingerprint may be attributed to it.
VERIFIED_FIELDS = (
    ("document_id", "document_id"),
    ("selection_id", "selection_id"),
    ("meeting_id", "meeting_id"),
    ("primary_provider_transcript_id", "primary_provider_transcript_id"),
    ("duration_minutes", "duration_minutes"),
    ("start_difference_minutes", "start_difference_minutes"),
    ("end_difference_minutes", "end_difference_minutes"),
)

DERIVED = "DERIVED"
NOT_DERIVABLE = "NOT_DERIVABLE"
NO_CURRENT_PACKAGE = "NO_CURRENT_PACKAGE"


class ModelInputFingerprintBackfill:
    """
    Stamps reusability provenance onto historical evaluations.

    `service_factory(punctuality_version, contract_version, parser_version)`
    must return a QA service configured for exactly those, because the
    package's Item 2 timing and its canonical document both depend on them.
    A package rebuilt under the wrong contract would fail the verification -
    correctly, but uselessly.
    """

    def __init__(self, *, service_factory):
        self.service_factory = service_factory
        self.log = logging.getLogger(__name__)

    def run(self, connection, *, persist: bool = True) -> dict:
        try:
            rows = connection.execute(UNSTAMPED).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "evaluation backfill read failed") from exc

        packages: dict = {}
        outcomes: dict = {DERIVED: 0, NOT_DERIVABLE: 0, NO_CURRENT_PACKAGE: 0}
        stamped = 0
        details = []

        for row in rows:
            (evaluation_id, lecture_id, document_id, selection_id, meeting_id,
             transcript_id, duration, start_diff, end_diff, prompt_sha, model_name,
             engine_version, contract_version, punctuality_version,
             parser_version) = row

            key = (str(lecture_id), punctuality_version, contract_version, model_name,
                   parser_version)
            if key not in packages:
                packages[key] = self._package(connection, lecture_id, punctuality_version,
                                              contract_version, parser_version)
            package = packages[key]
            if package is None:
                outcomes[NO_CURRENT_PACKAGE] += 1
                details.append({"evaluation_id": str(evaluation_id),
                                "outcome": NO_CURRENT_PACKAGE})
                continue

            evaluation = {
                "document_id": document_id, "selection_id": selection_id,
                "meeting_id": meeting_id,
                "primary_provider_transcript_id": transcript_id,
                "duration_minutes": duration,
                "start_difference_minutes": start_diff,
                "end_difference_minutes": end_diff,
            }
            mismatched = [name for name, field in VERIFIED_FIELDS
                          if str(evaluation[name]) != str(package[field])]
            if prompt_sha256(model=model_name) != prompt_sha:
                # The prompt contract itself moved; this answer belongs to a
                # different question and the fingerprint must not claim it.
                mismatched.append("prompt_sha256")
            if mismatched:
                outcomes[NOT_DERIVABLE] += 1
                details.append({"evaluation_id": str(evaluation_id),
                                "outcome": NOT_DERIVABLE, "mismatched": mismatched})
                continue

            fingerprint = qa_model_input_fingerprint(
                package=package, model=model_name, engine_version=engine_version,
                provider_contract_version=contract_version)
            outcomes[DERIVED] += 1
            details.append({"evaluation_id": str(evaluation_id), "outcome": DERIVED,
                            "model_input_fingerprint_prefix": fingerprint[:16],
                            "provider_contract_version": contract_version})
            if persist:
                self._stamp(connection, evaluation_id, fingerprint, contract_version)
                stamped += 1

        return {
            "mode": "BACKFILL" if persist else "DRY_RUN",
            "model_input_fingerprint_version": MODEL_INPUT_FINGERPRINT_VERSION,
            "considered": len(rows), "stamped": stamped, "outcomes": outcomes,
            "evaluations": details,
            "graph_calls": 0, "provider_calls": 0, "transcript_reads": 0,
            "legacy_tables_written": 0, "perfect_tables_written": 0,
        }

    def _package(self, connection, lecture_id, punctuality_version, contract_version,
                 parser_version):
        service = self.service_factory(punctuality_version, contract_version,
                                       parser_version)
        try:
            return service._load_lecture_package(connection, lecture_id)
        except Exception:
            # An unloadable package is a reason to skip, never to guess.
            return None

    def _stamp(self, connection, evaluation_id, fingerprint, contract_version) -> None:
        payload = {
            "model_input_fingerprint": fingerprint,
            "model_input_fingerprint_version": MODEL_INPUT_FINGERPRINT_VERSION,
            "model_input_fingerprint_source": "PHASE_3C3E_VERIFIED_BACKFILL",
            "provider_contract_version": contract_version,
        }
        try:
            connection.execute(STAMP, (json.dumps(payload, default=str), evaluation_id))
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "evaluation backfill write failed") from exc
