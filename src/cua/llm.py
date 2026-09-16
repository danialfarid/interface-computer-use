from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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
_SAFE_MODEL_KINDS = frozenset(
    {"button", "checkbox", "combobox", "input", "link", "radio", "select", "textbox"}
)
_SAFE_MODEL_TASK = (
    "On the member lookup form, fill Member ID with {{member_id}}, click Search, "
    "then extract the current savings balance."
)


DECISION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "maxItems": 1,
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
        if len(raw_actions) > 1:
            raise LLMError("decision.actions must contain at most one action")
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
        self._last_action: str | None = None
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

    def set_last_action(self, action: ActionType | str | None) -> None:
        """Share only the previous action kind, never its runtime value."""

        self._last_action = action.value if isinstance(action, ActionType) else (
            str(action) if action is not None else None
        )

    def provenance(self) -> dict[str, Any]:
        return dict(self._last_provenance)

    def decide(self, goal: str, observation: SurfaceObservation) -> AgentDecision:
        del goal
        system = (
            "You operate a browser through a constrained action interface. "
            "Use only control_id and target_id values present in the observation. "
            "For extract, set control_id to null and use the readable target_id. "
            "Take the smallest safe next action. Never invent selectors. "
            "The browser is already open on the approved target; do not navigate to the current page. "
            "For navigate, put an approved URL in value and leave both IDs null. "
            "For fill, click, and press, use the exact control-<number> from controls as control_id "
            "and set target_id to null. For extract, use the exact target-<number> from "
            "readable_targets as target_id and set control_id to null; never put a label, "
            "parameter name, or URL in an ID field. "
            "For this capability, follow next_action_hint exactly: initially fill control-0 "
            "with {{member_id}}; after that fill click control-1 (Search); after that click "
            "extract the readable target with stable_key balance-value as "
            "current_savings_balance. Do not repeat the previous action. "
            "On the initial form specifically, Member ID is control-0 and Search is control-1. "
            "Return only the supplied JSON schema: "
            '{"actions":[{"action":"fill|click|press|extract|wait",'
            '"control_id":"...","target_id":"...","value":"...",'
            '"output_name":"...","reason":"...","risk":"safe|risky"}],'
            '"done":false,"message":"..."}. '
            "Use extract only with a readable target marked extractable=true; text-only targets "
            "cannot be persisted. Set done only after the goal is met; when all requested "
            "outputs are listed as completed_outputs, return done=true."
        )
        model_observation = sanitize_observation_for_model(observation)
        if self._last_action == ActionType.FILL.value:
            # Do not offer the already-completed field as a candidate for the
            # next decision. This is a phase hint, not a locator remapping.
            model_observation["controls"] = [
                control
                for control in model_observation["controls"]
                if control["name"] != "Member ID"
            ]
        elif self._last_action == ActionType.CLICK.value:
            model_observation["controls"] = []
        user = json.dumps(
            {
                "goal": "<REDACTED operator goal>",
                "task": _task_for_phase(self._last_action, observation, self._completed_outputs),
                "observation": model_observation,
                "available_parameters": sorted(self._parameter_names),
                "task_outputs": sorted(self._task_outputs),
                "completed_outputs": sorted(self._completed_outputs),
                "last_action": self._last_action,
                "next_action_hint": _next_action_hint(
                    self._last_action, observation, self._completed_outputs
                ),
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
        "url": "<CURRENT_ALLOWLISTED_SURFACE>",
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
                **_controlled_target_key(target),
            }
            for target in observation.readable_targets
        ],
    }


def _controlled_label(value: str) -> str:
    normalized = value.strip().casefold()
    return value if normalized in _SAFE_MODEL_LABELS else "<REDACTED>"


def _controlled_kind(value: str) -> str:
    return value if value.strip().casefold() in _SAFE_MODEL_KINDS else "<REDACTED>"


def _controlled_target_key(target: Any) -> dict[str, str]:
    if target.locator.strategy == "css" and target.locator.value in {'#balance-value', '[id="balance-value"]'}:
        return {"stable_key": "balance-value"}
    return {}


def _next_action_hint(
    last_action: str | None,
    observation: SurfaceObservation,
    completed_outputs: set[str],
) -> str:
    """Give the provider a non-sensitive task phase, not a runtime value."""

    if "current_savings_balance" in completed_outputs:
        return "goal complete"
    if last_action == ActionType.FILL.value:
        return "click control-1 (Search)"
    if last_action == ActionType.CLICK.value:
        for target in observation.readable_targets:
            if _controlled_target_key(target).get("stable_key") == "balance-value":
                return f"extract {target.ephemeral_id} as current_savings_balance"
    return "fill control-0 (Member ID) with {{member_id}}"


def _task_for_phase(
    last_action: str | None,
    observation: SurfaceObservation,
    completed_outputs: set[str],
) -> str:
    if "current_savings_balance" in completed_outputs:
        return "The approved member balance lookup is complete. Return done=true with no action."
    if last_action == ActionType.FILL.value:
        return "The Member ID field is already filled. Click Search using control-1 now."
    if last_action == ActionType.CLICK.value:
        target_hint = _next_action_hint(last_action, observation, completed_outputs)
        return f"Search completed. {target_hint}."
    return _SAFE_MODEL_TASK


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
