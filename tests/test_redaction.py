from cua.redaction import redact_text, redact_value


def test_redaction_handles_bearer_and_key_formats_without_crashing():
    value = "Authorization: Bearer abc.def.ghi api_key=secret-value password: hunter2"

    redacted = redact_text(value)

    assert "abc.def.ghi" not in redacted
    assert "secret-value" not in redacted
    assert "hunter2" not in redacted
    assert "<REDACTED>" in redacted


def test_redaction_masks_named_sensitive_scalar_values_including_numbers():
    value = {"member_id": 1001, "name": "Jane Example", "balance": "$85.19", "count": 2}

    redacted = redact_value("payload", value)

    assert redacted["member_id"] == "<REDACTED>"
    assert redacted["name"] == "<REDACTED>"
    assert redacted["balance"] == "<REDACTED>"
    assert redacted["count"] == 2
