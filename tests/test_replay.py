from pathlib import Path
from dataclasses import replace
import threading
import time

from cua.evidence import EvidenceRecorder
from cua.handoff import HandoffCoordinator, HandoffState
from cua.models import (
    ActionStep,
    ActionType,
    BusinessOutcome,
    CapabilityArtifact,
    Checkpoint,
    CheckpointKind,
    Locator,
    OutputSpec,
    ParameterSpec,
    RunStatus,
)
from cua.policy import GuardrailPolicy
from cua.replay import ReplayRunner, _resolve_value
from cua.surface import SurfaceError, SurfaceObservation, SurfaceTimeout


class FakeReplaySurface:
    kind = "fake"
    url = "http://127.0.0.1:8765/"

    def __init__(self):
        self.page = "home"
        self.seen_values = []

    def observe(self):
        if self.page == "not-found":
            return SurfaceObservation(self.url, "Member not found", "Member not found", ())
        return SurfaceObservation(
            "http://127.0.0.1:8765/member",
            "Member details",
            "Member details Current savings balance",
            (),
        ) if self.page == "detail" else SurfaceObservation(self.url, "Lookup", "Lookup", ())

    def perform(self, action, locator=None, value=None, timeout_ms=5000):
        if action is ActionType.FILL:
            self.seen_values.append(value)
        if action is ActionType.CLICK:
            self.page = "detail" if self.seen_values != ["9999"] else "not-found"

    def extract(self, locator, timeout_ms=5000):
        return "$1,240.50"

    def capture(self, directory: Path, stem: str):
        screenshot = directory / f"{stem}.png"
        snapshot = directory / f"{stem}.txt"
        screenshot.write_bytes(b"snapshot")
        snapshot.write_text("snapshot", encoding="utf-8")
        return screenshot, snapshot


def artifact():
    return CapabilityArtifact(
        capability_id="member-balance-v1",
        name="Read member balance",
        description="Read a member balance.",
        surface_kind="fake",
        target={"url": "http://127.0.0.1:8765/", "origin": "http://127.0.0.1:8765"},
        parameters={"member_id": ParameterSpec("string", "Synthetic member identifier")},
        outputs={
            "balance": OutputSpec(
                "string", "Current savings balance", Locator("css", "#balance-value")
            )
        },
        steps=(
            ActionStep("fill", ActionType.FILL, Locator("label", "Member ID"), "{{member_id}}"),
            ActionStep("search", ActionType.CLICK, Locator("role", "button:Search")),
            ActionStep("read", ActionType.EXTRACT, Locator("css", "#balance-value"), "balance"),
        ),
        checkpoint=Checkpoint(CheckpointKind.TEXT_PRESENT, "Member details", "details visible"),
        business_outcomes=(BusinessOutcome("MEMBER_NOT_FOUND", "No such member", "Member not found"),),
    )


def test_replay_is_model_free_and_substitutes_inputs(tmp_path):
    surface = FakeReplaySurface()
    result = ReplayRunner(
        surface,
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path),
        artifact(),
        inputs={"member_id": "1001"},
    ).run()

    assert result.status is RunStatus.SUCCESS
    assert result.outputs == {"balance": "$1,240.50"}
    assert surface.seen_values == ["1001"]


def test_replay_url_substitution_preserves_query_component_encoding():
    assert _resolve_value(
        "http://127.0.0.1:8765/member?member={{member_id}}",
        {"member_id": "ab+cd & ef#gh"},
        url=True,
    ) == "http://127.0.0.1:8765/member?member=ab%2Bcd%20%26%20ef%23gh"


def test_replay_surfaces_not_found_as_business_outcome(tmp_path):
    result = ReplayRunner(
        FakeReplaySurface(),
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path),
        artifact(),
        inputs={"member_id": "9999"},
    ).run()

    assert result.status is RunStatus.BUSINESS_OUTCOME
    assert result.outcome_code == "MEMBER_NOT_FOUND"


def test_replay_rejects_missing_input_before_touching_surface(tmp_path):
    surface = FakeReplaySurface()
    result = ReplayRunner(
        surface,
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path),
        artifact(),
        inputs={},
    ).run()

    assert result.status is RunStatus.HARD_FAILURE
    assert result.error_code == "INVALID_INPUT"
    assert surface.seen_values == []


def test_replay_rejects_undeclared_placeholder_before_any_ui_action(tmp_path):
    malformed = replace(
        artifact(),
        steps=(
            artifact().steps[1],
            ActionStep("late-fill", ActionType.FILL, Locator("label", "Member ID"), "{{unknown}}"),
            artifact().steps[2],
        ),
    )
    surface = FakeReplaySurface()
    result = ReplayRunner(
        surface,
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path),
        malformed,
        inputs={"member_id": "1001"},
    ).run()

    assert result.error_code == "INVALID_ARTIFACT"
    assert surface.seen_values == []


def test_replay_rejects_success_when_declared_output_was_not_extracted(tmp_path):
    saved = artifact()
    incomplete = replace(saved, steps=saved.steps[:2])
    result = ReplayRunner(
        FakeReplaySurface(),
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path),
        incomplete,
        inputs={"member_id": "1001"},
    ).run()

    assert result.status is RunStatus.HARD_FAILURE
    assert result.error_code == "INVALID_ARTIFACT"


def test_snapshot_failure_does_not_mask_structured_surface_failure(tmp_path):
    class BrokenCaptureSurface(FakeReplaySurface):
        def perform(self, action, locator=None, value=None, timeout_ms=5000):
            raise SurfaceError("element detached")

        def capture(self, directory: Path, stem: str):
            raise SurfaceError("page closed")

    result = ReplayRunner(
        BrokenCaptureSurface(),
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path),
        artifact(),
        inputs={"member_id": "1001"},
    ).run()

    assert result.status is RunStatus.HARD_FAILURE
    assert result.error_code == "SURFACE_ERROR"
    assert result.failed_step == "fill"


def test_replay_preflights_a_click_destination_before_performing_it(tmp_path):
    class ForbiddenDestinationSurface(FakeReplaySurface):
        def preview_url(self, locator, timeout_ms=5000):
            return "http://127.0.0.1:8765/admin"

    surface = ForbiddenDestinationSurface()
    result = ReplayRunner(
        surface,
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path),
        artifact(),
        inputs={"member_id": "1001"},
    ).run()

    assert result.error_code == "POLICY_BLOCKED"
    assert surface.seen_values == ["1001"]
    assert surface.page == "home"


def test_replay_succeeds_after_transient_timeout_recovery(tmp_path):
    class FlakySurface(FakeReplaySurface):
        failures_left = 2

        def perform(self, action, locator=None, value=None, timeout_ms=5000):
            if action is ActionType.FILL and self.failures_left:
                self.failures_left -= 1
                raise SurfaceTimeout("temporary slowness")
            super().perform(action, locator, value, timeout_ms)

    result = ReplayRunner(
        FlakySurface(),
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path),
        artifact(),
        inputs={"member_id": "1001"},
    ).run()

    assert result.status is RunStatus.SUCCESS


def test_replay_does_not_retry_a_click_after_an_ambiguous_timeout(tmp_path):
    class AmbiguousClickSurface(FakeReplaySurface):
        click_attempts = 0

        def perform(self, action, locator=None, value=None, timeout_ms=5000):
            if action is ActionType.CLICK:
                self.click_attempts += 1
                raise SurfaceTimeout("click completed but response timed out")
            super().perform(action, locator, value, timeout_ms)

    surface = AmbiguousClickSurface()
    policy = GuardrailPolicy(
        allowed_origins=("http://127.0.0.1:8765",),
        risky_actions=frozenset({ActionType.CLICK}),
    )
    result = ReplayRunner(
        surface,
        policy,
        EvidenceRecorder(tmp_path),
        artifact(),
        inputs={"member_id": "1001"},
        confirmed_risky=True,
    ).run()

    assert result.status is RunStatus.RECOVERABLE_FAILURE
    assert result.error_code == "TIMEOUT"
    assert surface.click_attempts == 1


def test_replay_observation_failure_is_structured(tmp_path):
    class BrokenObservationSurface(FakeReplaySurface):
        def observe(self):
            raise SurfaceError("page closed while observing")

    result = ReplayRunner(
        BrokenObservationSurface(),
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path),
        artifact(),
        inputs={"member_id": "1001"},
    ).run()

    assert result.status is RunStatus.HARD_FAILURE
    assert result.error_code == "SURFACE_ERROR"


def test_replay_can_resume_after_same_session_human_takeover(tmp_path):
    class FailingOnceSurface(FakeReplaySurface):
        def __init__(self):
            super().__init__()
            self.failed = False

        def perform(self, action, locator=None, value=None, timeout_ms=5000):
            if action is ActionType.FILL and not self.failed:
                self.failed = True
                raise SurfaceError("field became unavailable")
            super().perform(action, locator, value, timeout_ms)

    surface = FailingOnceSurface()
    coordinator = HandoffCoordinator(
        surface,
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path / "handoff"),
    )
    operator_errors = []

    def operator():
        try:
            while not coordinator.list_requests():
                time.sleep(0.01)
            request = coordinator.list_requests()[0]
            coordinator.take_control(request.intervention_id, "reviewer")
            coordinator.record_human_action(
                request.intervention_id,
                ActionStep("human-fill", ActionType.FILL, Locator("label", "Member ID"), "1001"),
            )
            coordinator.resume(request.intervention_id)
        except Exception as exc:  # surfaced below so this test cannot pass silently
            operator_errors.append(exc)

    worker = threading.Thread(target=operator)
    worker.start()
    result = ReplayRunner(
        surface,
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path / "run"),
        artifact(),
        inputs={"member_id": "1001"},
        handoff=coordinator,
        handoff_wait_s=2,
    ).run()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert not operator_errors
    assert result.status is RunStatus.SUCCESS
    assert coordinator.list_requests()[0].state is HandoffState.RESUMED


def test_replay_handoff_can_supply_a_failed_extraction(tmp_path):
    class FailingExtractionSurface(FakeReplaySurface):
        def __init__(self):
            super().__init__()
            self.failures_left = 3

        def extract(self, locator, timeout_ms=5000):
            if self.failures_left:
                self.failures_left -= 1
                raise SurfaceTimeout("balance was temporarily unavailable")
            return super().extract(locator, timeout_ms)

    surface = FailingExtractionSurface()
    coordinator = HandoffCoordinator(
        surface,
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path / "handoff"),
    )
    operator_errors = []

    def operator():
        try:
            while not coordinator.list_requests():
                time.sleep(0.01)
            request = coordinator.list_requests()[0]
            coordinator.take_control(request.intervention_id, "reviewer")
            coordinator.record_human_action(
                request.intervention_id,
                ActionStep("human-read", ActionType.EXTRACT, Locator("css", "#balance-value"), "balance"),
            )
            coordinator.resume(request.intervention_id)
        except Exception as exc:
            operator_errors.append(exc)

    worker = threading.Thread(target=operator)
    worker.start()
    result = ReplayRunner(
        surface,
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path / "run"),
        artifact(),
        inputs={"member_id": "1001"},
        handoff=coordinator,
        handoff_wait_s=2,
    ).run()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert not operator_errors
    assert result.status is RunStatus.SUCCESS, result.to_dict()
    assert result.outputs == {"balance": "$1,240.50"}
