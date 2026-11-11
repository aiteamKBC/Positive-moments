"""
Reading verified evidence and social-media metadata from stored clips.

The shapes are the real ones in `qa_doctors_sessions.positive_clips` on
2026-10-17: 839 legacy clips with the type only as a `reason` prefix, and 69
v5 clips carrying `selector_evidence_type` / `evidence_type` /
`verified_evidence_type`, `semantic_verification`, `social_media_verification`,
`social_media_formats`, `reel_segments` and `carousel_slide_outline`. The text
in them is invented.
"""
from django.test import SimpleTestCase

from positive_mentions.evidence import summarise
from positive_mentions.utils import normalize_clips


def legacy_clip(**overrides):
    clip = {
        "start": "00:10:00.000", "end": "00:10:20.000", "start_cue": 100, "end_cue": 104,
        "speaker": "Learner A", "positive_quote": "I finally understand it.",
        "quote": "Honestly, I finally understand it.", "category": "content",
        "feedback_target": "content", "feedback_scope": "current_session",
        "reason": "POSITIVE_MOMENT: the learner says the topic now makes sense.",
        "confidence": 0.9, "duration_seconds": 20, "dialogue": [],
        "positive_speakers": ["Learner A"], "other_speakers": [],
        "conversation_type": "learner_statement", "trainer_included": False,
        "semantic_verification": {"verdict": "accept", "confidence": 0.8,
                                  "reason": "Supported by the quote."},
    }
    clip.update(overrides)
    return clip


def verified_clip(**overrides):
    clip = legacy_clip(
        start="00:20:00.000", end="00:21:05.000", duration_seconds=65,
        start_cue=200, end_cue=210, speaker="Trainer", positive_quote="Here is how the case unfolded.",
        reason="OFSTED_CASE_STUDY: the tutor explains a workplace case.",
        selector_evidence_type="POSITIVE_MOMENT", evidence_type="OFSTED_CASE_STUDY",
        verified_evidence_type="OFSTED_CASE_STUDY", evidence_type_reclassified=True,
        selector_reason="Looked like learner praise.",
        conversation_type="tutor_case_study", trainer_included=True,
        semantic_verification={
            "verdict": "accept", "confidence": 0.91, "reason": "A worked case study.",
            "advisory_pass": True, "mode": "advisory", "requires_human_review": False,
            "final_evidence_type": "OFSTED_CASE_STUDY", "selector_evidence_type": "POSITIVE_MOMENT",
            "verified_evidence_type": "OFSTED_CASE_STUDY", "evidence_type_reclassified": True,
        },
        social_media_primary_format="reel", social_media_formats=["reel", "carousel"],
        social_media_ready=True, social_media_rationale="A clear story in under a minute.",
        social_media_verification={"status": "verified", "confidence": 0.7,
                                   "reason": "Formats agree.", "final_evidence_type": "OFSTED_CASE_STUDY"},
        reel_edit_mode="contiguous", reel_estimated_duration_seconds=48,
        reel_segments=[{"start_cue": 200, "end_cue": 205}, {"start_cue": 207, "end_cue": 210}],
        carousel_slide_outline=["The problem", "What the team tried", "The result"],
        dialogue=[
            {"start_cue": 200, "end_cue": 200, "start": "00:20:00.000", "end": "00:20:04.000",
             "speaker": "Trainer", "text": "Here is how the case unfolded."},
            {"start_cue": 205, "end_cue": 205, "start": "00:20:30.000", "end": "00:20:36.000",
             "speaker": "Trainer", "text": "They changed the process."},
            {"start_cue": 210, "end_cue": 210, "start": "00:21:00.000", "end": "00:21:05.000",
             "speaker": "Trainer", "text": "And the delays stopped."},
        ],
    )
    clip.update(overrides)
    return clip


def one(clip):
    (normalized,) = normalize_clips([clip])
    return normalized


class LegacyCompatibilityTests(SimpleTestCase):
    def test_a_legacy_clip_takes_its_type_from_the_reason_prefix(self):
        clip = one(legacy_clip())
        self.assertEqual(clip["final_evidence_type"], "POSITIVE_MOMENT")
        self.assertEqual(clip["evidence_type_source"], "reason_prefix")
        self.assertIsNone(clip["evidence_type"])

    def test_a_legacy_ofsted_prefix_is_read_too(self):
        clip = one(legacy_clip(reason="OFSTED_CASE_STUDY: a worked example."))
        self.assertEqual(clip["final_evidence_type"], "OFSTED_CASE_STUDY")

    def test_a_reason_without_a_prefix_leaves_the_type_unknown(self):
        clip = one(legacy_clip(reason="Learner praised the session."))
        self.assertIsNone(clip["final_evidence_type"])
        self.assertIsNone(clip["evidence_type_source"])

    def test_missing_new_fields_stay_unknown_not_false(self):
        clip = one(legacy_clip())
        for key in ("social_media_ready", "social_media_formats", "reel_segments",
                    "carousel_slide_outline", "social_verification_status",
                    "reel_edit_mode", "evidence_type_reclassified", "semantic_advisory_pass"):
            self.assertIsNone(clip[key], key)
        self.assertEqual(clip["social_formats"], [])
        self.assertFalse(clip["is_social_candidate"])

    def test_no_semantic_verdict_means_not_verified_never_accepted(self):
        clip = one(legacy_clip(semantic_verification=None))
        self.assertEqual(clip["semantic_status"], "not_verified")
        self.assertIsNone(clip["semantic_verdict"])
        self.assertEqual(clip["semantic_verification"], {})       # existing contract kept

    def test_existing_fields_and_order_are_unchanged(self):
        clips = normalize_clips([legacy_clip(start="00:30:00.000"), legacy_clip(start="00:05:00.000")])
        self.assertEqual([clip["original_index"] for clip in clips], [1, 0])
        self.assertEqual(clips[0]["quote"], "Honestly, I finally understand it.")
        self.assertEqual(clips[0]["positive_speakers"], ["Learner A"])


class VerifiedEvidenceTests(SimpleTestCase):
    def test_the_verified_type_wins_over_the_selector(self):
        clip = one(verified_clip())
        self.assertEqual(clip["final_evidence_type"], "OFSTED_CASE_STUDY")
        self.assertEqual(clip["evidence_type_source"], "verified")
        self.assertEqual(clip["selector_evidence_type"], "POSITIVE_MOMENT")
        self.assertTrue(clip["evidence_type_reclassified"])

    def test_the_verified_type_wins_over_a_contradicting_reason_prefix(self):
        clip = one(verified_clip(reason="POSITIVE_MOMENT: selector's words."))
        self.assertEqual(clip["final_evidence_type"], "OFSTED_CASE_STUDY")

    def test_semantic_fields_are_read_from_the_nested_verifier(self):
        clip = one(verified_clip())
        self.assertEqual(clip["semantic_verdict"], "accept")
        self.assertEqual(clip["semantic_status"], "accepted")
        self.assertEqual(clip["semantic_confidence"], 0.91)
        self.assertEqual(clip["semantic_reason"], "A worked case study.")
        self.assertTrue(clip["semantic_advisory_pass"])

    def test_a_flat_semantic_verdict_wins_when_present(self):
        clip = one(verified_clip(semantic_verdict="reject", semantic_reason="Flat reason."))
        self.assertEqual(clip["semantic_status"], "rejected")
        self.assertEqual(clip["semantic_reason"], "Flat reason.")

    def test_an_ai_rejected_candidate_stays_in_the_list(self):
        rejected = verified_clip(semantic_verification={"verdict": "reject", "reason": "Not evidence."})
        clips = normalize_clips([legacy_clip(), rejected])
        self.assertEqual(len(clips), 2)
        self.assertEqual(sorted(clip["semantic_status"] for clip in clips), ["accepted", "rejected"])

    def test_a_verifier_reject_type_is_kept_as_reject(self):
        clip = one(verified_clip(evidence_type="REJECT", verified_evidence_type="REJECT",
                                 semantic_verification={"verdict": "reject", "final_evidence_type": "REJECT"}))
        self.assertEqual(clip["final_evidence_type"], "REJECT")

    def test_social_verifier_fields_are_flattened(self):
        clip = one(verified_clip())
        self.assertEqual(clip["social_verification_status"], "verified")
        self.assertEqual(clip["social_verifier_confidence"], 0.7)
        self.assertEqual(clip["social_verifier_reason"], "Formats agree.")


class SocialMediaTests(SimpleTestCase):
    def test_ofsted_with_reel_and_carousel_keeps_both_classifications(self):
        clip = one(verified_clip())
        self.assertEqual(clip["final_evidence_type"], "OFSTED_CASE_STUDY")
        self.assertEqual(clip["social_formats"], ["reel", "carousel"])
        self.assertTrue(clip["is_social_candidate"])

    def test_the_type_is_never_inferred_from_social_formats(self):
        clip = one(legacy_clip(reason="No prefix here.", social_media_formats=["reel"]))
        self.assertIsNone(clip["final_evidence_type"])
        self.assertTrue(clip["is_social_candidate"])

    def test_reel_segments_carry_times_from_the_stored_dialogue(self):
        clip = one(verified_clip())
        self.assertEqual(clip["reel_segments"], [
            {"start_cue": 200, "end_cue": 205, "start": "00:20:00.000", "end": "00:20:36.000"},
            {"start_cue": 207, "end_cue": 210, "start": None, "end": "00:21:05.000"},
        ])
        self.assertEqual(clip["reel_edit_mode"], "contiguous")
        self.assertEqual(clip["reel_estimated_duration_seconds"], 48)

    def test_the_carousel_outline_keeps_its_order(self):
        clip = one(verified_clip())
        self.assertEqual(clip["carousel_slide_outline"],
                         ["The problem", "What the team tried", "The result"])

    def test_an_empty_format_list_is_not_a_candidate_and_none_is_not_a_format(self):
        clip = one(verified_clip(social_media_formats=[], social_media_primary_format="none",
                                 social_media_ready=False))
        self.assertEqual(clip["social_media_formats"], [])
        self.assertFalse(clip["is_social_candidate"])
        self.assertFalse(clip["social_media_ready"])

    def test_malformed_optional_fields_are_dropped_not_guessed(self):
        clip = one(verified_clip(reel_segments="200-210", carousel_slide_outline=["One", 2, None],
                                 social_media_formats="reel"))
        self.assertIsNone(clip["reel_segments"])
        self.assertEqual(clip["carousel_slide_outline"], ["One"])
        self.assertIsNone(clip["social_media_formats"])
        self.assertEqual(clip["social_formats"], ["reel"])             # from the primary format


class SummaryTests(SimpleTestCase):
    def test_counts_come_from_clip_metadata_and_never_double_count(self):
        clips = normalize_clips([
            verified_clip(),                                               # OFSTED, reel+carousel, accepted
            legacy_clip(start_cue=1, end_cue=2),                           # PM legacy, accepted
            legacy_clip(start_cue=3, end_cue=4, semantic_verification={"verdict": "reject"},
                        social_media_formats=["quote_post"]),             # PM, rejected, quote post
            legacy_clip(start_cue=5, end_cue=6, reason="no prefix", semantic_verification={}),
        ])
        self.assertEqual(summarise(clips), {
            "total": 4, "ai_accepted": 2, "ai_rejected": 1, "not_verified": 1,
            "positive_moments": 2, "ofsted_case_studies": 1, "rejected_as_evidence": 0,
            "unclassified": 1, "social_candidates": 2, "reel": 1, "carousel": 1, "quote_post": 1,
        })
