import json
from pathlib import Path
import pytest

from cua.models import (
    ActionStep,
    ActionType,
    BusinessOutcome,
    CapabilityArtifact,
    Checkpoint,
    CheckpointKind,
    Locator,
    OutputSpec,
    ParameterSpec,
)


def test_published_schema_matches_runtime_step_and_output_contract():
    schema = json.loads(
        (Path(__file__).parents[1] / "schemas" / "capability.schema.json").read_text()
    )

    assert schema["$defs"]["output"]["properties"]["type"] == {
        "enum": ["string", "integer"]
    }
    assert len(schema["$defs"]["step"]["allOf"]) == 3


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
                Locator("css", "#balance-value"),
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
            ActionStep("read", ActionType.EXTRACT, Locator("css", "#balance-value"), "balance"),
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


def test_capability_loader_rejects_unknown_fields_and_missing_timestamp():
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
    payload["approved"] = True

    with pytest.raises(ValueError, match="unknown field"):
        CapabilityArtifact.from_dict(payload)

    payload = json.loads(artifact.to_json())
    del payload["created_at"]
    with pytest.raises(ValueError, match="required field"):
        CapabilityArtifact.from_dict(payload)


def test_locator_loader_rejects_more_than_four_fallbacks():
    payload = {
        "strategy": "css",
        "value": "#balance-value",
        "fallback": [
            {"strategy": "css", "value": "#balance-value"} for _ in range(5)
        ],
    }

    with pytest.raises(ValueError, match="at most four"):
        Locator.from_dict(payload)


def test_capability_rejects_invalid_typed_step_and_parameter_fields():
    artifact = CapabilityArtifact(
        capability_id="cap",
        name="Capability",
        description="Description",
        surface_kind="browser",
        target={"origin": "http://127.0.0.1:8765", "url": "http://127.0.0.1:8765/"},
        parameters={"member_id": ParameterSpec("string", "Member identifier")},
        outputs={},
        steps=(ActionStep("wait", ActionType.WAIT),),
        checkpoint=Checkpoint(CheckpointKind.TEXT_PRESENT, "ready", "ready"),
    )
    payload = json.loads(artifact.to_json())
    payload["steps"][0]["timeout_ms"] = 1.9
    payload["parameters"]["member_id"]["required"] = "false"

    with pytest.raises(ValueError, match="invalid capability artifact"):
        CapabilityArtifact.from_dict(payload)


def test_capability_rejects_sensitive_role_locator_names():
    artifact = CapabilityArtifact(
        capability_id="cap",
        name="Capability",
        description="Description",
        surface_kind="browser",
        target={"origin": "http://127.0.0.1:8765", "url": "http://127.0.0.1:8765/"},
        parameters={},
        outputs={},
        steps=(ActionStep("click", ActionType.CLICK, Locator("role", "button:alice smith")),),
        checkpoint=Checkpoint(CheckpointKind.TEXT_PRESENT, "ready", "ready"),
    )

    with pytest.raises(ValueError, match="sensitive text"):
        artifact.validate()


def test_capability_rejects_non_ascii_role_locator_runtime_text():
    artifact = CapabilityArtifact(
        capability_id="cap",
        name="Capability",
        description="Description",
        surface_kind="browser",
        target={"origin": "http://127.0.0.1:8765", "url": "http://127.0.0.1:8765/"},
        parameters={},
        outputs={},
        steps=(ActionStep("click", ActionType.CLICK, Locator("role", "link:José Núñez")),),
        checkpoint=Checkpoint(CheckpointKind.TEXT_PRESENT, "ready", "ready"),
    )

    with pytest.raises(ValueError, match="non-ASCII"):
        artifact.validate()


def test_capability_rejects_non_ascii_label_locator_runtime_text():
    artifact = CapabilityArtifact(
        capability_id="cap",
        name="Capability",
        description="Description",
        surface_kind="browser",
        target={"origin": "http://127.0.0.1:8765", "url": "http://127.0.0.1:8765/"},
        parameters={},
        outputs={},
        steps=(ActionStep("fill", ActionType.FILL, Locator("label", "José Núñez"), "{{member_id}}"),),
        checkpoint=Checkpoint(CheckpointKind.TEXT_PRESENT, "ready", "ready"),
    )

    with pytest.raises(ValueError, match="non-ASCII"):
        artifact.validate()


def test_capability_rejects_unsafe_free_form_metadata():
    artifact = CapabilityArtifact(
        capability_id="cap",
        name="José Núñez",
        description="Description",
        surface_kind="browser",
        target={"origin": "http://127.0.0.1:8765", "url": "http://127.0.0.1:8765/"},
        parameters={},
        outputs={},
        steps=(ActionStep("wait", ActionType.WAIT),),
        checkpoint=Checkpoint(CheckpointKind.TEXT_PRESENT, "ready", "ready"),
    )

    with pytest.raises(ValueError, match="unsafe free-form text"):
        artifact.validate()


def test_capability_rejects_query_data_in_css_link_locator():
    artifact = CapabilityArtifact(
        capability_id="cap",
        name="Capability",
        description="Description",
        surface_kind="browser",
        target={"origin": "http://127.0.0.1:8765", "url": "http://127.0.0.1:8765/"},
        parameters={},
        outputs={},
        steps=(
            ActionStep(
                "click",
                ActionType.CLICK,
                Locator("css", 'a[href="/?person=Jos%C3%A9%20N%C3%BA%C3%B1ez"]'),
            ),
        ),
        checkpoint=Checkpoint(CheckpointKind.TEXT_PRESENT, "ready", "ready"),
    )

    with pytest.raises(ValueError, match="query data"):
        artifact.validate()


def test_capability_rejects_unparameterized_target_query_values():
    artifact = CapabilityArtifact(
        capability_id="cap",
        name="Capability",
        description="Description",
        surface_kind="browser",
        target={
            "origin": "http://127.0.0.1:8765",
            "url": "http://127.0.0.1:8765/?member=alice-smith",
        },
        parameters={"member_id": ParameterSpec("string", "Member identifier")},
        outputs={},
        steps=(ActionStep("wait", ActionType.WAIT),),
        checkpoint=Checkpoint(CheckpointKind.TEXT_PRESENT, "ready", "ready"),
    )

    with pytest.raises(ValueError, match="query values must be declared placeholders"):
        artifact.validate()


def test_capability_rejects_unreviewed_path_and_query_key_data():
    for url in (
        "http://127.0.0.1:8765/member/alice-smith",
        "http://127.0.0.1:8765/member?alice-smith={{member_id}}",
    ):
        artifact = CapabilityArtifact(
            capability_id="cap",
            name="Capability",
            description="Description",
            surface_kind="browser",
            target={"origin": "http://127.0.0.1:8765", "url": url},
            parameters={"member_id": ParameterSpec("string", "Member identifier")},
            outputs={},
            steps=(ActionStep("wait", ActionType.WAIT),),
            checkpoint=Checkpoint(CheckpointKind.TEXT_PRESENT, "ready", "ready"),
        )

        with pytest.raises(ValueError):
            artifact.validate()


def test_capability_allows_parameterized_target_query_values():
    artifact = CapabilityArtifact(
        capability_id="cap",
        name="Capability",
        description="Description",
        surface_kind="browser",
        target={
            "origin": "http://127.0.0.1:8765",
            "url": "http://127.0.0.1:8765/?member={{member_id}}",
        },
        parameters={"member_id": ParameterSpec("string", "Member identifier")},
        outputs={},
        steps=(ActionStep("wait", ActionType.WAIT),),
        checkpoint=Checkpoint(CheckpointKind.TEXT_PRESENT, "ready", "ready"),
    )

    artifact.validate()


def test_capability_rejects_empty_business_outcome_fields():
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
        business_outcomes=(BusinessOutcome("", "description", "ready"),),
    )

    with pytest.raises(ValueError, match="business outcomes"):
        artifact.validate()


@pytest.mark.parametrize(
    "step",
    [
        ActionStep("navigate", ActionType.NAVIGATE),
        ActionStep("click", ActionType.CLICK),
        ActionStep("fill", ActionType.FILL, value="value"),
        ActionStep("press", ActionType.PRESS, Locator("role", "button:Search")),
    ],
)
def test_capability_rejects_steps_missing_required_contract_fields(step):
    artifact = CapabilityArtifact(
        capability_id="cap",
        name="Capability",
        description="Description",
        surface_kind="browser",
        target={"origin": "http://127.0.0.1:8765", "url": "http://127.0.0.1:8765/"},
        parameters={},
        outputs={},
        steps=(step,),
        checkpoint=Checkpoint(CheckpointKind.TEXT_PRESENT, "ready", "ready"),
    )

    with pytest.raises(ValueError, match="requires"):
        artifact.validate()


def test_capability_requires_extract_step_to_match_declared_output_source():
    artifact = CapabilityArtifact(
        capability_id="cap",
        name="Capability",
        description="Description",
        surface_kind="browser",
        target={"origin": "http://127.0.0.1:8765", "url": "http://127.0.0.1:8765/"},
        parameters={},
        outputs={"value": OutputSpec("integer", "Value", Locator("css", "#balance-value"))},
        steps=(
            ActionStep("extract", ActionType.EXTRACT, Locator("css", "#confirm-action"), "value"),
        ),
        checkpoint=Checkpoint(CheckpointKind.TEXT_PRESENT, "ready", "ready"),
    )

    with pytest.raises(ValueError, match="does not match its output source"):
        artifact.validate()


def test_capability_rejects_undeclared_placeholders_before_replay():
    artifact = CapabilityArtifact(
        capability_id="cap",
        name="Capability",
        description="Description",
        surface_kind="browser",
        target={"origin": "http://127.0.0.1:8765", "url": "http://127.0.0.1:8765/"},
        parameters={},
        outputs={},
        steps=(ActionStep("wait", ActionType.WAIT, value="{{undeclared}}"),),
        checkpoint=Checkpoint(CheckpointKind.TEXT_PRESENT, "ready", "ready"),
    )

    with pytest.raises(ValueError, match="undeclared parameter"):
        artifact.validate()
