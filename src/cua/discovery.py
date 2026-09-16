from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import re
from typing import Protocol
from urllib.parse import parse_qsl, quote, quote_plus, urlsplit, urlunsplit

from .evidence import EvidenceRecorder
from .handoff import HandoffCoordinator
from .llm import AgentAction, AgentDecision, DecisionClient, LLMError
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
from .policy import ConfirmationRequired, GuardrailPolicy, PolicyViolation, check_action_destination
from .redaction import redact_url
from .surface import SurfaceAppError, SurfaceError, SurfaceObservation, SurfaceTimeout, UnexpectedDialog


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

    def capture(self, directory: Path, stem: str) -> tuple[Path | None, Path]: ...


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
        handoff: HandoffCoordinator | None = None,
        handoff_wait_s: float = 300.0,
    ):
        self.surface = surface
        self.client = client
        self.policy = policy
        self.evidence = evidence
        self.template = template
        self.parameter_values = parameter_values
        self.max_steps = max_steps
        self.confirmed_risky = confirmed_risky
        self.handoff = handoff
        self.handoff_wait_s = handoff_wait_s
        self.recorded_steps: list[ActionStep] = []
        self.output_sources: dict[str, Locator] = {}
        self._artifact_target_url = template.target["url"]
        self._handoff_step_start = 0
        self._handoff_entry_url = template.target["url"]
        self._handoff_used = False
        self.evidence.sensitive_names.update(
            name for name, spec in self.template.output_descriptions.items() if spec[2]
        )
        self.evidence.sensitive_values.update(self.parameter_values.values())
        set_client_parameter_names = getattr(self.client, "set_parameter_names", None)
        if callable(set_client_parameter_names):
            set_client_parameter_names(set(self.parameter_values))
        set_client_task_context = getattr(self.client, "set_task_context", None)
        if callable(set_client_task_context):
            set_client_task_context(set(self.template.output_descriptions))
        set_sensitive_values = getattr(self.surface, "set_sensitive_values", None)
        if callable(set_sensitive_values):
            set_sensitive_values(set(self.parameter_values.values()))
        set_navigation_guard = getattr(self.surface, "set_navigation_guard", None)
        if callable(set_navigation_guard):
            set_navigation_guard(self.policy.check_url)
        if self.handoff is not None:
            self.handoff.on_human_action = self._record_human_action
            self.handoff.on_human_output = self._validate_human_output

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

        step_number = 0
        while True:
            while step_number < self.max_steps:
                step_number += 1
                try:
                    observation = self.surface.observe()
                except PolicyViolation as exc:
                    if self._try_handoff(goal, f"observation-{step_number}", str(exc), None):
                        completed = self._complete_after_handoff(f"observation-{step_number}")
                        if completed is not None:
                            return completed
                        continue
                    return self._failure(RunStatus.HARD_FAILURE, f"observation-{step_number}", "POLICY_BLOCKED", str(exc))
                except (UnexpectedDialog, SurfaceAppError, SurfaceTimeout, SurfaceError) as exc:
                    if self._try_handoff(goal, f"observation-{step_number}", str(exc), None):
                        completed = self._complete_after_handoff(f"observation-{step_number}")
                        if completed is not None:
                            return completed
                        continue
                    return self._surface_failure(f"observation-{step_number}", exc)
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
                ready = self._complete_if_ready(observation, f"decision-{step_number}")
                if ready is not None:
                    return ready
                try:
                    set_client_completed_outputs = getattr(self.client, "set_completed_outputs", None)
                    if callable(set_client_completed_outputs):
                        set_client_completed_outputs(set(self.output_sources))
                    decision = self.client.decide(goal, observation)
                    provenance = getattr(self.client, "provenance", lambda: {})()
                    self.evidence.event(
                        "decision",
                        step=step_number,
                        decision=_safe_decision_payload(decision),
                        **provenance,
                    )
                    for action_number, action in enumerate(decision.actions, start=1):
                        self._execute_action(step_number, action_number, action, observation)
                except LLMError as exc:
                    if self._try_handoff(goal, f"decision-{step_number}", str(exc), observation):
                        completed = self._complete_after_handoff(f"decision-{step_number}")
                        if completed is not None:
                            return completed
                        continue
                    return self._failure(RunStatus.HARD_FAILURE, f"decision-{step_number}", "LLM_ERROR", str(exc))
                except (PolicyViolation, ConfirmationRequired) as exc:
                    if self._try_handoff(goal, f"step-{step_number}", str(exc), observation):
                        completed = self._complete_after_handoff(f"step-{step_number}")
                        if completed is not None:
                            return completed
                        continue
                    return self._failure(RunStatus.HARD_FAILURE, f"step-{step_number}", "POLICY_BLOCKED", str(exc))
                except UnexpectedDialog as exc:
                    if self._try_handoff(goal, f"step-{step_number}", str(exc), observation):
                        completed = self._complete_after_handoff(f"step-{step_number}")
                        if completed is not None:
                            return completed
                        continue
                    return self._surface_failure(f"step-{step_number}", exc)
                except SurfaceAppError as exc:
                    if self._try_handoff(goal, f"step-{step_number}", str(exc), observation):
                        completed = self._complete_after_handoff(f"step-{step_number}")
                        if completed is not None:
                            return completed
                        continue
                    return self._surface_failure(f"step-{step_number}", exc)
                except SurfaceError as exc:
                    if self._try_handoff(goal, f"step-{step_number}", str(exc), observation):
                        completed = self._complete_after_handoff(f"step-{step_number}")
                        if completed is not None:
                            return completed
                        continue
                    return self._surface_failure(f"step-{step_number}", exc)

                if decision.done:
                    try:
                        current = self.surface.observe()
                    except PolicyViolation as exc:
                        return self._failure(
                            RunStatus.HARD_FAILURE,
                            f"checkpoint-{step_number}",
                            "POLICY_BLOCKED",
                            str(exc),
                        )
                    except (UnexpectedDialog, SurfaceAppError, SurfaceTimeout, SurfaceError) as exc:
                        if self._try_handoff(
                            goal,
                            f"checkpoint-{step_number}",
                            str(exc),
                            None,
                        ):
                            completed = self._complete_after_handoff(f"checkpoint-{step_number}")
                            if completed is not None:
                                return completed
                            step_number = 0
                            continue
                        return self._surface_failure(f"checkpoint-{step_number}", exc)
                    business = self._business_outcome(current)
                    if business is not None:
                        result = self._business_result(business)
                        self.evidence.event("run_finished", result=result.to_dict())
                        return result, None
                    if self._checkpoint_matches(current):
                        missing_outputs = sorted(set(self.template.output_descriptions) - set(self.output_sources))
                        if missing_outputs:
                            self.evidence.failure_snapshot(self.surface, f"failure-outputs-{step_number}")
                            return self._failure(
                                RunStatus.HARD_FAILURE,
                                f"decision-{step_number}",
                                "OUTPUTS_MISSING",
                                "discovery did not record required output(s): " + ", ".join(missing_outputs),
                            )
                        try:
                            artifact = self._artifact()
                            artifact.validate()
                        except ValueError as exc:
                            return self._failure(
                                RunStatus.HARD_FAILURE,
                                f"decision-{step_number}",
                                "INVALID_ARTIFACT",
                                str(exc),
                            )
                        self.evidence.artifact_file(artifact)
                        result = RunResult(
                            status=RunStatus.SUCCESS,
                            run_id=self.evidence.run_id,
                            outputs={"declared": sorted(self.output_sources)},
                            evidence_dir=str(self.evidence.directory),
                        )
                        self.evidence.event("run_finished", result=result.to_dict())
                        return result, artifact
                    self.evidence.failure_snapshot(self.surface, f"failure-checkpoint-{step_number}")
                    if self._try_handoff(
                        goal,
                        f"checkpoint-{step_number}",
                        "discovery checkpoint was not met",
                        current,
                    ):
                        try:
                            resumed = self.surface.observe()
                        except PolicyViolation as exc:
                            return self._failure(
                                RunStatus.HARD_FAILURE,
                                f"checkpoint-{step_number}",
                                "POLICY_BLOCKED",
                                str(exc),
                            )
                        except (UnexpectedDialog, SurfaceAppError, SurfaceTimeout, SurfaceError) as exc:
                            return self._surface_failure(f"checkpoint-{step_number}", exc)
                        completed = self._complete_after_handoff(
                            f"checkpoint-{step_number}", resumed
                        )
                        if completed is not None:
                            return completed
                        continue
                    return self._failure(
                        RunStatus.HARD_FAILURE,
                        f"decision-{step_number}",
                        "CHECKPOINT_NOT_MET",
                        self.template.checkpoint.description,
                    )

            try:
                observation = self.surface.observe()
            except PolicyViolation as exc:
                return self._failure(RunStatus.HARD_FAILURE, "max-steps", "POLICY_BLOCKED", str(exc))
            except (UnexpectedDialog, SurfaceAppError, SurfaceTimeout, SurfaceError) as exc:
                if self._try_handoff(goal, "max-steps", str(exc), None):
                    completed = self._complete_after_handoff("max-steps")
                    if completed is not None:
                        return completed
                    step_number = 0
                    continue
                return self._surface_failure("max-steps", exc)
            business = self._business_outcome(observation)
            if business is not None:
                result = self._business_result(business)
                self.evidence.event("run_finished", result=result.to_dict())
                return result, None
            if self.handoff is None or self._handoff_used:
                self.evidence.failure_snapshot(self.surface, "failure-max-steps")
                return self._failure(
                    RunStatus.ESCALATED,
                    "max-steps",
                    "MAX_STEPS",
                    "discovery did not reach the goal",
                )
            request = self.handoff.create_request(
                goal=goal,
                capability_id=self.template.capability_id,
                step="max-steps",
                reason="discovery reached its step budget without a safe completion",
                observation=observation,
            )
            self._handoff_step_start = len(self.recorded_steps)
            self._handoff_entry_url = observation.url
            self.evidence.event("run_paused", intervention_id=request.intervention_id)
            if not self.handoff.wait_for_resume(request.intervention_id, self.handoff_wait_s):
                self.evidence.failure_snapshot(self.surface, "failure-handoff-timeout")
                return self._failure(
                    RunStatus.ESCALATED,
                    "handoff",
                    "HANDOFF_TIMEOUT",
                    "human intervention was not completed before the wait expired",
                )
            self.evidence.event("run_resumed", intervention_id=request.intervention_id)
            self._start_replay_boundary()
            self._handoff_used = True
            step_number = 0
            try:
                resumed_observation = self.surface.observe()
            except PolicyViolation as exc:
                return self._failure(RunStatus.HARD_FAILURE, "post-handoff", "POLICY_BLOCKED", str(exc))
            except SurfaceError as exc:
                return self._surface_failure("post-handoff", exc)
            completed = self._complete_after_handoff("post-handoff", resumed_observation)
            if completed is not None:
                return completed

    def _execute_action(
        self,
        step_number: int,
        action_number: int,
        action: AgentAction,
        observation: SurfaceObservation,
    ) -> None:
        locator: Locator | None = None
        preferred_target = action.action is ActionType.EXTRACT
        if preferred_target and action.target_id:
            target = next((item for item in observation.readable_targets if item.ephemeral_id == action.target_id), None)
            if target is None:
                raise SurfaceError(f"unknown readable target id: {action.target_id}")
            locator = target.locator
        elif not preferred_target and action.control_id:
            control = next((item for item in observation.controls if item.ephemeral_id == action.control_id), None)
            if control is None:
                raise SurfaceError(f"unknown control id: {action.control_id}")
            locator = control.locator
        elif action.target_id:
            target = next((item for item in observation.readable_targets if item.ephemeral_id == action.target_id), None)
            if target is None:
                raise SurfaceError(f"unknown readable target id: {action.target_id}")
            locator = target.locator
        elif action.control_id:
            control = next((item for item in observation.controls if item.ephemeral_id == action.control_id), None)
            if control is None:
                raise SurfaceError(f"unknown control id: {action.control_id}")
            locator = control.locator
        if action.action is ActionType.EXTRACT:
            if locator is None or not action.output_name:
                raise SurfaceError("extract requires target_id and output_name")
            if locator.strategy == "text":
                raise SurfaceError("refusing to persist a text output locator")
            output_name = self._declared_output_name(action.output_name)
            extract_step = ActionStep(
                f"step-{step_number}-{action_number}",
                ActionType.EXTRACT,
                locator,
                output_name,
                risk=action.risk,
                description=_controlled_action_description(action.action),
            )
            self.policy.check_step(extract_step, confirmed=self.confirmed_risky)
            previous_source = self.output_sources.get(output_name)
            if previous_source is not None and previous_source != locator:
                raise SurfaceError(
                    f"output {output_name} was already recorded from a different locator"
                )
            value = self.surface.extract(locator)
            self.output_sources[output_name] = locator
            self.evidence.sensitive_values.add(value)
            set_sensitive_values = getattr(self.surface, "set_sensitive_values", None)
            if callable(set_sensitive_values):
                set_sensitive_values({value})
            self.evidence.event("extraction", name=output_name, value=value)
            self.recorded_steps.append(extract_step)
            return

        runtime_value = action.value
        if runtime_value is not None:
            parameter_match = _PARAMETER.fullmatch(runtime_value)
            if parameter_match and parameter_match.group(1) in self.parameter_values:
                runtime_value = self.parameter_values[parameter_match.group(1)]
        artifact_value = (
            _parameterize_persisted_value(runtime_value, self.parameter_values, action.action)
            if runtime_value is not None
            else None
        )
        if action.action is ActionType.NAVIGATE:
            if runtime_value is None:
                raise SurfaceError("navigate requires a value")
            self.policy.check_url(runtime_value)
        step = ActionStep(
            f"step-{step_number}-{action_number}",
            action.action,
            locator,
            artifact_value,
            risk=action.risk,
            description=_controlled_action_description(action.action),
        )
        self.policy.check_step(step, confirmed=self.confirmed_risky)
        check_action_destination(self.policy, self.surface, step)
        self.surface.perform(action.action, locator, runtime_value, step.timeout_ms)
        self.policy.check_url(self.surface.url)
        self.recorded_steps.append(step)
        self.evidence.event(
            "action",
            step=step.to_dict(),
            reason=_controlled_action_description(action.action),
        )

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

    def _business_result(self, outcome: BusinessOutcome) -> RunResult:
        return RunResult(
            status=RunStatus.BUSINESS_OUTCOME,
            run_id=self.evidence.run_id,
            outcome_code=outcome.code,
            message=outcome.description,
            evidence_dir=str(self.evidence.directory),
        )

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
            target={
                **self.template.target,
                "url": self._safe_target_url(),
            },
            parameters=self.template.parameters,
            outputs=outputs,
            steps=tuple(self.recorded_steps),
            checkpoint=self.template.checkpoint,
            business_outcomes=self.template.business_outcomes,
        )

    def _declared_output_name(self, name: str) -> str:
        if name in self.template.output_descriptions:
            return name
        if len(self.template.output_descriptions) == 1:
            return next(iter(self.template.output_descriptions))
        raise SurfaceError(f"extract output is not declared: {name}")

    def _safe_target_url(self) -> str:
        parameterized = _parameterize_url(self._artifact_target_url, self.parameter_values)
        redacted = redact_url(parameterized)
        if redacted != parameterized:
            raise ValueError("target URL contains credentials or an unparameterized secret")
        return parameterized

    def _record_human_action(self, step: ActionStep) -> None:
        if step.action is ActionType.EXTRACT:
            output_name = self._declared_output_name(step.value or "")
            if step.target is None:
                raise SurfaceError("human extract requires a target")
            self.output_sources[output_name] = step.target
            recorded = replace(step, value=output_name)
        else:
            recorded = replace(
                step,
                value=(
                    _parameterize_persisted_value(step.value, self.parameter_values, step.action)
                    if step.value is not None
                    else None
                ),
                description=_controlled_action_description(step.action),
            )
        self.recorded_steps.append(recorded)

    def _validate_human_output(self, step: ActionStep, value: str) -> None:
        if step.target is not None and step.target.strategy == "text":
            raise SurfaceError("refusing to persist a text human output locator")

    def _try_handoff(
        self,
        goal: str,
        step: str,
        reason: str,
        observation: SurfaceObservation | None,
    ) -> bool:
        if self.handoff is None or self._handoff_used:
            return False
        self._handoff_step_start = len(self.recorded_steps)
        if observation is None:
            try:
                observation = self.surface.observe()
            except (PolicyViolation, SurfaceError):
                observation = SurfaceObservation(self.surface.url, "", "", ())
        self._handoff_entry_url = observation.url
        request = self.handoff.create_request(
            goal=goal,
            capability_id=self.template.capability_id,
            step=step,
            reason=reason,
            observation=observation,
        )
        self.evidence.event("run_paused", intervention_id=request.intervention_id)
        if not self.handoff.wait_for_resume(request.intervention_id, self.handoff_wait_s):
            return False
        self.evidence.event("run_resumed", intervention_id=request.intervention_id)
        self._start_replay_boundary()
        self._handoff_used = True
        return True

    def _start_replay_boundary(self) -> None:
        """Start the artifact from the state reached after human intervention."""

        post_handoff_steps = self.recorded_steps[self._handoff_step_start :]
        entry_url = self._handoff_entry_url
        for step in post_handoff_steps:
            if step.action is ActionType.NAVIGATE and step.value:
                entry_url = step.value
                break
        self._artifact_target_url = entry_url
        self.output_sources = {
            step.value: self.output_sources[step.value]
            for step in post_handoff_steps
            if step.action is ActionType.EXTRACT
            and step.value in self.output_sources
        }
        anchor = ActionStep(
            "handoff-anchor",
            ActionType.NAVIGATE,
            value=self._safe_target_url(),
            description="Replay from the allowlisted state reached after human handoff.",
        )
        self.recorded_steps = [
            anchor,
            *post_handoff_steps,
        ]

    def _complete_after_handoff(
        self, step: str, observation: SurfaceObservation | None = None
    ) -> tuple[RunResult, CapabilityArtifact | None] | None:
        if observation is None:
            try:
                observation = self.surface.observe()
            except (PolicyViolation, UnexpectedDialog, SurfaceAppError, SurfaceTimeout, SurfaceError):
                return None
        business = self._business_outcome(observation)
        if business is not None:
            result = self._business_result(business)
            self.evidence.event("run_finished", result=result.to_dict())
            return result, None
        if not self._checkpoint_matches(observation):
            return None
        missing_outputs = sorted(set(self.template.output_descriptions) - set(self.output_sources))
        if missing_outputs:
            return None
        try:
            artifact = self._artifact()
            artifact.validate()
            self.evidence.artifact_file(artifact)
        except ValueError as exc:
            return self._failure(RunStatus.HARD_FAILURE, step, "INVALID_ARTIFACT", str(exc))
        result = RunResult(
            status=RunStatus.SUCCESS,
            run_id=self.evidence.run_id,
            outputs={"declared": sorted(self.output_sources)},
            evidence_dir=str(self.evidence.directory),
        )
        self.evidence.event("run_finished", result=result.to_dict())
        return result, artifact

    def _complete_if_ready(
        self, observation: SurfaceObservation, step: str
    ) -> tuple[RunResult, CapabilityArtifact | None] | None:
        if not self.recorded_steps:
            return None
        if not self._checkpoint_matches(observation):
            return None
        missing_outputs = sorted(set(self.template.output_descriptions) - set(self.output_sources))
        if missing_outputs:
            return None
        try:
            artifact = self._artifact()
            artifact.validate()
            self.evidence.artifact_file(artifact)
        except ValueError as exc:
            return self._failure(RunStatus.HARD_FAILURE, step, "INVALID_ARTIFACT", str(exc))
        result = RunResult(
            status=RunStatus.SUCCESS,
            run_id=self.evidence.run_id,
            outputs={"declared": sorted(self.output_sources)},
            evidence_dir=str(self.evidence.directory),
        )
        self.evidence.event("run_finished", result=result.to_dict())
        return result, artifact

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

    def _surface_failure(self, step: str, exc: SurfaceError) -> tuple[RunResult, None]:
        self.evidence.failure_snapshot(self.surface, f"failure-{step}")
        if isinstance(exc, UnexpectedDialog):
            code = "UNEXPECTED_DIALOG"
        elif isinstance(exc, SurfaceAppError):
            code = "APP_ERROR"
        elif isinstance(exc, SurfaceTimeout):
            code = "TIMEOUT"
        else:
            code = "SURFACE_ERROR"
        status = RunStatus.RECOVERABLE_FAILURE if isinstance(exc, SurfaceTimeout) else RunStatus.HARD_FAILURE
        return self._failure(status, step, code, str(exc))


def _controlled_action_description(action: ActionType) -> str:
    return {
        ActionType.NAVIGATE: "Navigate to the approved destination.",
        ActionType.CLICK: "Click the identified control.",
        ActionType.FILL: "Fill the identified control.",
        ActionType.PRESS: "Press the identified control.",
        ActionType.WAIT: "Wait for the bounded timeout.",
        ActionType.EXTRACT: "Extract the declared output.",
    }[action]


def _safe_decision_payload(decision: AgentDecision) -> dict[str, object]:
    payload = decision.to_dict()
    payload["message"] = "<REDACTED>"
    for action in payload.get("actions", []):
        if isinstance(action, dict):
            action["reason"] = "<REDACTED>"
    return payload


def _parameterize_text(value: str, parameter_values: dict[str, str]) -> str:
    result = value
    for name, actual in sorted(parameter_values.items(), key=lambda item: len(item[1]), reverse=True):
        if not actual:
            continue
        replacement = "{{" + name + "}}"
        candidates = sorted({actual, quote(actual, safe=""), quote_plus(actual)}, key=len, reverse=True)
        for candidate in candidates:
            if candidate == actual and actual.isdigit():
                result = re.sub(rf"(?<!\d){re.escape(actual)}(?!\d)", replacement, result)
            else:
                result = result.replace(candidate, replacement)
    return result


def _parameterize_persisted_value(
    value: str,
    parameter_values: dict[str, str],
    action: ActionType,
) -> str:
    result = (
        _parameterize_url(value, parameter_values)
        if action is ActionType.NAVIGATE
        else _parameterize_text(value, parameter_values)
    )
    if action is ActionType.FILL and not _PARAMETER.fullmatch(result):
        if value not in parameter_values.values():
            raise SurfaceError("refusing to persist an unparameterized fill value")
    if action is ActionType.NAVIGATE and redact_url(result) != result:
        raise SurfaceError("refusing to persist a navigation containing credentials or a secret")
    return result


_PARAMETER = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")


def _parameterize_url(value: str, parameter_values: dict[str, str]) -> str:
    parsed = urlsplit(value)
    if not parsed.netloc and not parsed.path.startswith("/"):
        return _parameterize_text(value, parameter_values)
    query = "&".join(
        f"{quote(key, safe='')}={quote(_parameterize_text(item, parameter_values), safe='{}')}"
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
    )
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            query,
            parsed.fragment,
        )
    )
