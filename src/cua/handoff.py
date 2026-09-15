from __future__ import annotations

from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from typing import Any, Callable
import uuid

from .evidence import EvidenceRecorder
from .models import ActionStep, ActionType, Locator, RiskClass
from .policy import ConfirmationRequired, GuardrailPolicy, PolicyViolation, check_action_destination
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


@dataclass
class _PendingHumanAction:
    intervention_id: str
    step: ActionStep
    confirmed: bool
    done: threading.Event = field(default_factory=threading.Event)
    error: Exception | None = None


class HandoffCoordinator:
    """Transfers ownership while keeping automation and the operator on one surface."""

    def __init__(self, surface: Any, policy: GuardrailPolicy, evidence: EvidenceRecorder):
        self.surface = surface
        self.policy = policy
        self.evidence = evidence
        self._requests: dict[str, InterventionRequest] = {}
        self._condition = threading.Condition()
        self._owner_thread_id = threading.get_ident()
        self._pending_actions: list[_PendingHumanAction] = []
        self.on_human_action: Callable[[ActionStep], None] | None = None
        self.on_human_output: Callable[[ActionStep, str], None] | None = None
        set_navigation_guard = getattr(self.surface, "set_navigation_guard", None)
        if callable(set_navigation_guard):
            set_navigation_guard(self.policy.check_url)

    def create_request(
        self,
        *,
        goal: str,
        capability_id: str,
        step: str,
        reason: str,
        observation: SurfaceObservation,
    ) -> InterventionRequest:
        try:
            screenshot, snapshot = self.surface.capture(
                self.evidence.directory, f"intervention-{uuid.uuid4().hex[:8]}"
            )
        except Exception as exc:
            self.evidence.event("intervention_snapshot_unavailable", error=str(exc))
            screenshot, snapshot = None, None
        request = InterventionRequest(
            intervention_id=uuid.uuid4().hex,
            run_id=self.evidence.run_id,
            goal=goal,
            capability_id=capability_id,
            step=step,
            reason=reason,
            state=HandoffState.REQUESTED,
            current_url=self.evidence.redact_url(observation.url),
            observation=self.evidence.redact_payload(observation.to_dict()),
            screenshot=str(screenshot) if screenshot is not None else None,
            snapshot=str(snapshot) if snapshot is not None else None,
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
        pending = _PendingHumanAction(intervention_id, step, confirmed)
        if threading.get_ident() == self._owner_thread_id:
            self._apply_human_action(pending)
            if pending.error is not None:
                raise pending.error
            return
        with self._condition:
            request = self.get(intervention_id)
            if request.state != HandoffState.HUMAN_CONTROL:
                raise RuntimeError("human must hold control before acting")
            self._pending_actions.append(pending)
            self._condition.notify_all()
        if not pending.done.wait(timeout=30.0):
            raise SurfaceError("timed out waiting for the browser owner to apply the human action")
        if pending.error is not None:
            raise pending.error

    def process_pending_actions(
        self,
        *,
        intervention_id: str | None = None,
        expires_at: float | None = None,
    ) -> int:
        """Apply queued HTTP actions on the thread that owns the live browser."""

        if threading.get_ident() != self._owner_thread_id:
            raise RuntimeError("browser actions must be processed by the coordinator owner")
        with self._condition:
            pending = list(self._pending_actions)
            self._pending_actions.clear()
        for item in pending:
            try:
                if (
                    intervention_id == item.intervention_id
                    and expires_at is not None
                    and time.monotonic() >= expires_at
                ):
                    self._expire_request(item.intervention_id)
                    raise SurfaceError("intervention expired before the action was applied")
                self._apply_human_action(item)
            except Exception as exc:  # communicate the structured error to the API caller
                item.error = exc
            finally:
                item.done.set()
        return len(pending)

    def _apply_human_action(self, pending: _PendingHumanAction) -> None:
        request = self.get(pending.intervention_id)
        if request.state != HandoffState.HUMAN_CONTROL:
            raise RuntimeError("human must hold control before acting")
        self.policy.check_step(pending.step, confirmed=pending.confirmed)
        check_action_destination(self.policy, self.surface, pending.step)
        extracted: str | None = None
        if pending.step.action is ActionType.EXTRACT:
            if pending.step.target is None:
                raise SurfaceError("human extract requires a target")
            extracted = self.surface.extract(pending.step.target, pending.step.timeout_ms)
            self.evidence.sensitive_values.add(extracted)
            set_sensitive_values = getattr(self.surface, "set_sensitive_values", None)
            if callable(set_sensitive_values):
                set_sensitive_values({extracted})
        else:
            self.surface.perform(
                pending.step.action,
                pending.step.target,
                pending.step.value,
                pending.step.timeout_ms,
            )
        self.policy.check_url(self.surface.url)
        request.current_url = self.evidence.redact_url(self.surface.url)
        try:
            request.observation = self.evidence.redact_payload(self.surface.observe().to_dict())
        except SurfaceError as exc:
            request.observation = self.evidence.redact_payload({
                "url": request.current_url,
                "title": "",
                "text": "",
                "controls": [],
                "readable_targets": [],
                "observation_error": str(exc),
            })
        action = pending.step.to_dict()
        redacted_action = {
            "id": action.get("id"),
            **self.evidence.redact_payload({key: value for key, value in action.items() if key != "id"}),
        }
        if pending.step.action.value in {"fill", "navigate"} and "value" in redacted_action:
            redacted_action["value"] = "<REDACTED>"
        request.human_actions.append(redacted_action)
        self.evidence.event(
            "human_action",
            intervention_id=pending.intervention_id,
            operator=request.operator,
            step=action,
            extracted_value="<REDACTED>" if extracted is not None else None,
        )
        if extracted is not None and self.on_human_output is not None:
            self.on_human_output(pending.step, extracted)
        if self.on_human_action is not None:
            self.on_human_action(pending.step)

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
        expires_at = time.monotonic() + timeout_s
        while True:
            self.process_pending_actions(intervention_id=intervention_id, expires_at=expires_at)
            with self._condition:
                request = self._requests[intervention_id]
                if request.state in {HandoffState.RESUMED, HandoffState.CLOSED}:
                    return request.state == HandoffState.RESUMED
                remaining = expires_at - time.monotonic()
                if remaining <= 0:
                    self._expire_request(intervention_id)
                    with self._condition:
                        return self._requests[intervention_id].state == HandoffState.RESUMED
                self._condition.wait(timeout=min(0.1, remaining))

    def _expire_request(self, intervention_id: str) -> None:
        pending: list[_PendingHumanAction] = []
        with self._condition:
            request = self._requests[intervention_id]
            if request.state not in {HandoffState.REQUESTED, HandoffState.HUMAN_CONTROL}:
                return
            request.state = HandoffState.CLOSED
            operator = request.operator
            retained: list[_PendingHumanAction] = []
            for item in self._pending_actions:
                if item.intervention_id == intervention_id:
                    pending.append(item)
                else:
                    retained.append(item)
            self._pending_actions = retained
            self._condition.notify_all()
        for item in pending:
            item.error = SurfaceError("intervention expired before the action was applied")
            item.done.set()
        self.evidence.event("intervention_expired", intervention_id=intervention_id, operator=operator)


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
                            risk=RiskClass(str(action.get("risk", RiskClass.SAFE.value))),
                            timeout_ms=int(action.get("timeout_ms", 5_000)),
                            description=str(action.get("description", "")),
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
