from __future__ import annotations

from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from typing import Any
import uuid

from .evidence import EvidenceRecorder
from .models import ActionStep, ActionType, Locator
from .policy import ConfirmationRequired, GuardrailPolicy, PolicyViolation
from .surface import SurfaceError, SurfaceObservation


class HandoffState:
    REQUESTED = "requested"
    HUMAN_CONTROL = "human_control"
    RESUMED = "resumed"
    CLOSED = "closed"


@dataclass
class InterventionRequest:
    intervention_id: str
    run_id: str
    goal: str
    capability_id: str
    step: str
    reason: str
    state: str
    current_url: str
    observation: dict[str, Any]
    screenshot: str | None = None
    snapshot: str | None = None
    operator: str | None = None
    human_actions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "intervention_id": self.intervention_id,
            "run_id": self.run_id,
            "goal": self.goal,
            "capability_id": self.capability_id,
            "step": self.step,
            "reason": self.reason,
            "state": self.state,
            "current_url": self.current_url,
            "observation": self.observation,
            "screenshot": self.screenshot,
            "snapshot": self.snapshot,
            "operator": self.operator,
            "human_actions": self.human_actions,
        }


class HandoffCoordinator:
    """Transfers ownership while keeping automation and the operator on one surface."""

    def __init__(self, surface: Any, policy: GuardrailPolicy, evidence: EvidenceRecorder):
        self.surface = surface
        self.policy = policy
        self.evidence = evidence
        self._requests: dict[str, InterventionRequest] = {}
        self._condition = threading.Condition()

    def create_request(
        self,
        *,
        goal: str,
        capability_id: str,
        step: str,
        reason: str,
        observation: SurfaceObservation,
    ) -> InterventionRequest:
        screenshot, snapshot = self.surface.capture(self.evidence.directory, f"intervention-{uuid.uuid4().hex[:8]}")
        request = InterventionRequest(
            intervention_id=uuid.uuid4().hex,
            run_id=self.evidence.run_id,
            goal=goal,
            capability_id=capability_id,
            step=step,
            reason=reason,
            state=HandoffState.REQUESTED,
            current_url=observation.url,
            observation=observation.to_dict(),
            screenshot=str(screenshot) if screenshot is not None else None,
            snapshot=str(snapshot),
        )
        with self._condition:
            self._requests[request.intervention_id] = request
            self._condition.notify_all()
        self.evidence.event("intervention_requested", intervention=request.to_dict())
        return request

    def list_requests(self) -> list[InterventionRequest]:
        with self._condition:
            return list(self._requests.values())

    def get(self, intervention_id: str) -> InterventionRequest:
        with self._condition:
            try:
                return self._requests[intervention_id]
            except KeyError as exc:
                raise KeyError(f"unknown intervention: {intervention_id}") from exc

    def take_control(self, intervention_id: str, operator: str) -> InterventionRequest:
        with self._condition:
            request = self.get(intervention_id)
            if request.state != HandoffState.REQUESTED:
                raise RuntimeError(f"intervention is {request.state}, not requested")
            request.state = HandoffState.HUMAN_CONTROL
            request.operator = operator
            self._condition.notify_all()
        self.evidence.event("control_transferred", intervention_id=intervention_id, operator=operator)
        return request

    def record_human_action(
        self,
        intervention_id: str,
        step: ActionStep,
        *,
        confirmed: bool = False,
    ) -> None:
        request = self.get(intervention_id)
        if request.state != HandoffState.HUMAN_CONTROL:
            raise RuntimeError("human must hold control before acting")
        self.policy.check_step(step, confirmed=confirmed)
        self.surface.perform(step.action, step.target, step.value, step.timeout_ms)
        self.policy.check_url(self.surface.url)
        action = step.to_dict()
        request.human_actions.append(action)
        self.evidence.event(
            "human_action",
            intervention_id=intervention_id,
            operator=request.operator,
            step=action,
        )

    def resume(self, intervention_id: str) -> InterventionRequest:
        with self._condition:
            request = self.get(intervention_id)
            if request.state != HandoffState.HUMAN_CONTROL:
                raise RuntimeError(f"intervention is {request.state}, not human_control")
            request.state = HandoffState.RESUMED
            self._condition.notify_all()
        self.evidence.event("control_returned", intervention_id=intervention_id, operator=request.operator)
        return request

    def wait_for_resume(self, intervention_id: str, timeout_s: float = 300.0) -> bool:
        with self._condition:
            resumed = self._condition.wait_for(
                lambda: self._requests[intervention_id].state in {HandoffState.RESUMED, HandoffState.CLOSED},
                timeout=timeout_s,
            )
            return resumed and self._requests[intervention_id].state == HandoffState.RESUMED


class HandoffServer:
    """A localhost-only operator surface for the active BrowserSurface session."""

    def __init__(self, coordinator: HandoffCoordinator, host: str = "127.0.0.1", port: int = 0):
        self.coordinator = coordinator
        self.server = ThreadingHTTPServer((host, port), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, name="cua-handoff", daemon=True)

    def _handler(self):
        coordinator = self.coordinator

        class Handler(BaseHTTPRequestHandler):
            def _json(self, status: int, payload: Any) -> None:
                body = json.dumps(payload, sort_keys=True).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0"))
                return json.loads(self.rfile.read(length) or b"{}")

            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/interventions":
                    self._json(404, {"error": "not found"})
                    return
                self._json(200, {"interventions": [item.to_dict() for item in coordinator.list_requests()]})

            def do_POST(self) -> None:  # noqa: N802
                parts = self.path.strip("/").split("/")
                if len(parts) != 3 or parts[0] != "interventions":
                    self._json(404, {"error": "not found"})
                    return
                intervention_id, operation = parts[1], parts[2]
                try:
                    body = self._body()
                    if operation == "take-control":
                        result = coordinator.take_control(intervention_id, str(body.get("operator", "operator")))
                    elif operation == "resume":
                        result = coordinator.resume(intervention_id)
                    elif operation == "action":
                        action = body["action"]
                        step = ActionStep(
                            id=str(action.get("id", "human-action")),
                            action=ActionType(str(action["action"])),
                            target=Locator.from_dict(action["target"]) if action.get("target") else None,
                            value=str(action["value"]) if action.get("value") is not None else None,
                        )
                        coordinator.record_human_action(
                            intervention_id,
                            step,
                            confirmed=bool(body.get("confirmed", False)),
                        )
                        result = coordinator.get(intervention_id)
                    else:
                        self._json(404, {"error": "unknown operation"})
                        return
                    self._json(200, result.to_dict())
                except (KeyError, ValueError, PolicyViolation, ConfirmationRequired, SurfaceError, RuntimeError) as exc:
                    self._json(400, {"error": str(exc)})

            def log_message(self, _format: str, *_args: object) -> None:
                return

        return Handler

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
