from pathlib import Path
from dataclasses import replace
import threading
import time

import pytest

from cua.discovery import DiscoveryRunner, DiscoveryTemplate, _parameterize_text
from cua.evidence import EvidenceRecorder
from cua.handoff import HandoffCoordinator
from cua.llm import AgentAction, AgentDecision, LLMError, ScriptedDecisionClient
from cua.models import (
    ActionStep,
    ActionType,
    BusinessOutcome,
    Checkpoint,
    CheckpointKind,
    Locator,
    ParameterSpec,
    RiskClass,
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
    assert surface.actions[1][2] == "1001"
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


def test_discovery_maps_model_output_name_to_single_declared_output(tmp_path):
    client = ScriptedDecisionClient(
        [
            AgentDecision((AgentAction(ActionType.FILL, "control-0", value="1001"),)),
            AgentDecision((AgentAction(ActionType.CLICK, "control-1"),)),
            AgentDecision(
                (AgentAction(ActionType.EXTRACT, target_id="target-0", output_name="balance for 1001"),),
                done=True,
            ),
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

    assert result.status is RunStatus.SUCCESS
    assert artifact is not None
    assert set(artifact.outputs) == {"balance"}
    assert "1001" not in artifact.to_json()


def test_parameterization_does_not_replace_short_numeric_ids_inside_other_values():
    assert _parameterize_text("/member?member=1001", {"member_id": "1001"}) == "/member?member={{member_id}}"
    assert _parameterize_text("/member?member=1001", {"member_id": "10"}) == "/member?member=1001"


def test_parameterization_handles_uri_encoded_input_values():
    assert _parameterize_text(
        "/member?member=ab%20cd", {"member_id": "ab cd"}
    ) == "/member?member={{member_id}}"


def test_human_discovery_action_is_parameterized_before_artifact_recording(tmp_path):
    surface = FakeSurface()
    coordinator = HandoffCoordinator(
        surface,
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path),
    )
    runner = DiscoveryRunner(
        surface,
        ScriptedDecisionClient([]),
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path / "discovery"),
        _template(),
        parameter_values={"member_id": "1001"},
        handoff=coordinator,
    )
    request = coordinator.create_request(
        goal="goal", capability_id="cap", step="step", reason="stuck", observation=surface.observe()
    )
    coordinator.take_control(request.intervention_id, "reviewer")
    coordinator.record_human_action(
        request.intervention_id,
        ActionStep("human-fill", ActionType.FILL, Locator("label", "Member ID"), "1001"),
    )

    assert runner.recorded_steps[0].value == "{{member_id}}"


def test_discovery_escalates_a_blocked_risky_action_to_human_control(tmp_path):
    surface = FakeSurface()
    policy = GuardrailPolicy(
        allowed_origins=("http://127.0.0.1:8765",),
        risky_actions=frozenset({ActionType.CLICK}),
    )
    coordinator = HandoffCoordinator(surface, policy, EvidenceRecorder(tmp_path / "handoff"))
    client = ScriptedDecisionClient(
        [
            AgentDecision(
                (AgentAction(ActionType.CLICK, "control-1", risk=RiskClass.RISKY),)
            ),
            AgentDecision(
                (AgentAction(ActionType.EXTRACT, target_id="target-0", output_name="balance"),),
                done=True,
            ),
        ]
    )
    operator_errors = []

    def operator():
        try:
            while not coordinator.list_requests():
                time.sleep(0.01)
            request = coordinator.list_requests()[0]
            coordinator.take_control(request.intervention_id, "reviewer")
            coordinator.record_human_action(
                request.intervention_id,
                ActionStep("human-search", ActionType.CLICK, Locator("role", "button:Search")),
                confirmed=True,
            )
            coordinator.resume(request.intervention_id)
        except Exception as exc:
            operator_errors.append(exc)

    worker = threading.Thread(target=operator)
    worker.start()
    result, artifact = DiscoveryRunner(
        surface,
        client,
        policy,
        EvidenceRecorder(tmp_path / "discovery"),
        _template(),
        parameter_values={"member_id": "1001"},
        handoff=coordinator,
        handoff_wait_s=2,
    ).run("look up member 1001")
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert not operator_errors
    assert result.status is RunStatus.SUCCESS
    assert artifact is not None
    assert artifact.steps[0].id == "human-search"


@pytest.mark.parametrize("model_marks_done", [False, True])
def test_discovery_accepts_complete_human_work_at_the_step_budget_boundary(tmp_path, model_marks_done):
    surface = FakeSurface()
    policy = GuardrailPolicy.local_demo("http://127.0.0.1:8765")
    coordinator = HandoffCoordinator(surface, policy, EvidenceRecorder(tmp_path / "handoff"))
    operator_errors = []

    def operator():
        try:
            while not coordinator.list_requests():
                time.sleep(0.01)
            request = coordinator.list_requests()[0]
            coordinator.take_control(request.intervention_id, "reviewer")
            coordinator.record_human_action(
                request.intervention_id,
                ActionStep("human-fill", ActionType.FILL, Locator("label", "Member ID"), "1001"),
            )
            coordinator.record_human_action(
                request.intervention_id,
                ActionStep("human-search", ActionType.CLICK, Locator("role", "button:Search")),
            )
            coordinator.record_human_action(
                request.intervention_id,
                ActionStep("human-read", ActionType.EXTRACT, Locator("css", "#balance-value"), "balance"),
            )
            coordinator.resume(request.intervention_id)
        except Exception as exc:
            operator_errors.append(exc)

    worker = threading.Thread(target=operator)
    worker.start()
    result, artifact = DiscoveryRunner(
        surface,
        ScriptedDecisionClient([AgentDecision((), done=model_marks_done)]),
        policy,
        EvidenceRecorder(tmp_path / "discovery"),
        _template(),
        parameter_values={"member_id": "1001"},
        max_steps=1,
        handoff=coordinator,
        handoff_wait_s=2,
    ).run("look up member 1001")
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert not operator_errors
    assert result.status is RunStatus.SUCCESS
    assert artifact is not None


def test_discovery_accepts_complete_human_work_after_llm_error(tmp_path):
    surface = FakeSurface()
    policy = GuardrailPolicy.local_demo("http://127.0.0.1:8765")
    coordinator = HandoffCoordinator(surface, policy, EvidenceRecorder(tmp_path / "handoff"))
    operator_errors = []

    def operator():
        try:
            while not coordinator.list_requests():
                time.sleep(0.01)
            request = coordinator.list_requests()[0]
            coordinator.take_control(request.intervention_id, "reviewer")
            coordinator.record_human_action(
                request.intervention_id,
                ActionStep("human-fill", ActionType.FILL, Locator("label", "Member ID"), "1001"),
            )
            coordinator.record_human_action(
                request.intervention_id,
                ActionStep("human-search", ActionType.CLICK, Locator("role", "button:Search")),
            )
            coordinator.record_human_action(
                request.intervention_id,
                ActionStep("human-read", ActionType.EXTRACT, Locator("css", "#balance-value"), "balance"),
            )
            coordinator.resume(request.intervention_id)
        except Exception as exc:
            operator_errors.append(exc)

    class FailingClient:
        def decide(self, _goal, _observation):
            raise LLMError("provider unavailable")

    worker = threading.Thread(target=operator)
    worker.start()
    result, artifact = DiscoveryRunner(
        surface,
        FailingClient(),
        policy,
        EvidenceRecorder(tmp_path / "discovery"),
        _template(),
        parameter_values={"member_id": "1001"},
        max_steps=1,
        handoff=coordinator,
        handoff_wait_s=2,
    ).run("look up member 1001")
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert not operator_errors
    assert result.status is RunStatus.SUCCESS
    assert artifact is not None


def test_discovery_reports_business_outcome_after_llm_error_handoff(tmp_path):
    class NotFoundSurface(FakeSurface):
        def observe(self):
            if self.page == "not-found":
                return SurfaceObservation(self.url, "Member not found", "Member not found", ())
            return super().observe()

        def perform(self, action, locator=None, value=None, timeout_ms=5000):
            super().perform(action, locator, value, timeout_ms)
            if action is ActionType.CLICK and self.seen_member == "9999":
                self.page = "not-found"

        @property
        def seen_member(self):
            fills = [value for action, _locator, value in self.actions if action is ActionType.FILL]
            return fills[-1] if fills else None

    surface = NotFoundSurface()
    policy = GuardrailPolicy.local_demo("http://127.0.0.1:8765")
    coordinator = HandoffCoordinator(surface, policy, EvidenceRecorder(tmp_path / "handoff"))
    operator_errors = []

    def operator():
        try:
            while not coordinator.list_requests():
                time.sleep(0.01)
            request = coordinator.list_requests()[0]
            coordinator.take_control(request.intervention_id, "reviewer")
            coordinator.record_human_action(
                request.intervention_id,
                ActionStep("human-fill", ActionType.FILL, Locator("label", "Member ID"), "9999"),
            )
            coordinator.record_human_action(
                request.intervention_id,
                ActionStep("human-search", ActionType.CLICK, Locator("role", "button:Search")),
            )
            coordinator.resume(request.intervention_id)
        except Exception as exc:
            operator_errors.append(exc)

    class FailingClient:
        def decide(self, _goal, _observation):
            raise LLMError("provider unavailable")

    worker = threading.Thread(target=operator)
    worker.start()
    result, artifact = DiscoveryRunner(
        surface,
        FailingClient(),
        policy,
        EvidenceRecorder(tmp_path / "discovery"),
        _template(),
        parameter_values={"member_id": "9999"},
        max_steps=1,
        handoff=coordinator,
        handoff_wait_s=2,
    ).run("look up member 9999")
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert not operator_errors
    assert result.status is RunStatus.BUSINESS_OUTCOME
    assert result.outcome_code == "MEMBER_NOT_FOUND"
    assert artifact is None


def test_discovery_rejects_sensitive_target_query_values(tmp_path):
    template = replace(
        _template(),
        target={
            "url": "http://127.0.0.1:8765/?token=SyntheticSecret",
            "origin": "http://127.0.0.1:8765",
        },
    )
    client = ScriptedDecisionClient(
        [
            AgentDecision((AgentAction(ActionType.FILL, "control-0", value="1001"),)),
            AgentDecision((AgentAction(ActionType.CLICK, "control-1"),)),
            AgentDecision(
                (AgentAction(ActionType.EXTRACT, target_id="target-0", output_name="balance"),),
                done=True,
            ),
        ]
    )

    result, artifact = DiscoveryRunner(
        FakeSurface(),
        client,
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path),
        template,
        parameter_values={"member_id": "1001"},
    ).run("look up member 1001")

    assert result.status is RunStatus.HARD_FAILURE
    assert result.error_code == "INVALID_ARTIFACT"
    assert artifact is None
