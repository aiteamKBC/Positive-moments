"""
Phase 3C3D unit tests: the provider-enforced structured output contract.

The defect these pin down is proven, not hypothetical. On 2026-09-17 the model
answered `"K"` / `"S"` / `"B"` where the schema declares
`["Knowledge", "Skill", "Behaviour"]`, four times across two pilots, and
exhausted all three generations for one lecture - because the schema was only
ever prompt text. `response_format` said `{"type": "json_object"}`.

Nothing here calls a provider or a database.
"""
import json
import uuid
from datetime import datetime, timezone

import pytest

from app.qa import structured_output as contract
from app.qa.inputs import qa_source_fingerprint
from app.qa.prompt import STRUCTURED_OUTPUT_SCHEMA
from app.qa.provider import OpenAIChatProvider
from app.qa.validation import KSB_TYPES, validate_structured_output

from tests.unit.test_shadow_qa import good_output, package


KSB_ENUM = ["Knowledge", "Skill", "Behaviour"]


def _ksb_type_node(schema):
    return schema["properties"]["ksbs_covered"]["items"]["properties"]["type"]


def _captured_body(contract_version):
    """The exact JSON body the provider would POST, without posting it."""
    sent = {}

    class _Provider(OpenAIChatProvider):
        def _post(self, path, body):
            sent["path"] = path
            sent["body"] = json.loads(body.decode("utf-8"))
            return {"model": "gpt-5.2", "id": "resp_1", "usage": {},
                    "choices": [{"message": {"content": json.dumps(good_output())}}]}

    provider = _Provider(api_key="k", model="gpt-5.2",
                         response_contract=contract_version)
    response = provider.complete_json(system_message="S", user_message="U")
    return sent, response


# --- 1. the strict schema really is sent to the provider ---------------------

def test_the_provider_receives_a_strict_json_schema_contract():
    sent, _ = _captured_body(contract.STRICT_JSON_SCHEMA)
    response_format = sent["body"]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["name"] == contract.STRICT_SCHEMA_NAME
    assert response_format["json_schema"]["schema"] == contract.STRICT_OUTPUT_SCHEMA


def test_the_ksb_enum_is_in_the_body_that_reaches_the_provider():
    """The whole point: the enum must travel as a CONSTRAINT, not as prose."""
    sent, _ = _captured_body(contract.STRICT_JSON_SCHEMA)
    schema = sent["body"]["response_format"]["json_schema"]["schema"]
    assert _ksb_type_node(schema)["enum"] == KSB_ENUM


def test_the_old_contract_is_still_selectable_and_still_sends_plain_json_mode():
    """The G2 Keith attempts were bought under this; it stays reproducible."""
    sent, _ = _captured_body(contract.JSON_OBJECT)
    assert sent["body"]["response_format"] == {"type": "json_object"}
    assert "json_schema" not in json.dumps(sent["body"]["response_format"])


def test_the_default_contract_is_the_strict_one():
    assert contract.DEFAULT_PROVIDER_CONTRACT == contract.STRICT_JSON_SCHEMA
    provider = OpenAIChatProvider(api_key="k", model="gpt-5.2")
    assert provider.response_contract == contract.STRICT_JSON_SCHEMA


def test_an_unknown_contract_is_refused_rather_than_defaulted():
    with pytest.raises(contract.ProviderContractError):
        contract.response_format("strict_json_schema_v99")
    with pytest.raises(contract.ProviderContractError):
        OpenAIChatProvider(api_key="k", model="gpt-5.2", response_contract="nonsense")


# --- 2. the strict schema is a faithful, strictly weaker derivation ----------

def test_strict_mode_requires_every_property_and_forbids_extras():
    def walk(node, path="$"):
        if not isinstance(node, dict):
            return
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, path
            assert sorted(node["required"]) == sorted(node["properties"]), path
        for name, child in (node.get("properties") or {}).items():
            walk(child, f"{path}.{name}")
        if isinstance(node.get("items"), dict):
            walk(node["items"], f"{path}[]")

    walk(contract.STRICT_OUTPUT_SCHEMA)


def test_unsupported_bounds_are_dropped_and_named_not_silently_kept():
    body = json.dumps(contract.STRICT_OUTPUT_SCHEMA)
    for keyword in ("minItems", "maxItems", "minimum", "maximum"):
        assert keyword not in body
    # And the derivation says so out loud, so nobody has to rediscover it.
    assert ".checklist_evaluation.minItems" in contract.DROPPED_CONSTRAINTS
    assert ".teaching_quality.rating_1_5.minimum" in contract.DROPPED_CONSTRAINTS


def test_every_dropped_bound_is_still_enforced_by_the_local_validator():
    """Weaker at the provider is only acceptable because the validator holds."""
    assert validate_structured_output(good_output()) == []
    twelve = good_output()
    twelve["checklist_evaluation"] = twelve["checklist_evaluation"] + [
        dict(twelve["checklist_evaluation"][0])]
    assert any(code.startswith("CHECKLIST_COUNT") for code in
               validate_structured_output(twelve))
    rating = good_output()
    rating["teaching_quality"] = {**rating["teaching_quality"], "rating_1_5": 9}
    assert validate_structured_output(rating)


def test_only_originally_optional_properties_became_nullable():
    assert contract.OPTIONAL_PROPERTY_NAMES == {"speaker", "reasoning"}
    for path in contract.NULLABLE_OPTIONAL_PROPERTIES:
        assert path.rsplit(".", 1)[-1] in {"speaker", "reasoning"}


def test_the_base_schema_is_not_mutated_by_the_derivation():
    checklist = STRUCTURED_OUTPUT_SCHEMA["properties"]["checklist_evaluation"]
    assert checklist["minItems"] == 11 and checklist["maxItems"] == 11
    assert _ksb_type_node(STRUCTURED_OUTPUT_SCHEMA)["enum"] == KSB_ENUM


# --- 3. K / S / B stay invalid, and are never rewritten ----------------------

@pytest.mark.parametrize("value", ["K", "S", "B"])
def test_the_abbreviations_the_model_actually_emitted_are_rejected(value):
    """The exact failure mode observed for G2 Keith on 2026-09-17."""
    payload = good_output(ksbs_covered=[
        {"type": value, "title": "Planning", "evidence_clips": []}])
    assert any(code.startswith("INVALID_KSB_TYPE")
               for code in validate_structured_output(payload))
    # And the provider schema would have refused it before it was paid for.
    assert value not in _ksb_type_node(contract.STRICT_OUTPUT_SCHEMA)["enum"]


@pytest.mark.parametrize("value", KSB_ENUM)
def test_the_three_spelled_out_types_are_accepted(value):
    payload = good_output(ksbs_covered=[
        {"type": value, "title": "Planning", "evidence_clips": []}])
    assert validate_structured_output(payload) == []


@pytest.mark.parametrize("value", ["knowledge", "Skills", "Attitude", "", "Behavior"])
def test_any_other_enum_value_is_rejected_including_near_misses(value):
    payload = good_output(ksbs_covered=[
        {"type": value, "title": "Planning", "evidence_clips": []}])
    assert any(code.startswith("INVALID_KSB_TYPE")
               for code in validate_structured_output(payload))


def test_nothing_in_the_pipeline_translates_an_abbreviation():
    """`K` must stay `K` all the way to the validator, and be refused there."""
    payload = {"ksbs_covered": [{"type": "K", "title": "t", "evidence_clips": []}]}
    normalised, dropped = contract.drop_null_optionals(payload)
    assert normalised["ksbs_covered"][0]["type"] == "K"
    assert dropped == 0
    assert "K" not in KSB_TYPES


# --- 4. the nullable encoding is undone, and only that ----------------------

def test_null_speaker_and_reasoning_are_treated_as_absent():
    payload = good_output()
    payload["checklist_evaluation"][0] = {
        **payload["checklist_evaluation"][0], "reasoning": None,
        "evidence_clips": [{"start": "00:00:01.000", "end": "00:00:04.000",
                            "speaker": None}]}
    cleaned, dropped = contract.drop_null_optionals(payload)
    assert dropped == 2
    assert "reasoning" not in cleaned["checklist_evaluation"][0]
    assert "speaker" not in cleaned["checklist_evaluation"][0]["evidence_clips"][0]
    assert validate_structured_output(cleaned) == []


def test_a_null_on_a_required_property_is_left_alone_for_the_validator():
    payload = good_output()
    payload["checklist_evaluation"][0] = {
        **payload["checklist_evaluation"][0], "status": None}
    cleaned, dropped = contract.drop_null_optionals(payload)
    assert dropped == 0
    assert cleaned["checklist_evaluation"][0]["status"] is None
    assert validate_structured_output(cleaned)


def test_a_non_null_optional_value_survives_untouched():
    payload = good_output()
    payload["checklist_evaluation"][0] = {
        **payload["checklist_evaluation"][0], "reasoning": "durationMinutes 120"}
    cleaned, dropped = contract.drop_null_optionals(payload)
    assert dropped == 0
    assert cleaned["checklist_evaluation"][0]["reasoning"] == "durationMinutes 120"


def test_the_old_contract_does_not_strip_nulls():
    """Only strict mode produces the null encoding, so only it undoes it."""
    output = good_output()
    output["checklist_evaluation"][0] = {
        **output["checklist_evaluation"][0], "reasoning": None}

    class _Provider(OpenAIChatProvider):
        def _post(self, path, body):
            return {"model": "gpt-5.2", "id": "r", "usage": {},
                    "choices": [{"message": {"content": json.dumps(output)}}]}

    plain = _Provider(api_key="k", model="gpt-5.2",
                      response_contract=contract.JSON_OBJECT).complete_json(
        system_message="S", user_message="U")
    assert plain["null_optionals_dropped"] == 0
    assert plain["output"]["checklist_evaluation"][0]["reasoning"] is None

    strict = _Provider(api_key="k", model="gpt-5.2",
                       response_contract=contract.STRICT_JSON_SCHEMA).complete_json(
        system_message="S", user_message="U")
    assert strict["null_optionals_dropped"] == 1


# --- 5. the contract version is provenance ----------------------------------

def test_the_contract_version_changes_the_generation_fingerprint():
    base = package()
    old = qa_source_fingerprint(package=base, model="gpt-5.2",
                                provider_contract_version=contract.JSON_OBJECT)
    new = qa_source_fingerprint(package=base, model="gpt-5.2",
                                provider_contract_version=contract.STRICT_JSON_SCHEMA)
    assert old != new


def test_the_fingerprint_is_stable_for_one_contract():
    base = package()
    assert (qa_source_fingerprint(package=base, model="gpt-5.2",
                                  provider_contract_version=contract.STRICT_JSON_SCHEMA)
            == qa_source_fingerprint(package=dict(base), model="gpt-5.2",
                                     provider_contract_version=contract.STRICT_JSON_SCHEMA))


def test_the_fingerprint_default_is_the_strict_contract():
    base = package()
    assert (qa_source_fingerprint(package=base, model="gpt-5.2")
            == qa_source_fingerprint(package=base, model="gpt-5.2",
                                     provider_contract_version=contract.STRICT_JSON_SCHEMA))


def test_the_response_carries_its_contract_provenance():
    _, response = _captured_body(contract.STRICT_JSON_SCHEMA)
    assert response["response_contract"] == contract.STRICT_JSON_SCHEMA
    provenance = response["contract_provenance"]
    assert provenance["provider_enforced_schema"] is True
    assert provenance["strict_schema_sha256"] == contract.STRICT_OUTPUT_SCHEMA_SHA256
    # The dropped bounds are declared, so "provider-enforced" is never read as
    # "everything is provider-enforced".
    assert provenance["constraints_not_provider_enforced"]


def test_the_old_contract_declares_that_nothing_was_enforced():
    provenance = contract.contract_provenance(contract.JSON_OBJECT)
    assert provenance["provider_enforced_schema"] is False
    assert provenance["strict_schema_sha256"] is None


def test_provenance_never_contains_the_schema_body_or_a_secret():
    printed = json.dumps(contract.contract_provenance(contract.STRICT_JSON_SCHEMA))
    assert "Knowledge" not in printed
    assert "additionalProperties" not in printed
    assert '"enum"' not in printed
    # The schema travels as a hash, not as a body.
    assert contract.STRICT_OUTPUT_SCHEMA_SHA256 in printed


def test_the_strict_schema_hash_is_pinned_to_its_content():
    import hashlib
    expected = hashlib.sha256(
        json.dumps(contract.STRICT_OUTPUT_SCHEMA, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()
    assert contract.STRICT_OUTPUT_SCHEMA_SHA256 == expected
