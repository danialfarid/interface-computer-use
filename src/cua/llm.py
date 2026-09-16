from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from .models import ActionType, RiskClass
from .surface import SurfaceObservation


class LLMError(RuntimeError):
    """The model could not return a valid, policy-checkable decision."""


_SAFE_MODEL_LABELS = frozenset(
    {
        "member id",
        "search",
        "return to lookup",
        "member details",
        "current savings balance",
        "member services console",
    }
)
_SAFE_MODEL_PATHS = frozenset({"/", "/member"})
_SAFE_MODEL_KINDS = frozenset(
    {"button", "checkbox", "combobox", "input", "link", "radio", "select", "textbox"}
)


DECISION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": [item.value for item in ActionType]},
                    "control_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "target_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "value": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "output_name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "reason": {"type": "string"},
                    "risk": {"type": "string", "enum": [item.value for item in RiskClass]},
                },
                "required": [
                    "action",
                    "control_id",
                    "target_id",
                    "value",
                    "output_name",
                    "reason",
                    "risk",
                ],
                "additionalProperties": False,
            },
        },
        "done": {"type": "boolean"},
        "message": {"type": "string"},
    },
    "required": ["actions", "done", "message"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class AgentAction:
    action: ActionType
    control_id: str | None = None
    target_id: str | None = None
    value: str | None = None
    output_name: str | None = None
    reason: str = ""
    risk: RiskClass = RiskClass.SAFE

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "control_id": self.control_id,
            "target_id": self.target_id,
            "value": self.value,
            "output_name": self.output_name,
            "reason": self.reason,
            "risk": self.risk.value,
        }


@dataclass(frozen=True)
class AgentDecision:
    actions: tuple[AgentAction, ...]
    done: bool = False
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "actions": [action.to_dict() for action in self.actions],
            "done": self.done,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AgentDecision":
        raw_actions = payload.get("actions", [])
        if not isinstance(raw_actions, list):
            raise LLMError("decision.actions must be an array")
        actions = []
        for item in raw_actions:
            if not isinstance(item, dict):
                raise LLMError("each decision action must be an object")
            try:
                actions.append(
                    AgentAction(
                        action=ActionType(str(item["action"])),
                        control_id=_nullable_string(item.get("control_id")),
                        target_id=_nullable_string(item.get("target_id")),
                        value=_nullable_string(item.get("value")),
                        output_name=_nullable_string(item.get("output_name")),
                        reason=str(item.get("reason", "")),
                        risk=RiskClass(str(item.get("risk", RiskClass.SAFE.value))),
                    )
                )
            except (KeyError, ValueError) as exc:
                raise LLMError(f"invalid decision action: {item}") from exc
        return cls(tuple(actions), bool(payload.get("done", False)), str(payload.get("message", "")))


class DecisionClient(Protocol):
    def decide(self, goal: str, observation: SurfaceObservation) -> AgentDecision:
        ...


class ScriptedDecisionClient:
    """Offline client for tests and deterministic local development."""

    def __init__(self, decisions: list[AgentDecision]):
        self._decisions = iter(decisions)

    def decide(self, goal: str, observation: SurfaceObservation) -> AgentDecision:
        del goal, observation
        try:
            return next(self._decisions)
        except StopIteration as exc:
            raise LLMError("scripted decision sequence exhausted") from exc

    def provenance(self) -> dict[str, str]:
        return {"decision_source": "scripted"}


class OpenAICompatibleClient:
    """Small JSON-only adapter for OpenAI-compatible chat-completions endpoints."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        endpoint: str | None = None,
        timeout_s: float = 60.0,
    ):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.model = model or os.environ.get("CUA_LLM_MODEL", "gpt-4o-mini")
        self.endpoint = endpoint or os.environ.get(
            "CUA_LLM_ENDPOINT", "https://api.openai.com/v1/chat/completions"
        )
        self.timeout_s = timeout_s
        self._parameter_names: set[str] = set()
        self._task_outputs: set[str] = set()
        self._completed_outputs: set[str] = set()
        self._last_provenance: dict[str, Any] = {
            "decision_source": "provider",
            "model": self.model,
            "endpoint_host": _endpoint_host(self.endpoint),
        }
        if not self.api_key:
            raise LLMError("OPENAI_API_KEY is required for a live discovery run")

    def set_parameter_names(self, names: set[str] | tuple[str, ...]) -> None:
        self._parameter_names = {str(name) for name in names}

    def set_task_context(self, output_names: set[str] | tuple[str, ...]) -> None:
        self._task_outputs = {str(name) for name in output_names}

    def set_completed_outputs(self, names: set[str]) -> None:
        self._completed_outputs = {str(name) for name in names}

    def provenance(self) -> dict[str, Any]:
        return dict(self._last_provenance)

    def decide(self, goal: str, observation: SurfaceObservation) -> AgentDecision:
        del goal
        system = (
            "You operate a browser through a constrained action interface. "
            "Use only control_id and target_id values present in the observation. "
            "For extract, set control_id to null and use the readable target_id. "
            "Take the smallest safe next action. Never invent selectors. "
            "Return only the supplied JSON schema: "
            '{"actions":[{"action":"fill|click|press|extract|wait",'
            '"control_id":"...","target_id":"...","value":"...",'
            '"output_name":"...","reason":"...","risk":"safe|risky"}],'
            '"done":false,"message":"..."}. '
            "Use extract only with a readable target marked extractable=true; text-only targets "
            "cannot be persisted. Set done only after the goal is met; when all requested "
            "outputs are listed as completed_outputs, return done=true."
        )
        user = json.dumps(
            {
                "goal": "<REDACTED operator goal>",
                "observation": sanitize_observation_for_model(observation),
                "available_parameters": sorted(self._parameter_names),
                "task_outputs": sorted(self._task_outputs),
                "completed_outputs": sorted(self._completed_outputs),
            },
            sort_keys=True,
        )
        request_body = json.dumps(
            {
                "model": self.model,
                "temperature": 0,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "computer_use_decision",
                        "strict": True,
                        "schema": DECISION_JSON_SCHEMA,
                    },
                },
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
        ).encode()
        request = Request(
            self.endpoint,
            data=request_body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self._last_provenance = {
                **self._last_provenance,
                **_provider_provenance(payload),
            }
            content = payload["choices"][0]["message"]["content"]
            return AgentDecision.from_dict(json.loads(content))
        except HTTPError as exc:
            raise LLMError(f"LLM HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}") from exc
        except (URLError, TimeoutError) as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise LLMError(f"LLM returned an invalid decision payload: {exc}") from exc


def _nullable_string(value: Any) -> str | None:
    """Accept a common JSON-mode quirk without treating it as a control id."""

    if value is None or value == "null":
        return None
    return str(value)


def sanitize_observation_for_model(
    observation: SurfaceObservation,
    sensitive_values: set[str] | tuple[str, ...] = (),
    parameter_values: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return only an allowlisted structural view for a model provider.

    The optional arguments are retained for callers compiled against the early
    API, but deliberately ignored: values from a regulated surface must never
    be sent to the provider for heuristic redaction.
    """

    del sensitive_values, parameter_values
    return {
        "url": _safe_model_url(observation.url),
        "title": _controlled_label(observation.title),
        "text": "<REDACTED>",
        "controls": [
            {
                "id": control.ephemeral_id,
                "kind": _controlled_kind(control.kind),
                "name": _controlled_label(control.name),
                "locator_strategy": control.locator.strategy,
            }
            for control in observation.controls
        ],
        "readable_targets": [
            {
                "id": target.ephemeral_id,
                "text": _controlled_label(target.text),
                "locator_strategy": target.locator.strategy,
                "extractable": target.locator.strategy != "text",
            }
            for target in observation.readable_targets
        ],
    }


def _controlled_label(value: str) -> str:
    normalized = value.strip().casefold()
    return value if normalized in _SAFE_MODEL_LABELS else "<REDACTED>"


def _controlled_kind(value: str) -> str:
    return value if value.strip().casefold() in _SAFE_MODEL_KINDS else "<REDACTED>"


def _safe_model_url(value: str) -> str:
    parsed = urlsplit(value)
    hostname = parsed.hostname or "<REDACTED>"
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        port = ""
    path = parsed.path if parsed.path in _SAFE_MODEL_PATHS else "/<REDACTED>"
    return f"{parsed.scheme}://{hostname}{port}{path}"


def _endpoint_host(endpoint: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(endpoint).hostname or "unknown"


def _provider_provenance(payload: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if isinstance(payload, dict) and isinstance(payload.get("id"), str):
        result["provider_response_id"] = payload["id"]
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if isinstance(usage, dict):
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if isinstance(usage.get(key), int):
                result[key] = usage[key]
    return result
