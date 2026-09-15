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
    assert "<REDACTED>" in redacted


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
