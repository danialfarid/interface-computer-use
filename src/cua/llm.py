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
        if not self.api_key:
            raise LLMError("OPENAI_API_KEY is required for a live discovery run")

    def decide(self, goal: str, observation: SurfaceObservation) -> AgentDecision:
        system = (
            "You operate a browser through a constrained action interface. "
            "Use only control_id and target_id values present in the observation. "
            "Take the smallest safe next action. Never invent selectors. "
            "Return only the supplied JSON schema: "
            '{"actions":[{"action":"fill|click|press|extract|wait",'
            '"control_id":"...","target_id":"...","value":"...",'
            '"output_name":"...","reason":"...","risk":"safe|risky"}],'
            '"done":false,"message":"..."}. '
            "Use extract for declared readable values; set done only after the goal is met."
        )
        user = json.dumps({"goal": goal, "observation": observation.to_dict()}, sort_keys=True)
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
