from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import json
from typing import Any, Mapping


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
        return cls(
            strategy=str(value["strategy"]),
            value=str(value["value"]),
            fallback=tuple(cls.from_dict(item) for item in value.get("fallback", [])),
            rationale=str(value.get("rationale", "")),
        )


@dataclass(frozen=True)
class ParameterSpec:
    type: str
    description: str
    required: bool = True


@dataclass(frozen=True)
class OutputSpec:
    type: str
    description: str
    source: Locator
    sensitive: bool = True

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
        target = value.get("target")
        return cls(
            id=str(value["id"]),
            action=ActionType(str(value["action"])),
            target=Locator.from_dict(target) if target else None,
            value=str(value["value"]) if value.get("value") is not None else None,
            risk=RiskClass(str(value.get("risk", RiskClass.SAFE.value))),
            timeout_ms=int(value.get("timeout_ms", 5_000)),
            description=str(value.get("description", "")),
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
        if not self.steps:
            raise ValueError("a capability must contain at least one step")
        for step in self.steps:
            if not step.id or step.timeout_ms < 1:
                raise ValueError(f"step {step.id!r} must have a positive timeout")
            if step.action is ActionType.EXTRACT:
                if step.target is None or step.value not in self.outputs:
                    raise ValueError(f"extract step {step.id} must name a declared output")
                if self.outputs[step.value].source != step.target:
                    raise ValueError(f"extract step {step.id} does not match its output source")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CapabilityArtifact":
        parameters = {
            name: ParameterSpec(**dict(spec))
            for name, spec in dict(value.get("parameters", {})).items()
        }
        outputs = {
            name: OutputSpec(
                type=str(spec["type"]),
                description=str(spec["description"]),
                source=Locator.from_dict(spec["source"]),
                sensitive=bool(spec.get("sensitive", True)),
            )
            for name, spec in dict(value.get("outputs", {})).items()
        }
        checkpoint_value = value["checkpoint"]
        checkpoint = Checkpoint(
            kind=CheckpointKind(str(checkpoint_value["kind"])),
            value=str(checkpoint_value["value"]),
            description=str(checkpoint_value["description"]),
        )
        artifact = cls(
            schema_version=str(value.get("schema_version", "1.0")),
            artifact_version=int(value.get("artifact_version", 1)),
            capability_id=str(value["capability_id"]),
            name=str(value["name"]),
            description=str(value["description"]),
            surface_kind=str(value["surface_kind"]),
            target={str(k): str(v) for k, v in dict(value["target"]).items()},
            parameters=parameters,
            outputs=outputs,
            steps=tuple(ActionStep.from_dict(step) for step in value["steps"]),
            checkpoint=checkpoint,
            business_outcomes=tuple(
                BusinessOutcome(
                    code=str(item["code"]),
                    description=str(item["description"]),
                    detection_text=str(item["detection_text"]),
                )
                for item in value.get("business_outcomes", [])
            ),
            created_at=str(value.get("created_at", "")),
        )
        artifact.validate()
        return artifact


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
