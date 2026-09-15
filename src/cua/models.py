from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import json
from typing import Any, Mapping

from .redaction import redact_text


SUPPORTED_SCHEMA_VERSION = "1.0"


class ActionType(StrEnum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    PRESS = "press"
    WAIT = "wait"
    EXTRACT = "extract"


class RiskClass(StrEnum):
    SAFE = "safe"
    RISKY = "risky"


class CheckpointKind(StrEnum):
    TEXT_PRESENT = "text_present"
    URL_PREFIX = "url_prefix"


@dataclass(frozen=True)
class Locator:
    """A reviewable locator with an explicit primary and optional fallback."""

    SUPPORTED_STRATEGIES = frozenset({"label", "text", "css", "role"})

    strategy: str
    value: str
    fallback: tuple["Locator", ...] = ()
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "strategy": self.strategy,
            "value": self.value,
        }
        if self.fallback:
            result["fallback"] = [item.to_dict() for item in self.fallback]
        if self.rationale:
            result["rationale"] = self.rationale
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Locator":
        if not isinstance(value, Mapping):
            raise ValueError("locator must be an object")
        strategy = value.get("strategy")
        locator_value = value.get("value")
        if not isinstance(strategy, str) or strategy not in cls.SUPPORTED_STRATEGIES:
            raise ValueError(f"unsupported locator strategy: {strategy!r}")
        if not isinstance(locator_value, str) or not locator_value:
            raise ValueError("locator value must be a non-empty string")
        fallback = value.get("fallback", [])
        rationale = value.get("rationale", "")
        if not isinstance(fallback, list):
            raise ValueError("locator fallback must be a list")
        if not isinstance(rationale, str):
            raise ValueError("locator rationale must be a string")
        return cls(
            strategy=strategy,
            value=locator_value,
            fallback=tuple(cls.from_dict(item) for item in fallback),
            rationale=rationale,
        )


@dataclass(frozen=True)
class ParameterSpec:
    type: str
    description: str
    required: bool = True

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ParameterSpec":
        if not isinstance(value, Mapping):
            raise ValueError("parameter specification must be an object")
        parameter_type = value.get("type")
        description = value.get("description")
        required = value.get("required", True)
        if not isinstance(parameter_type, str) or not isinstance(description, str):
            raise ValueError("parameter type and description must be strings")
        if not isinstance(required, bool):
            raise ValueError("parameter required must be a boolean")
        return cls(parameter_type, description, required)


@dataclass(frozen=True)
class OutputSpec:
    type: str
    description: str
    source: Locator
    sensitive: bool = True

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OutputSpec":
        if not isinstance(value, Mapping):
            raise ValueError("output specification must be an object")
        output_type = value.get("type")
        description = value.get("description")
        sensitive = value.get("sensitive", True)
        if not isinstance(output_type, str) or not isinstance(description, str):
            raise ValueError("output type and description must be strings")
        if not isinstance(sensitive, bool):
            raise ValueError("output sensitive must be a boolean")
        return cls(output_type, description, Locator.from_dict(value.get("source", {})), sensitive)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "description": self.description,
            "source": self.source.to_dict(),
            "sensitive": self.sensitive,
        }


@dataclass(frozen=True)
class ActionStep:
    id: str
    action: ActionType
    target: Locator | None = None
    value: str | None = None
    risk: RiskClass = RiskClass.SAFE
    timeout_ms: int = 5_000
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "action": self.action.value,
            "risk": self.risk.value,
            "timeout_ms": self.timeout_ms,
        }
        if self.target is not None:
            result["target"] = self.target.to_dict()
        if self.value is not None:
            result["value"] = self.value
        if self.description:
            result["description"] = self.description
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ActionStep":
        if not isinstance(value, Mapping):
            raise ValueError("action step must be an object")
        target = value.get("target")
        step_id = value.get("id")
        action = value.get("action")
        risk = value.get("risk", RiskClass.SAFE.value)
        timeout_ms = value.get("timeout_ms", 5_000)
        description = value.get("description", "")
        step_value = value.get("value")
        if not isinstance(step_id, str) or not step_id:
            raise ValueError("step id must be a non-empty string")
        if not isinstance(action, str):
            raise ValueError("step action must be a string")
        if not isinstance(risk, str):
            raise ValueError("step risk must be a string")
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool):
            raise ValueError("step timeout_ms must be an integer")
        if not isinstance(description, str):
            raise ValueError("step description must be a string")
        if step_value is not None and not isinstance(step_value, str):
            raise ValueError("step value must be a string")
        return cls(
            id=step_id,
            action=ActionType(action),
            target=Locator.from_dict(target) if target is not None else None,
            value=step_value,
            risk=RiskClass(risk),
            timeout_ms=timeout_ms,
            description=description,
        )


@dataclass(frozen=True)
class Checkpoint:
    kind: CheckpointKind
    value: str
    description: str

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind.value,
            "value": self.value,
            "description": self.description,
        }


@dataclass(frozen=True)
class BusinessOutcome:
    code: str
    description: str
    detection_text: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "description": self.description,
            "detection_text": self.detection_text,
        }


@dataclass(frozen=True)
class CapabilityArtifact:
    """The model-independent capability contract saved after discovery."""

    capability_id: str
    name: str
    description: str
    surface_kind: str
    target: dict[str, str]
    parameters: dict[str, ParameterSpec]
    outputs: dict[str, OutputSpec]
    steps: tuple[ActionStep, ...]
    checkpoint: Checkpoint
    business_outcomes: tuple[BusinessOutcome, ...] = ()
    schema_version: str = SUPPORTED_SCHEMA_VERSION
    artifact_version: int = 1
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "artifact_version": self.artifact_version,
            "capability_id": self.capability_id,
            "name": self.name,
            "description": self.description,
            "surface_kind": self.surface_kind,
            "target": dict(self.target),
            "parameters": {name: asdict(spec) for name, spec in self.parameters.items()},
            "outputs": {name: spec.to_dict() for name, spec in self.outputs.items()},
            "steps": [step.to_dict() for step in self.steps],
            "checkpoint": self.checkpoint.to_dict(),
            "business_outcomes": [outcome.to_dict() for outcome in self.business_outcomes],
            "created_at": self.created_at,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    def validate(self) -> None:
        if not isinstance(self.schema_version, str):
            raise ValueError("schema_version must be a string")
        if not isinstance(self.artifact_version, int) or isinstance(self.artifact_version, bool):
            raise ValueError("artifact_version must be an integer")
        if self.schema_version != SUPPORTED_SCHEMA_VERSION:
            raise ValueError(f"unsupported capability schema version: {self.schema_version}")
        if self.artifact_version < 1:
            raise ValueError("artifact_version must be at least 1")
        if not self.capability_id or not self.name:
            raise ValueError("capability_id and name are required")
        if not self.target.get("url") or not self.target.get("origin"):
            raise ValueError("target must include url and origin")
        if not self.checkpoint.value or not self.checkpoint.description:
            raise ValueError("checkpoint value and description are required")
        for name, spec in self.parameters.items():
            if spec.type not in {"string", "integer"}:
                raise ValueError(f"unsupported parameter type for {name}: {spec.type}")
        for name, spec in self.outputs.items():
            if spec.type not in {"string", "integer"}:
                raise ValueError(f"unsupported output type for {name}: {spec.type}")
            if spec.source.strategy == "text":
                raise ValueError(f"output source for {name} cannot use a text locator")
            _validate_locator_persistence(spec.source, f"output {name}")
        if not self.steps:
            raise ValueError("a capability must contain at least one step")
        extracted_outputs: set[str] = set()
        for step in self.steps:
            if not step.id or step.timeout_ms < 1:
                raise ValueError(f"step {step.id!r} must have a positive timeout")
            if step.target is not None:
                if step.target.strategy == "text":
                    raise ValueError(f"step {step.id} cannot persist a text locator")
                _validate_locator_persistence(step.target, f"step {step.id}")
            if step.action is ActionType.EXTRACT:
                if step.target is None or step.value not in self.outputs:
                    raise ValueError(f"extract step {step.id} must name a declared output")
                if self.outputs[step.value].source != step.target:
                    raise ValueError(f"extract step {step.id} does not match its output source")
                extracted_outputs.add(step.value)
        missing_outputs = sorted(set(self.outputs) - extracted_outputs)
        if missing_outputs:
            raise ValueError("declared output(s) have no extraction step: " + ", ".join(missing_outputs))
        for outcome in self.business_outcomes:
            if not all(
                isinstance(item, str) and item
                for item in (outcome.code, outcome.description, outcome.detection_text)
            ):
                raise ValueError("business outcomes require non-empty string fields")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CapabilityArtifact":
        try:
            return cls._from_dict(value)
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"invalid capability artifact: {exc}") from exc

    @classmethod
    def _from_dict(cls, value: Mapping[str, Any]) -> "CapabilityArtifact":
        raw_parameters = value.get("parameters", {})
        raw_outputs = value.get("outputs", {})
        raw_steps = value.get("steps")
        if not isinstance(raw_parameters, Mapping) or not isinstance(raw_outputs, Mapping):
            raise ValueError("parameters and outputs must be objects")
        if not isinstance(raw_steps, list):
            raise ValueError("steps must be a list")
        parameters = {
            name: ParameterSpec.from_dict(spec)
            for name, spec in raw_parameters.items()
            if isinstance(name, str)
        }
        if len(parameters) != len(raw_parameters):
            raise ValueError("parameter names must be strings")
        outputs = {
            name: OutputSpec.from_dict(spec)
            for name, spec in raw_outputs.items()
            if isinstance(name, str)
        }
        if len(outputs) != len(raw_outputs):
            raise ValueError("output names must be strings")
        checkpoint_value = value["checkpoint"]
        if not isinstance(checkpoint_value, Mapping):
            raise ValueError("checkpoint must be an object")
        checkpoint_kind = checkpoint_value.get("kind")
        checkpoint_text = checkpoint_value.get("value")
        checkpoint_description = checkpoint_value.get("description")
        if not all(isinstance(item, str) for item in (checkpoint_kind, checkpoint_text, checkpoint_description)):
            raise ValueError("checkpoint kind, value, and description must be strings")
        checkpoint = Checkpoint(
            kind=CheckpointKind(checkpoint_kind),
            value=checkpoint_text,
            description=checkpoint_description,
        )
        required_strings = ("capability_id", "name", "description", "surface_kind")
        if not all(isinstance(value.get(name), str) for name in required_strings):
            raise ValueError("capability identity fields must be strings")
        target = value.get("target")
        if not isinstance(target, Mapping) or not all(
            isinstance(key, str) and isinstance(item, str) for key, item in target.items()
        ):
            raise ValueError("target must be an object of string fields")
        schema_version = value.get("schema_version", SUPPORTED_SCHEMA_VERSION)
        artifact_version = value.get("artifact_version", 1)
        created_at = value.get("created_at", "")
        raw_business_outcomes = value.get("business_outcomes", [])
        if not isinstance(schema_version, str):
            raise ValueError("schema_version must be a string")
        if not isinstance(artifact_version, int) or isinstance(artifact_version, bool):
            raise ValueError("artifact_version must be an integer")
        if not isinstance(created_at, str):
            raise ValueError("created_at must be a string")
        if not isinstance(raw_business_outcomes, list):
            raise ValueError("business_outcomes must be a list")
        for item in raw_business_outcomes:
            if not isinstance(item, Mapping) or not all(
                isinstance(item.get(name), str) for name in ("code", "description", "detection_text")
            ):
                raise ValueError("business outcomes must contain string fields")
        artifact = cls(
            schema_version=schema_version,
            artifact_version=artifact_version,
            capability_id=value["capability_id"],
            name=value["name"],
            description=value["description"],
            surface_kind=value["surface_kind"],
            target=dict(target),
            parameters=parameters,
            outputs=outputs,
            steps=tuple(ActionStep.from_dict(step) for step in raw_steps),
            checkpoint=checkpoint,
            business_outcomes=tuple(
                BusinessOutcome(
                    code=item["code"],
                    description=item["description"],
                    detection_text=item["detection_text"],
                )
                for item in raw_business_outcomes
            ),
            created_at=created_at,
        )
        artifact.validate()
        return artifact


def _validate_locator_persistence(locator: Locator, owner: str) -> None:
    if locator.strategy not in Locator.SUPPORTED_STRATEGIES:
        raise ValueError(f"{owner} has an unsupported locator strategy: {locator.strategy}")
    if redact_text(locator.value) != locator.value:
        raise ValueError(f"{owner} locator appears to contain sensitive text")
    for fallback in locator.fallback:
        _validate_locator_persistence(fallback, owner)


class RunStatus(StrEnum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    RECOVERABLE_FAILURE = "recoverable_failure"
    HARD_FAILURE = "hard_failure"
    ESCALATED = "escalated"


@dataclass(frozen=True)
class RunResult:
    status: RunStatus
    run_id: str
    outputs: dict[str, Any] = field(default_factory=dict)
    outcome_code: str | None = None
    failed_step: str | None = None
    error_code: str | None = None
    message: str | None = None
    evidence_dir: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "run_id": self.run_id,
            "outputs": self.outputs,
            "outcome_code": self.outcome_code,
            "failed_step": self.failed_step,
            "error_code": self.error_code,
            "message": self.message,
            "evidence_dir": self.evidence_dir,
        }
