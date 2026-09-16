import pytest

from cua.cli import _coerce_inputs, _parse_inputs, _remap_to_approved_demo, main
from cua.models import ParameterSpec


def test_parse_inputs_accepts_equals_in_value():
    assert _parse_inputs(["member_id=1001", "note=a=b"])["note"] == "a=b"


def test_cli_coerces_declared_integer_inputs():
    artifact = type("Artifact", (), {"parameters": {"count": ParameterSpec("integer", "Count")}})()

    assert _coerce_inputs({"count": "1002"}, artifact) == {"count": 1002}
    with pytest.raises(ValueError, match="count must be an integer"):
        _coerce_inputs({"count": "not-a-number"}, artifact)


def test_discovery_cli_pins_the_target_to_the_synthetic_origin(tmp_path):
    assert main(
        [
            "discover",
            "--target-url",
            "https://real-system.example/",
            "--member-id",
            "1001",
            "--evidence-dir",
            str(tmp_path),
        ]
    ) == 1


def test_cli_only_remaps_a_credential_free_loopback_root():
    payload = {
        "target": {
            "origin": "http://127.0.0.1:58110",
            "url": "http://127.0.0.1:58110/",
        },
        "other": "unchanged",
    }

    remapped = _remap_to_approved_demo(payload)
    assert remapped["target"] == {
        "origin": "http://127.0.0.1:8765",
        "url": "http://127.0.0.1:8765/",
    }
    with pytest.raises(ValueError):
        _remap_to_approved_demo(
            {
                "target": {
                    "origin": "http://127.0.0.1:58110",
                    "url": "http://127.0.0.1:58110/?member=1001",
                }
            }
        )
