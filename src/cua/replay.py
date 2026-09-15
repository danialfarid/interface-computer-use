from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Protocol

from .evidence import EvidenceRecorder
from .handoff import HandoffCoordinator
from .models import ActionStep, ActionType, CapabilityArtifact, Locator, RunResult, RunStatus
from .policy import ConfirmationRequired, GuardrailPolicy, PolicyViolation, check_action_destination
from .surface import SurfaceAppError, SurfaceError, SurfaceObservation, SurfaceTimeout, UnexpectedDialog


class ReplaySurface(Protocol):
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


class InputValidationError(ValueError):
    pass


class ReplayRunner:
    """Execute a saved capability without asking a model what to do next."""

    def __init__(
        self,
        surface: ReplaySurface,
        policy: GuardrailPolicy,
        evidence: EvidenceRecorder,
        artifact: CapabilityArtifact,
        *,
        inputs: dict[str, Any],
        confirmed_risky: bool = False,
        max_retries: int = 2,
        handoff: HandoffCoordinator | None = None,
        handoff_wait_s: float = 300.0,
    ):
        self.surface = surface
        self.policy = policy
        self.evidence = evidence
        self.artifact = artifact
        self.inputs = inputs
        self.confirmed_risky = confirmed_risky
        self.max_retries = max_retries
        self.handoff = handoff
        self.handoff_wait_s = handoff_wait_s
        self._handoff_used = False

    def run(self) -> RunResult:
        self.evidence.event(
            "run_started",
            mode="replay",
            capability_id=self.artifact.capability_id,
            artifact_version=self.artifact.artifact_version,
            inputs=self.inputs,
        )
        try:
            self.artifact.validate()
        except ValueError as exc:
            return self._finish(
                RunResult(
                    RunStatus.HARD_FAILURE,
                    self.evidence.run_id,
                    failed_step="artifact-validation",
                    error_code="INVALID_ARTIFACT",
                    message=str(exc),
                    evidence_dir=str(self.evidence.directory),
                )
            )
        try:
            self._validate_inputs()
        except InputValidationError as exc:
            return self._finish(
                RunResult(
                    RunStatus.HARD_FAILURE,
                    self.evidence.run_id,
                    failed_step="input-validation",
                    error_code="INVALID_INPUT",
                    message=str(exc),
                    evidence_dir=str(self.evidence.directory),
                )
            )

        outputs: dict[str, Any] = {}
        for step in self.artifact.steps:
            try:
                observation = self.surface.observe()
            except SurfaceError as exc:
                if self._try_handoff(step, exc):
                    continue
                return self._surface_failure(step, exc)
            business = self._business_outcome(observation)
            if business is not None:
                return self._finish(
                    RunResult(
                        RunStatus.BUSINESS_OUTCOME,
                        self.evidence.run_id,
                        outcome_code=business[0],
                        message=business[1],
                        evidence_dir=str(self.evidence.directory),
                    )
                )
            try:
                self._run_step(step, outputs)
            except (PolicyViolation, ConfirmationRequired) as exc:
                if self._try_handoff(step, exc):
                    continue
                return self._failure(step, "POLICY_BLOCKED", str(exc))
            except InputValidationError as exc:
                return self._failure(step, "INVALID_INPUT", str(exc))
            except UnexpectedDialog as exc:
                if self._try_handoff(step, exc):
                    continue
                return self._surface_failure(step, exc)
            except SurfaceAppError as exc:
                if self._try_handoff(step, exc):
                    continue
                return self._surface_failure(step, exc)
            except SurfaceTimeout as exc:
                if self._try_handoff(step, exc):
                    continue
                return self._surface_failure(step, exc)
            except SurfaceError as exc:
                if self._try_handoff(step, exc):
                    continue
                return self._surface_failure(step, exc)

        try:
            observation = self.surface.observe()
        except SurfaceError as exc:
            if self._try_handoff(ActionStep("final-observation", ActionType.WAIT), exc):
                try:
                    observation = self.surface.observe()
                except SurfaceError as second_exc:
                    return self._surface_failure(ActionStep("final-observation", ActionType.WAIT), second_exc)
            else:
                return self._surface_failure(ActionStep("final-observation", ActionType.WAIT), exc)
        business = self._business_outcome(observation)
        if business is not None:
            return self._finish(
                RunResult(
                    RunStatus.BUSINESS_OUTCOME,
                    self.evidence.run_id,
                    outcome_code=business[0],
                    message=business[1],
                    evidence_dir=str(self.evidence.directory),
                )
            )
        missing_outputs = sorted(set(self.artifact.outputs) - set(outputs))
        if missing_outputs:
            self.evidence.failure_snapshot(self.surface, "failure-outputs")
            return self._failure(
                ActionStep("outputs", ActionType.WAIT),
                "OUTPUTS_MISSING",
                "replay did not produce declared output(s): " + ", ".join(missing_outputs),
            )
        if not self._checkpoint_matches(observation):
            self.evidence.failure_snapshot(self.surface, "failure-checkpoint")
            return self._failure(
                ActionStep("checkpoint", ActionType.WAIT),
                "CHECKPOINT_NOT_MET",
                self.artifact.checkpoint.description,
            )
        result = RunResult(
            RunStatus.SUCCESS,
            self.evidence.run_id,
            outputs=outputs,
            evidence_dir=str(self.evidence.directory),
        )
        return self._finish(result)

    def _run_step(self, step: ActionStep, outputs: dict[str, Any]) -> None:
        self.policy.check_step(step, confirmed=self.confirmed_risky)
        value = _resolve_value(step.value, self.inputs)
        if step.action is ActionType.NAVIGATE:
            if value is None:
                raise InputValidationError(f"step {step.id} has no navigation URL")
            self.policy.check_url(value)
        if step.action is ActionType.EXTRACT:
            if step.target is None or not step.value:
                raise SurfaceError(f"extract step {step.id} is incomplete")
            for attempt in range(self.max_retries + 1):
                try:
                    extracted = self.surface.extract(step.target, step.timeout_ms)
                    outputs[step.value] = _coerce_output(
                        self.artifact.outputs[step.value].type,
                        extracted,
                        step.value,
                    )
                    self.evidence.event(
                        "extraction", step=step.id, name=step.value, value=extracted, attempt=attempt + 1
                    )
                    return
                except SurfaceTimeout:
                    if attempt < self.max_retries:
                        self.evidence.event(
                            "recoverable", step=step.id, code="TRANSIENT_TIMEOUT", attempt=attempt + 1
                        )
                        continue
                    raise

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                check_action_destination(self.policy, self.surface, step)
                self.surface.perform(step.action, step.target, value, step.timeout_ms)
                self.policy.check_url(self.surface.url)
                self.evidence.event("action", step=step.to_dict(), attempt=attempt + 1)
                return
            except SurfaceTimeout as exc:
                last_error = exc
                if attempt < self.max_retries:
                    self.evidence.event("recoverable", step=step.id, code="TRANSIENT_TIMEOUT", attempt=attempt + 1)
                    continue
                raise
            except SurfaceError as exc:
                last_error = exc
                break
        assert last_error is not None
        raise last_error

    def _validate_inputs(self) -> None:
        expected = set(self.artifact.parameters)
        provided = set(self.inputs)
        missing = sorted(name for name in expected if self.artifact.parameters[name].required and name not in provided)
        unknown = sorted(provided - expected)
        if missing:
            raise InputValidationError(f"missing required input(s): {', '.join(missing)}")
        if unknown:
            raise InputValidationError(f"unknown input(s): {', '.join(unknown)}")
        for name, spec in self.artifact.parameters.items():
            if name not in self.inputs:
                continue
            value = self.inputs[name]
            if spec.type == "string" and not isinstance(value, str):
                raise InputValidationError(f"{name} must be a string")
            if spec.type == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
                raise InputValidationError(f"{name} must be an integer")

    def _checkpoint_matches(self, observation: SurfaceObservation) -> bool:
        checkpoint = self.artifact.checkpoint
        if checkpoint.kind.value == "text_present":
            return checkpoint.value in observation.text
        if checkpoint.kind.value == "url_prefix":
            return observation.url.startswith(checkpoint.value)
        return False

    def _business_outcome(self, observation: SurfaceObservation) -> tuple[str, str] | None:
        for outcome in self.artifact.business_outcomes:
            if outcome.detection_text.lower() in observation.text.lower():
                return outcome.code, outcome.description
        return None

    def _failure(
        self,
        step: ActionStep,
        code: str,
        message: str,
        status: RunStatus = RunStatus.HARD_FAILURE,
    ) -> RunResult:
        return self._finish(
            RunResult(
                status,
                self.evidence.run_id,
                failed_step=step.id,
                error_code=code,
                message=message,
                evidence_dir=str(self.evidence.directory),
            )
        )

    def _surface_failure(self, step: ActionStep, exc: SurfaceError) -> RunResult:
        self.evidence.failure_snapshot(self.surface, f"failure-{step.id}")
        if isinstance(exc, UnexpectedDialog):
            code = "UNEXPECTED_DIALOG"
        elif isinstance(exc, SurfaceAppError):
            code = "APP_ERROR"
        elif isinstance(exc, SurfaceTimeout):
            code = "TIMEOUT"
        else:
            code = "SURFACE_ERROR"
        status = RunStatus.RECOVERABLE_FAILURE if isinstance(exc, SurfaceTimeout) else RunStatus.HARD_FAILURE
        return self._failure(step, code, str(exc), status)

    def _try_handoff(self, step: ActionStep, reason: Exception) -> bool:
        if self.handoff is None or self._handoff_used:
            return False
        try:
            observation = self.surface.observe()
        except SurfaceError:
            observation = SurfaceObservation(self.surface.url, "", "", ())
        request = self.handoff.create_request(
            goal=f"replay capability {self.artifact.name}",
            capability_id=self.artifact.capability_id,
            step=step.id,
            reason=str(reason),
            observation=observation,
        )
        self.evidence.event("run_paused", intervention_id=request.intervention_id)
        if not self.handoff.wait_for_resume(request.intervention_id, self.handoff_wait_s):
            return False
        self.evidence.event("run_resumed", intervention_id=request.intervention_id)
        self._handoff_used = True
        return True

    def _finish(self, result: RunResult) -> RunResult:
        self.evidence.event("run_finished", result=result.to_dict())
        return result


def _coerce_output(output_type: str, value: str, name: str) -> Any:
    if output_type == "string":
        return value
    if output_type == "integer":
        try:
            return int(value.strip())
        except (AttributeError, ValueError) as exc:
            raise SurfaceError(f"output {name} is not a valid integer: {value!r}") from exc
    raise SurfaceError(f"unsupported output type for {name}: {output_type}")


_PARAMETER = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")


def _resolve_value(value: str | None, inputs: dict[str, Any]) -> str | None:
    if value is None:
        return None

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in inputs:
            raise InputValidationError(f"step references missing input: {name}")
        return str(inputs[name])

    return _PARAMETER.sub(replace, value)
