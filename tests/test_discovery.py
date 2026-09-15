from pathlib import Path

from cua.discovery import DiscoveryRunner, DiscoveryTemplate
from cua.evidence import EvidenceRecorder
from cua.llm import AgentAction, AgentDecision, ScriptedDecisionClient
from cua.models import (
    ActionType,
    BusinessOutcome,
    Checkpoint,
    CheckpointKind,
    Locator,
    ParameterSpec,
    RunStatus,
)
from cua.policy import GuardrailPolicy
from cua.surface import Control, ReadableTarget, SurfaceObservation


class FakeSurface:
    kind = "fake"
    url = "http://127.0.0.1:8765/"

    def __init__(self):
        self.actions = []
        self.page = "home"

    def observe(self):
        if self.page == "home":
            return SurfaceObservation(
                self.url,
                "Lookup",
                "Member Services Console Member ID Search",
                (Control("control-0", "input", "Member ID", Locator("label", "Member ID")),
                 Control("control-1", "button", "Search", Locator("role", "button:Search"))),
            )
        return SurfaceObservation(
            "http://127.0.0.1:8765/member",
            "Member details",
            "Member details Current savings balance $1,240.50",
            (),
            (ReadableTarget("target-0", "$1,240.50", Locator("css", "#balance-value")),),
        )

    def perform(self, action, locator=None, value=None, timeout_ms=5000):
        self.actions.append((action, locator, value))
        if action is ActionType.CLICK:
            self.page = "detail"

    def extract(self, locator, timeout_ms=5000):
        return "$1,240.50"

    def capture(self, directory: Path, stem: str):
        screenshot = directory / f"{stem}.png"
        snapshot = directory / f"{stem}.txt"
        screenshot.write_bytes(b"not-a-real-image")
        snapshot.write_text("redacted snapshot", encoding="utf-8")
        return screenshot, snapshot


def _template():
    return DiscoveryTemplate(
        capability_id="member-balance-v1",
        name="Read member balance",
        description="Find a member and read the current savings balance.",
        target={"url": "http://127.0.0.1:8765/", "origin": "http://127.0.0.1:8765"},
        parameters={"member_id": ParameterSpec("string", "Synthetic member identifier")},
        output_descriptions={"balance": ("string", "Current savings balance", True)},
        checkpoint=Checkpoint(CheckpointKind.TEXT_PRESENT, "Member details", "details visible"),
        business_outcomes=(BusinessOutcome("MEMBER_NOT_FOUND", "No such member", "Member not found"),),
    )


def test_discovery_records_parameterized_steps_and_output(tmp_path):
    surface = FakeSurface()
    client = ScriptedDecisionClient(
        [
            AgentDecision((AgentAction(ActionType.FILL, "control-0", value="1001", reason="enter member"),)),
            AgentDecision((AgentAction(ActionType.CLICK, "control-1", reason="search"),)),
            AgentDecision(
                (AgentAction(
                    ActionType.EXTRACT,
                    target_id="target-0",
                    output_name="balance",
                    reason="read member 1001 balance",
                ),),
                done=True,
            ),
        ]
    )
    runner = DiscoveryRunner(
        surface,
        client,
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path),
        _template(),
        parameter_values={"member_id": "1001"},
    )

    result, artifact = runner.run("look up member 1001 and read the current savings balance")

    assert result.status is RunStatus.SUCCESS
    assert artifact is not None
    assert artifact.steps[1].value == "{{member_id}}" or artifact.steps[0].value == "{{member_id}}"
    assert artifact.outputs["balance"].source.strategy == "css"
    assert "1001" not in artifact.to_json()


def test_discovery_returns_business_outcome_without_crashing(tmp_path):
    class NotFoundSurface(FakeSurface):
        def observe(self):
            return SurfaceObservation(self.url, "Member not found", "Member not found", ())

    runner = DiscoveryRunner(
        NotFoundSurface(),
        ScriptedDecisionClient([]),
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path),
        _template(),
        parameter_values={"member_id": "9999"},
    )
    result, artifact = runner.run("look up member 9999")

    assert result.status is RunStatus.BUSINESS_OUTCOME
    assert result.outcome_code == "MEMBER_NOT_FOUND"
    assert artifact is None


def test_model_null_string_is_not_treated_as_a_target_id():
    decision = AgentDecision.from_dict(
        {
            "actions": [
                {
                    "action": "click",
                    "control_id": "control-1",
                    "target_id": "null",
                    "value": "null",
                    "output_name": "null",
                    "reason": "search",
                    "risk": "safe",
                }
            ],
            "done": False,
            "message": "",
        }
    )
    assert decision.actions[0].target_id is None
    assert decision.actions[0].value is None


def test_discovery_requires_all_declared_outputs_before_success(tmp_path):
    client = ScriptedDecisionClient(
        [
            AgentDecision((AgentAction(ActionType.FILL, "control-0", value="1001"),)),
            AgentDecision((AgentAction(ActionType.CLICK, "control-1"),)),
            AgentDecision((), done=True),
        ]
    )
    result, artifact = DiscoveryRunner(
        FakeSurface(),
        client,
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path),
        _template(),
        parameter_values={"member_id": "1001"},
    ).run("look up member 1001")

    assert result.status is RunStatus.HARD_FAILURE
    assert result.error_code == "OUTPUTS_MISSING"
    assert artifact is None
