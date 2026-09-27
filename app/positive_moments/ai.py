"""
Stages 1 and 3: the recall SELECTOR and the independent precision VERIFIER.

Both are strict-JSON-schema model calls through the platform's existing
OpenAI transport (app/qa/provider.py): same key handling, same bounded
retries, same sanitised errors. Neither prompt asks for, or accepts, quotes,
timestamps or reasoning: the selector names cue RANGES and speakers; the
verifier returns a verdict per rebuilt candidate. Nothing either model writes
is persisted as evidence, and no chain-of-thought is requested or stored.

THE VERIFIER IS BLIND TO THE SELECTOR
-------------------------------------
It is given only the rebuilt dialogue (from database cues), each line marked
TRAINER or LEARNER by the deterministic role rule. It never sees the
selector's category, reason or confidence, so it cannot rubber-stamp them.
"""
from __future__ import annotations

import json

from app.positive_moments import policy as p
from app.positive_moments.models import CanonicalTranscript, Candidate
from app.qa.provider import QA_PROVIDER, OpenAIChatProvider, ProviderError


# ---------------------------------------------------------------------------
# the provider call
# ---------------------------------------------------------------------------

class JsonSchemaModel(OpenAIChatProvider):
    """OpenAIChatProvider with a caller-supplied strict schema per call."""

    def complete_schema(self, *, system_message: str, user_message: str,
                        schema_name: str, schema: dict) -> dict:
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system_message},
                         {"role": "user", "content": user_message}],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": schema_name, "strict": True, "schema": schema}},
        }).encode("utf-8")
        last = None
        for attempt in range(1, self.max_tries + 1):
            try:
                payload = self._post("/chat/completions", body)
            except ProviderError as error:
                last = error
                retryable = error.http_status is None or error.http_status in (408, 409, 429) \
                    or (error.http_status or 0) >= 500
                if not retryable or attempt == self.max_tries:
                    error.attempts = attempt
                    raise
                self._sleep(self.wait_between_tries_ms / 1000)
                continue
            choices = payload.get("choices") or []
            content = (choices[0].get("message", {}).get("content")
                       if choices and isinstance(choices[0], dict) else None)
            if not content:
                last = ProviderError("empty_model_response", "model returned no content",
                                     attempts=attempt)
                if attempt == self.max_tries:
                    raise last
                continue
            try:
                parsed = json.loads(content)
            except ValueError as error:
                raise ProviderError("model_output_not_json", "model response was not valid JSON",
                                    attempts=attempt) from error
            return {"output": parsed, "provider": QA_PROVIDER,
                    "model_requested": self.model, "model_reported": payload.get("model"),
                    "response_id": payload.get("id"), "usage": payload.get("usage") or {},
                    "attempts": attempt}
        raise last or ProviderError("provider_error", "model call failed")


# ---------------------------------------------------------------------------
# shared rules text
# ---------------------------------------------------------------------------

BUSINESS_RULES = """
A POSITIVE MOMENT is learner-positive evidence from professional training.

ACCEPT when at least one LEARNER expresses real positive value about:
- the trainer/facilitator, their teaching or explanation, a teaching method or activity;
- the content, a concept or framework; support or coaching received;
- the learning experience: clearer understanding, confidence, useful learning;
- relevance to their role/work, or an intended practical application
  ("this would really help in my role", "I can actually use this with...");
- movement from confusion to understanding ("that makes much more sense now");
- appreciation of concrete trainer support.
The learner need not say "this training" if the connected dialogue clearly
establishes the training target.

REJECT: greetings, farewells, small talk; attendance/admin talk; jokes without
learning evidence; generic "thanks", "great", "amazing", "all good" without an
identifiable training target; trainer self-praise or trainer-only positivity;
neutral participation; merely answering a question correctly; reading a slide,
defining a concept or repeating supplied material; workplace/business success
stories with no training benefit or intended application; praise of software,
UI or meeting controls (mute, camera, screen share, connection, Teams, chat);
positive wording whose referent cannot be proven from the dialogue shown.
Never infer a training connection from the lecture title alone.

Trainer lines may sit inside a moment when they set the topic, ask the question
that elicits the feedback, give the explanation the learner then praises, or
show confusion becoming understanding. But only LEARNERS who themselves express
positive value are positive speakers. The trainer is never a positive speaker.
""".strip()


# ---------------------------------------------------------------------------
# stage 1: recall selector
# ---------------------------------------------------------------------------

SELECTOR_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["candidates"],
    "properties": {"candidates": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["start_cue", "end_cue", "positive_speakers", "category",
                     "reason", "confidence"],
        "properties": {
            "start_cue": {"type": "integer"},
            "end_cue": {"type": "integer"},
            "positive_speakers": {"type": "array", "items": {"type": "string"}},
            "category": {"type": "string", "enum": list(p.CATEGORIES)},
            "reason": {"type": "string"},
            "confidence": {"type": "number"},
        }}}},
}

SELECTOR_SYSTEM = f"""You are a RECALL-oriented selector of Positive Moments in a
timestamped training transcript. Propose every plausible candidate; a separate
verifier will reject weak ones, so prefer recall over precision.

{BUSINESS_RULES}

OUTPUT RULES
- Return cue RANGES only: start_cue and end_cue are cue numbers shown as c<N>.
- Never invent a cue number. Never quote text. Never give timestamps.
- positive_speakers: exact speaker names as shown, learners only.
- Prefer concise evidence of roughly 2-60 seconds; include just enough
  surrounding dialogue for the moment to make sense.
- reason: one short sentence naming the training target. No step-by-step thinking.
- confidence: 0 to 1.
- If there are no candidates, return an empty list."""


def format_cue(transcript: CanonicalTranscript, cue) -> str:
    seconds = cue.start_ms // 1000
    role = "TRAINER" if transcript.is_trainer(cue.speaker) else "LEARNER"
    stamp = f"{seconds // 3600:d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"c{cue.cue_index} [{stamp}] {cue.speaker or 'Unknown'} ({role}): {cue.text}"


def selector_user_message(transcript: CanonicalTranscript) -> str:
    lines = [f"Lecture: {transcript.subject or 'unknown'}",
             f"Trainer (deterministic): {transcript.trainer_speaker or 'unknown'}",
             "Transcript cues:"]
    lines.extend(format_cue(transcript, cue) for cue in transcript.cues)
    return "\n".join(lines)


def parse_candidates(output: dict) -> list:
    rows = output.get("candidates") if isinstance(output, dict) else None
    found = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            start, end = int(row.get("start_cue")), int(row.get("end_cue"))
        except (TypeError, ValueError):
            continue
        speakers = row.get("positive_speakers")
        found.append(Candidate(
            start_cue=start, end_cue=end,
            positive_speakers=tuple(str(s) for s in speakers) if isinstance(speakers, list) else (),
            category=row.get("category"), reason=row.get("reason"),
            confidence=row.get("confidence")))
    return found


def select(model, transcript: CanonicalTranscript) -> tuple[list, dict]:
    reply = model.complete_schema(system_message=SELECTOR_SYSTEM,
                                  user_message=selector_user_message(transcript),
                                  schema_name="positive_moment_candidates",
                                  schema=SELECTOR_SCHEMA)
    return parse_candidates(reply.get("output")), _provenance(reply, p.SELECTOR_PROMPT_VERSION)


# ---------------------------------------------------------------------------
# stage 3: independent precision verifier
# ---------------------------------------------------------------------------

VERIFIER_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["verdicts"],
    "properties": {"verdicts": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["candidate_id", "accept", "category", "training_target",
                     "positive_speakers", "rejection_code", "confidence"],
        "properties": {
            "candidate_id": {"type": "string"},
            "accept": {"type": "boolean"},
            "category": {"type": ["string", "null"], "enum": list(p.CATEGORIES) + [None]},
            "training_target": {"type": "string", "enum": list(p.VERIFIER_TARGETS)},
            "positive_speakers": {"type": "array", "items": {"type": "string"}},
            "rejection_code": {"type": ["string", "null"],
                               "enum": list(p.VERIFIER_REJECTION_CODES) + [None]},
            "confidence": {"type": "number"},
        }}}},
}

VERIFIER_SYSTEM = f"""You are an independent, PRECISION-oriented auditor of
proposed Positive Moments for publication as short video clips. Each candidate
is shown only as its exact dialogue. Judge it on that dialogue alone.

{BUSINESS_RULES}

For EVERY candidate return one verdict:
- accept: true only if the dialogue itself proves a learner expressed positive
  value about an identifiable training target. When in doubt, reject.
- category: one of the five when accepted, else null.
- training_target: what the positive value is about, or "none".
- positive_speakers: the LEARNERS (never the trainer) who express it.
- rejection_code: the best-fitting code when rejected, else null.
- confidence: 0 to 1 in your verdict.
Do not explain your reasoning."""


def verifier_user_message(moments: list) -> str:
    blocks = []
    for number, moment in enumerate(moments, start=1):
        lines = [f"### candidate m{number}"]
        for line in moment.dialogue:
            role = "TRAINER" if line["role"] == "trainer" else "LEARNER"
            lines.append(f"{line['speaker'] or 'Unknown'} ({role}): {line['text']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def verify(model, moments: list) -> tuple[dict, dict]:
    """{candidate_id: verdict}, provenance. candidate ids are m1..mN."""
    if not moments:
        return {}, {}
    reply = model.complete_schema(system_message=VERIFIER_SYSTEM,
                                  user_message=verifier_user_message(moments),
                                  schema_name="positive_moment_verdicts",
                                  schema=VERIFIER_SCHEMA)
    output = reply.get("output") if isinstance(reply.get("output"), dict) else {}
    verdicts = {}
    for row in output.get("verdicts") or []:
        if isinstance(row, dict) and isinstance(row.get("candidate_id"), str):
            verdicts.setdefault(row["candidate_id"].strip(), row)
    return verdicts, _provenance(reply, p.VERIFIER_PROMPT_VERSION)


def apply_verdicts(moments: list, verdicts: dict) -> tuple[list, dict]:
    """
    Keep only what the verifier accepted, on the verifier's own terms.

    The final category is the VERIFIER's; its positive speakers are
    intersected with the learners who actually speak in the rebuilt dialogue,
    so it cannot introduce the trainer or a name that is not there either.
    """
    accepted, rejections = [], {}

    def reject(code):
        rejections[code] = rejections.get(code, 0) + 1

    for number, moment in enumerate(moments, start=1):
        verdict = verdicts.get(f"m{number}")
        if verdict is None:
            reject(p.VERIFIER_NO_VERDICT)
            continue
        try:
            confidence = min(max(float(verdict.get("confidence")), 0.0), 1.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if verdict.get("accept") is not True:
            reject(p.VERIFIER_REJECTED)
            code = verdict.get("rejection_code")
            if code in p.VERIFIER_REJECTION_CODES:
                reject(f"VERIFIER:{code}")
            continue
        if confidence < p.MIN_VERIFIER_CONFIDENCE:
            reject(p.VERIFIER_LOW_CONFIDENCE)
            continue
        category = p.normalize_category(verdict.get("category"))
        learners = {p.normalize_speaker(line["speaker"]): line["speaker"]
                    for line in moment.dialogue if line["role"] == "learner" and line["speaker"]}
        named = [learners[p.normalize_speaker(s)] for s in verdict.get("positive_speakers") or []
                 if p.normalize_speaker(s) in learners]
        if category is None:
            reject(p.INVALID_CATEGORY)
            continue
        if not named:
            reject(p.NO_POSITIVE_SPEAKER)
            continue
        positive = set(p.normalize_speaker(s) for s in named)
        moment.category = category
        moment.positive_speakers = list(dict.fromkeys(named))
        moment.positive_quote = " ".join(line["text"] for line in moment.dialogue
                                         if p.normalize_speaker(line["speaker"]) in positive)
        for line in moment.dialogue:
            line["positive"] = p.normalize_speaker(line["speaker"]) in positive
        moment.verifier_verdict = "ACCEPTED"
        moment.verifier_confidence = confidence
        moment.verifier_detail = {"training_target": verdict.get("training_target"),
                                  "prompt_version": p.VERIFIER_PROMPT_VERSION}
        accepted.append(moment)
    return accepted, rejections


def _provenance(reply: dict, prompt_version: str) -> dict:
    usage = reply.get("usage") or {}
    return {"provider": reply.get("provider"), "model": reply.get("model_requested"),
            "model_reported": reply.get("model_reported"),
            "prompt_version": prompt_version, "attempts": reply.get("attempts"),
            "usage": {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens",
                                                 "total_tokens") if k in usage}}
