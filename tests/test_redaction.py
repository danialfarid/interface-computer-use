import json

from cua.evidence import EvidenceRecorder
from cua.redaction import redact_artifact_payload, redact_text, redact_value


def test_redaction_handles_bearer_and_key_formats_without_crashing():
    value = (
        "Authorization: Bearer abc.def.ghi api_key=secret-value password: hunter2 "
        "http://demo_user:synthetic_password@127.0.0.1:8765/"
    )

    redacted = redact_text(value)

    assert "abc.def.ghi" not in redacted
    assert "secret-value" not in redacted
    assert "hunter2" not in redacted
    assert "demo_user" not in redacted
    assert "synthetic_password" not in redacted
    assert "JANE EXAMPLE" not in redact_text("Member name\tJANE EXAMPLE")
    assert "jane example" not in redact_text("Member name\tjane example")
    assert "jane example" not in redact_text("Member name\njane example")
    assert "<REDACTED>" in redacted


def test_redaction_masks_unlabeled_numeric_values_in_observation_text():
    redacted = redact_text("Current savings balance 85")

    assert "85" not in redacted
    assert "<REDACTED>" in redacted


def test_evidence_redacts_sensitive_observation_payloads(tmp_path):
    evidence = EvidenceRecorder(tmp_path)
    evidence.event(
        "observation",
        observation={
            "url": "http://127.0.0.1:8765/member",
            "text": "Current savings balance 85",
            "readable_targets": [{"text": "85"}],
        },
    )

    record = json.loads(evidence.log_path.read_text(encoding="utf-8"))
    assert "85" not in json.dumps(record["payload"])


def test_evidence_redacts_readable_target_text_without_capitalization(tmp_path):
    evidence = EvidenceRecorder(tmp_path)
    evidence.event(
        "observation",
        observation={
            "readable_targets": [
                {"text": "jane example", "locator": {"strategy": "text", "value": "jane example"}}
            ]
        },
    )

    record = json.loads(evidence.log_path.read_text(encoding="utf-8"))
    payload = json.dumps(record["payload"])
    assert "jane example" not in payload


def test_evidence_redacts_sensitive_role_action_names(tmp_path):
    evidence = EvidenceRecorder(tmp_path)
    evidence.event(
        "action",
        step={"action": "click", "target": {"strategy": "role", "value": "link:alice smith"}},
    )

    record = json.loads(evidence.log_path.read_text(encoding="utf-8"))
    assert "alice smith" not in json.dumps(record["payload"])


def test_evidence_redacts_unicode_role_action_names(tmp_path):
    evidence = EvidenceRecorder(tmp_path)
    evidence.event(
        "action",
        step={"action": "click", "target": {"strategy": "role", "value": "link:José Núñez"}},
    )

    record = json.loads(evidence.log_path.read_text(encoding="utf-8"))
    assert "José Núñez" not in json.dumps(record["payload"], ensure_ascii=False)


def test_evidence_redacts_uri_encoded_sensitive_values(tmp_path):
    evidence = EvidenceRecorder(tmp_path)
    evidence.sensitive_values.add("ab cd")
    evidence.event("observation", url="http://127.0.0.1:8765/member?member=ab%20cd")

    record = json.loads(evidence.log_path.read_text(encoding="utf-8"))
    assert "ab%20cd" not in json.dumps(record["payload"])


def test_redaction_masks_named_sensitive_scalar_values_including_numbers():
    value = {
        "member_id": 1001,
        "name": "Jane Example",
        "address": "12 Oak Lane",
        "balance": "$85.19",
        "count": 2,
    }

    redacted = redact_value("payload", value)

    assert redacted["member_id"] == "<REDACTED>"
    assert redacted["name"] == "<REDACTED>"
    assert redacted["address"] == "<REDACTED>"
    assert redacted["balance"] == "<REDACTED>"
    assert redacted["count"] == 2


def test_artifact_redaction_preserves_executable_fields_while_removing_url_credentials():
    payload = {
        "target": {"url": "http://demo_user:synthetic_password@127.0.0.1:8765/?member=1001"},
        "steps": [
            {
                "target": {"strategy": "label", "value": "Member Number"},
                "value": "{{member_id}}",
                "description": "Read the member number",
            }
        ],
    }

    redacted = redact_artifact_payload(payload)

    assert "demo_user" not in redacted["target"]["url"]
    assert "synthetic_password" not in redacted["target"]["url"]
    assert "member=1001" in redacted["target"]["url"]
    assert redacted["steps"][0]["target"]["value"] == "Member Number"
    assert redacted["steps"][0]["value"] == "{{member_id}}"


def test_evidence_redacts_fill_values_even_when_they_are_not_recognizable_pii(tmp_path):
    evidence = EvidenceRecorder(tmp_path)
    evidence.event(
        "human_action",
        step={"action": "fill", "value": "jane example"},
    )

    record = json.loads(evidence.log_path.read_text(encoding="utf-8"))
    assert record["payload"]["step"]["value"] == "<REDACTED>"
