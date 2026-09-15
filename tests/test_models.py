import json

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
        target={"origin": "http://127.0.0.1:8765", "route": "/"},
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
