from pathlib import Path
from urllib.request import Request, urlopen
import json
import threading
import time

import pytest

from cua.evidence import EvidenceRecorder
from cua.handoff import HandoffCoordinator, HandoffServer, HandoffState
from cua.models import ActionStep, ActionType, Checkpoint, CheckpointKind, Locator
from cua.policy import GuardrailPolicy
from cua.surface import SurfaceObservation


class FakeSurface:
    kind = "fake"
    url = "http://127.0.0.1:8765/"

    def __init__(self):
        self.actions = []

    def observe(self):
        return SurfaceObservation(self.url, "Lookup", "Lookup", ())

    def perform(self, action, locator=None, value=None, timeout_ms=5000):
        self.actions.append((action, locator, value))

    def capture(self, directory: Path, stem: str):
        screenshot = directory / f"{stem}.png"
        snapshot = directory / f"{stem}.txt"
        screenshot.write_bytes(b"fake")
        snapshot.write_text("safe", encoding="utf-8")
        return screenshot, snapshot


def test_human_action_uses_same_surface_and_returns_control(tmp_path):
    surface = FakeSurface()
    coordinator = HandoffCoordinator(
        surface,
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path),
    )
    request = coordinator.create_request(
        goal="look up a member",
        capability_id="member-balance-v1",
        step="step-4",
        reason="model could not identify the next control",
        observation=surface.observe(),
    )
    coordinator.take_control(request.intervention_id, "reviewer")
    coordinator.record_human_action(
        request.intervention_id,
        ActionStep("human-1", ActionType.CLICK, Locator("role", "button:Search")),
    )
    coordinator.resume(request.intervention_id)

    assert surface.actions[0][0] is ActionType.CLICK
    assert coordinator.get(request.intervention_id).state == HandoffState.RESUMED
    assert coordinator.get(request.intervention_id).human_actions[0]["id"] == "human-1"


def test_local_operator_api_transfers_and_resumes(tmp_path):
    surface = FakeSurface()
    coordinator = HandoffCoordinator(
        surface,
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path),
    )
    request = coordinator.create_request(
        goal="goal", capability_id="cap", step="step", reason="stuck", observation=surface.observe()
    )
    server = HandoffServer(coordinator)
    server.start()
    try:
        with urlopen(server.url + "/interventions") as response:
            listing = json.loads(response.read())
        assert listing["interventions"][0]["state"] == HandoffState.REQUESTED

        def post(path, payload):
            request = Request(
                server.url + path,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request) as response:
                return json.loads(response.read())

        post(f"/interventions/{request.intervention_id}/take-control", {"operator": "reviewer"})
        result = post(f"/interventions/{request.intervention_id}/resume", {})
        assert result["state"] == HandoffState.RESUMED
    finally:
        server.close()


def test_operator_api_action_is_applied_by_browser_owner_thread(tmp_path):
    class UpdatingSurface(FakeSurface):
        def perform(self, action, locator=None, value=None, timeout_ms=5000):
            super().perform(action, locator, value, timeout_ms)
            if action is ActionType.CLICK:
                self.url = "http://127.0.0.1:8765/member"

        def observe(self):
            if self.url.endswith("/member"):
                return SurfaceObservation(self.url, "Member details", "Member details", ())
            return super().observe()

    surface = UpdatingSurface()
    coordinator = HandoffCoordinator(
        surface,
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path),
    )
    request = coordinator.create_request(
        goal="goal", capability_id="cap", step="step", reason="stuck", observation=surface.observe()
    )
    coordinator.take_control(request.intervention_id, "reviewer")
    server = HandoffServer(coordinator)
    server.start()
    try:
        action_request = Request(
            server.url + f"/interventions/{request.intervention_id}/action",
            data=json.dumps(
                {
                    "action": {
                        "id": "human-search",
                        "action": "click",
                        "target": {"strategy": "role", "value": "button:Search"},
                    }
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        response_body: list[dict] = []

        def call_api():
            with urlopen(action_request) as response:
                response_body.append(json.loads(response.read()))

        worker = threading.Thread(target=call_api)
        worker.start()
        while worker.is_alive():
            coordinator.process_pending_actions()
            time.sleep(0.01)
        worker.join()

        assert response_body[0]["human_actions"][0]["id"] == "human-search"
        assert response_body[0]["current_url"].endswith("/member")
        assert response_body[0]["observation"]["title"] == "Member details"
        assert surface.actions[0][0] is ActionType.CLICK
    finally:
        server.close()


def test_human_action_callback_can_extend_the_discovery_artifact(tmp_path):
    surface = FakeSurface()
    coordinator = HandoffCoordinator(
        surface,
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path),
    )
    recorded = []
    coordinator.on_human_action = recorded.append
    request = coordinator.create_request(
        goal="goal", capability_id="cap", step="step", reason="stuck", observation=surface.observe()
    )
    coordinator.take_control(request.intervention_id, "reviewer")
    step = ActionStep("human-search", ActionType.CLICK, Locator("role", "button:Search"))
    coordinator.record_human_action(request.intervention_id, step)

    assert recorded == [step]


def test_expired_handoff_closes_control_and_rejects_late_actions(tmp_path):
    surface = FakeSurface()
    coordinator = HandoffCoordinator(
        surface,
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path),
    )
    request = coordinator.create_request(
        goal="goal", capability_id="cap", step="step", reason="stuck", observation=surface.observe()
    )
    coordinator.take_control(request.intervention_id, "reviewer")

    assert coordinator.wait_for_resume(request.intervention_id, timeout_s=0) is False
    assert coordinator.get(request.intervention_id).state == HandoffState.CLOSED
    with pytest.raises(RuntimeError, match="human must hold"):
        coordinator.record_human_action(
            request.intervention_id,
            ActionStep("late", ActionType.CLICK, Locator("role", "button:Search")),
        )


def test_unclaimed_handoff_expires_before_late_takeover(tmp_path):
    surface = FakeSurface()
    coordinator = HandoffCoordinator(
        surface,
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path),
    )
    request = coordinator.create_request(
        goal="goal", capability_id="cap", step="step", reason="stuck", observation=surface.observe()
    )

    assert coordinator.wait_for_resume(request.intervention_id, timeout_s=0.01) is False
    assert coordinator.get(request.intervention_id).state == HandoffState.CLOSED
    with pytest.raises(RuntimeError, match="closed"):
        coordinator.take_control(request.intervention_id, "late-reviewer")


def test_handoff_deadline_rejects_actions_queued_after_a_slow_action(tmp_path):
    class SlowSurface(FakeSurface):
        def perform(self, action, locator=None, value=None, timeout_ms=5000):
            if action is ActionType.WAIT:
                time.sleep(int(value or "0") / 1000)
            super().perform(action, locator, value, timeout_ms)

    surface = SlowSurface()
    coordinator = HandoffCoordinator(
        surface,
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path),
    )
    request = coordinator.create_request(
        goal="goal", capability_id="cap", step="step", reason="stuck", observation=surface.observe()
    )
    coordinator.take_control(request.intervention_id, "reviewer")
    errors: list[Exception] = []

    def submit(step):
        try:
            coordinator.record_human_action(request.intervention_id, step)
        except Exception as exc:
            errors.append(exc)

    slow = threading.Thread(
        target=submit,
        args=(ActionStep("slow", ActionType.WAIT, value="100"),),
    )
    late = threading.Thread(
        target=submit,
        args=(ActionStep("late", ActionType.FILL, Locator("role", "input:Member ID"), "secret"),),
    )
    slow.start()
    while len(coordinator._pending_actions) < 1:
        time.sleep(0.001)
    late.start()
    while len(coordinator._pending_actions) < 2:
        time.sleep(0.001)

    assert coordinator.wait_for_resume(request.intervention_id, timeout_s=0.05) is False
    slow.join(timeout=2)
    late.join(timeout=2)

    assert not slow.is_alive()
    assert not late.is_alive()
    assert [action[0] for action in surface.actions] == [ActionType.WAIT]
    assert len(errors) == 1
    assert "expired" in str(errors[0])
    assert coordinator.get(request.intervention_id).state == HandoffState.CLOSED
