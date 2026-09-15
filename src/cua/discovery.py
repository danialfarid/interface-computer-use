from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
import uuid

from .evidence import EvidenceRecorder
from .llm import AgentAction, DecisionClient, LLMError
from .models import (
    ActionStep,
    ActionType,
    BusinessOutcome,
    CapabilityArtifact,
    Checkpoint,
    Locator,
    OutputSpec,
    ParameterSpec,
    RiskClass,
    RunResult,
    RunStatus,
)
from .policy import ConfirmationRequired, GuardrailPolicy, PolicyViolation
from .surface import SurfaceError, SurfaceObservation


class DiscoverySurface(Protocol):
    kind: str
    url: str

    def observe(self) -> SurfaceObservation: ...

    def perform(
        self,
        action: ActionType,
        locator: Locator | None = None,
        value: str | None = None,
        timeout_ms: int = 5_000,
    ) -> None: ...

    def extract(self, locator: Locator, timeout_ms: int = 5_000) -> str: ...

    def capture(self, directory: Path, stem: str) -> tuple[Path, Path]: ...


@dataclass(frozen=True)
class DiscoveryTemplate:
    capability_id: str
    name: str
    description: str
    target: dict[str, str]
    parameters: dict[str, ParameterSpec]
    output_descriptions: dict[str, tuple[str, str, bool]]
    checkpoint: Checkpoint
    business_outcomes: tuple[BusinessOutcome, ...] = ()


class DiscoveryRunner:
    def __init__(
        self,
        surface: DiscoverySurface,
        client: DecisionClient,
        policy: GuardrailPolicy,
        evidence: EvidenceRecorder,
        template: DiscoveryTemplate,
        *,
        parameter_values: dict[str, str],
        max_steps: int = 12,
        confirmed_risky: bool = False,
    ):
        self.surface = surface
        self.client = client
        self.policy = policy
        self.evidence = evidence
        self.template = template
        self.parameter_values = parameter_values
        self.max_steps = max_steps
        self.confirmed_risky = confirmed_risky
        self.recorded_steps: list[ActionStep] = []
        self.output_sources: dict[str, Locator] = {}

    def run(self, goal: str) -> tuple[RunResult, CapabilityArtifact | None]:
        self.evidence.event("run_started", mode="discovery", goal=goal, target=self.template.target)
        target_url = self.template.target["url"]
        try:
            self.policy.check_url(target_url)
            navigate = ActionStep("navigate", ActionType.NAVIGATE, value=target_url)
            self.policy.check_step(navigate, confirmed=self.confirmed_risky)
            self.surface.perform(ActionType.NAVIGATE, value=target_url)
            self.evidence.event("action", step=navigate.to_dict(), reason="open target")
        except (PolicyViolation, ConfirmationRequired, SurfaceError) as exc:
            return self._failure(RunStatus.HARD_FAILURE, "navigate", "START_FAILED", str(exc))

        for step_number in range(1, self.max_steps + 1):
            observation = self.surface.observe()
            self.evidence.event("observation", step=step_number, observation=observation.to_dict())
            business = self._business_outcome(observation)
            if business is not None:
                result = RunResult(
                    status=RunStatus.BUSINESS_OUTCOME,
                    run_id=self.evidence.run_id,
                    outcome_code=business.code,
                    message=business.description,
                    evidence_dir=str(self.evidence.directory),
                )
                self.evidence.event("run_finished", result=result.to_dict())
                return result, None
            try:
                decision = self.client.decide(goal, observation)
                self.evidence.event("decision", step=step_number, decision=decision.to_dict())
            except LLMError as exc:
                return self._failure(RunStatus.HARD_FAILURE, f"decision-{step_number}", "LLM_ERROR", str(exc))

            try:
                for action_number, action in enumerate(decision.actions, start=1):
                    self._execute_action(step_number, action_number, action, observation)
            except (PolicyViolation, ConfirmationRequired) as exc:
                return self._failure(RunStatus.HARD_FAILURE, f"step-{step_number}", "POLICY_BLOCKED", str(exc))
            except SurfaceError as exc:
                self.evidence.failure_snapshot(self.surface, f"failure-step-{step_number}")
                return self._failure(RunStatus.HARD_FAILURE, f"step-{step_number}", "SURFACE_ERROR", str(exc))

            if decision.done:
                current = self.surface.observe()
                if self._checkpoint_matches(current):
                    artifact = self._artifact()
                    self.evidence.json_file("artifact.json", artifact.to_dict())
                    result = RunResult(
                        status=RunStatus.SUCCESS,
                        run_id=self.evidence.run_id,
                        outputs={"declared": sorted(self.output_sources)},
                        evidence_dir=str(self.evidence.directory),
                    )
                    self.evidence.event("run_finished", result=result.to_dict())
                    return result, artifact
                return self._failure(
                    RunStatus.HARD_FAILURE,
                    f"decision-{step_number}",
                    "CHECKPOINT_NOT_MET",
                    self.template.checkpoint.description,
                )

        self.evidence.failure_snapshot(self.surface, "failure-max-steps")
        return self._failure(RunStatus.ESCALATED, "max-steps", "MAX_STEPS", "discovery did not reach the goal")

    def _execute_action(
        self,
        step_number: int,
        action_number: int,
        action: AgentAction,
        observation: SurfaceObservation,
    ) -> None:
        locator: Locator | None = None
        if action.control_id:
            control = next((item for item in observation.controls if item.ephemeral_id == action.control_id), None)
            if control is None:
                raise SurfaceError(f"unknown control id: {action.control_id}")
            locator = control.locator
        if action.target_id:
            target = next((item for item in observation.readable_targets if item.ephemeral_id == action.target_id), None)
            if target is None:
                raise SurfaceError(f"unknown readable target id: {action.target_id}")
            locator = target.locator
        if action.action is ActionType.EXTRACT:
            if locator is None or not action.output_name:
                raise SurfaceError("extract requires target_id and output_name")
            self.output_sources[action.output_name] = locator
            value = self.surface.extract(locator)
            self.evidence.event("extraction", name=action.output_name, value=value)
            self.recorded_steps.append(
                ActionStep(
                    f"step-{step_number}-{action_number}",
                    ActionType.EXTRACT,
                    locator,
                    action.output_name,
                    description=action.reason,
                )
            )
            return

        value = _parameterize(action.value, self.parameter_values)
        if action.action is ActionType.NAVIGATE:
            if value is None:
                raise SurfaceError("navigate requires a value")
            self.policy.check_url(value)
        step = ActionStep(
            f"step-{step_number}-{action_number}",
            action.action,
            locator,
            value,
            risk=action.risk,
            description=action.reason,
        )
        self.policy.check_step(step, confirmed=self.confirmed_risky)
        self.surface.perform(action.action, locator, action.value, step.timeout_ms)
        self.recorded_steps.append(step)
        self.evidence.event("action", step=step.to_dict(), reason=action.reason)

    def _checkpoint_matches(self, observation: SurfaceObservation) -> bool:
        checkpoint = self.template.checkpoint
        if checkpoint.kind.value == "text_present":
            return checkpoint.value in observation.text
        if checkpoint.kind.value == "url_prefix":
            return observation.url.startswith(checkpoint.value)
        return False

    def _business_outcome(self, observation: SurfaceObservation) -> BusinessOutcome | None:
        for outcome in self.template.business_outcomes:
            if outcome.detection_text.lower() in observation.text.lower():
                return outcome
        return None

    def _artifact(self) -> CapabilityArtifact:
        outputs = {}
        for name, locator in self.output_sources.items():
            output_type, description, sensitive = self.template.output_descriptions.get(
                name, ("string", name, True)
            )
            outputs[name] = OutputSpec(output_type, description, locator, sensitive)
        return CapabilityArtifact(
            capability_id=self.template.capability_id,
            name=self.template.name,
            description=self.template.description,
            surface_kind=self.surface.kind,
            target=self.template.target,
            parameters=self.template.parameters,
            outputs=outputs,
            steps=tuple(self.recorded_steps),
            checkpoint=self.template.checkpoint,
            business_outcomes=self.template.business_outcomes,
        )

    def _failure(self, status: RunStatus, step: str, code: str, message: str) -> tuple[RunResult, None]:
        result = RunResult(
            status=status,
            run_id=self.evidence.run_id,
            failed_step=step,
            error_code=code,
            message=message,
            evidence_dir=str(self.evidence.directory),
        )
        self.evidence.event("run_finished", result=result.to_dict())
        return result, None


def _parameterize(value: str | None, parameter_values: dict[str, str]) -> str | None:
    if value is None:
        return None
    for name, actual in parameter_values.items():
        if value == actual:
            return "{{" + name + "}}"
    return value
