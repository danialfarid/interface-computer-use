import json
import pytest

from cua.models import (
    ActionStep,
    ActionType,
    CapabilityArtifact,
    Checkpoint,
    CheckpointKind,
    Locator,
    OutputSpec,
    ParameterSpec,
)


def test_capability_round_trips_without_runtime_values():
    artifact = CapabilityArtifact(
        capability_id="member-balance-v1",
        name="Read member balance",
        description="Find a member and read the displayed balance.",
        surface_kind="browser",
        target={"origin": "http://127.0.0.1:8765", "url": "http://127.0.0.1:8765/"},
        parameters={"member_id": ParameterSpec("string", "Member identifier")},
        outputs={
            "balance": OutputSpec(
                "string",
                "Displayed balance",
                Locator("text", "balance-value"),
            )
        },
        steps=(
            ActionStep(
                "fill-member",
                ActionType.FILL,
                Locator("label", "Member ID"),
                "{{member_id}}",
            ),
            ActionStep("search", ActionType.CLICK, Locator("role", "button:Search")),
            ActionStep("read", ActionType.EXTRACT, Locator("text", "balance-value"), "balance"),
        ),
        checkpoint=Checkpoint(
            CheckpointKind.TEXT_PRESENT,
            "Member details",
            "Details panel is visible",
        ),
    )

    loaded = CapabilityArtifact.from_dict(json.loads(artifact.to_json()))

    assert loaded == artifact
    assert "member_id" in loaded.to_json()
    assert "12345" not in loaded.to_json()


def test_capability_rejects_unknown_schema_version():
    artifact = CapabilityArtifact(
        capability_id="cap",
        name="Capability",
        description="Description",
        surface_kind="browser",
        target={"origin": "http://127.0.0.1:8765", "url": "http://127.0.0.1:8765/"},
        parameters={},
        outputs={},
        steps=(ActionStep("wait", ActionType.WAIT),),
        checkpoint=Checkpoint(CheckpointKind.TEXT_PRESENT, "ready", "ready"),
    )
    payload = json.loads(artifact.to_json())
    payload["schema_version"] = "99.0"

    with pytest.raises(ValueError, match="unsupported capability schema version"):
        CapabilityArtifact.from_dict(payload)


def test_capability_normalizes_missing_contract_fields_to_value_error():
    artifact = CapabilityArtifact(
        capability_id="cap",
        name="Capability",
        description="Description",
        surface_kind="browser",
        target={"origin": "http://127.0.0.1:8765", "url": "http://127.0.0.1:8765/"},
        parameters={},
        outputs={},
        steps=(ActionStep("wait", ActionType.WAIT),),
        checkpoint=Checkpoint(CheckpointKind.TEXT_PRESENT, "ready", "ready"),
    )
    payload = json.loads(artifact.to_json())
    del payload["checkpoint"]

    with pytest.raises(ValueError, match="invalid capability artifact"):
        CapabilityArtifact.from_dict(payload)


def test_capability_requires_extract_step_to_match_declared_output_source():
    artifact = CapabilityArtifact(
        capability_id="cap",
        name="Capability",
        description="Description",
        surface_kind="browser",
        target={"origin": "http://127.0.0.1:8765", "url": "http://127.0.0.1:8765/"},
        parameters={},
        outputs={"value": OutputSpec("integer", "Value", Locator("css", "#one"))},
        steps=(
            ActionStep("extract", ActionType.EXTRACT, Locator("css", "#two"), "value"),
        ),
        checkpoint=Checkpoint(CheckpointKind.TEXT_PRESENT, "ready", "ready"),
    )

    with pytest.raises(ValueError, match="does not match its output source"):
        artifact.validate()
