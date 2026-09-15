from pathlib import Path

import pytest

from cua.demo_app import serve
from cua.evidence import EvidenceRecorder
from cua.models import (
    ActionStep,
    ActionType,
    BusinessOutcome,
    CapabilityArtifact,
    Checkpoint,
    CheckpointKind,
    Locator,
    RunStatus,
)
from cua.policy import GuardrailPolicy, PolicyViolation
from cua.replay import ReplayRunner
from cua.surface import BrowserSurface


@pytest.fixture(scope="module")
def demo_origin():
    server = serve(port=0)
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _state_artifact(origin: str, state: str, *, confirmation: bool = False) -> CapabilityArtifact:
    url = f"{origin}/runtime/{state}"
    return CapabilityArtifact(
        capability_id=f"runtime-{state}",
        name=f"Runtime {state}",
        description="Browser-backed exceptional-state fixture.",
        surface_kind="browser",
        target={"url": url, "origin": origin},
        parameters={},
        outputs={},
        steps=(
            ActionStep(
                "confirm",
                ActionType.CLICK,
                Locator("css", "#confirm-action"),
            ),
        ) if confirmation else (ActionStep("observe", ActionType.WAIT),),
        checkpoint=Checkpoint(CheckpointKind.TEXT_PRESENT, "fixture", "fixture page remains visible"),
        business_outcomes=(
            BusinessOutcome("VALIDATION_ERROR", "The application rejected the input.", "Validation error"),
            BusinessOutcome("PERMISSION_DENIED", "The operator lacks permission.", "Permission denied"),
            BusinessOutcome("SESSION_EXPIRED", "The session expired.", "Session expired"),
        ),
    )


@pytest.mark.parametrize(
    ("state", "outcome"),
    [
        ("validation", "VALIDATION_ERROR"),
        ("permission-denied", "PERMISSION_DENIED"),
        ("session-expired", "SESSION_EXPIRED"),
    ],
)
def test_browser_surface_replay_reports_business_runtime_states(tmp_path, demo_origin, state, outcome):
    surface = BrowserSurface.open(f"{demo_origin}/runtime/{state}")
    try:
        result = ReplayRunner(
            surface,
            GuardrailPolicy.local_demo(demo_origin),
            EvidenceRecorder(tmp_path),
            _state_artifact(demo_origin, state),
            inputs={},
        ).run()
    finally:
        surface.close()

    assert result.status is RunStatus.BUSINESS_OUTCOME
    assert result.outcome_code == outcome


def test_browser_surface_replay_reports_application_error(tmp_path, demo_origin):
    surface = BrowserSurface.open(f"{demo_origin}/runtime/app-error")
    try:
        result = ReplayRunner(
            surface,
            GuardrailPolicy.local_demo(demo_origin),
            EvidenceRecorder(tmp_path),
            _state_artifact(demo_origin, "app-error"),
            inputs={},
        ).run()
    finally:
        surface.close()

    assert result.status is RunStatus.HARD_FAILURE
    assert result.error_code == "APP_ERROR"
    assert (Path(result.evidence_dir) / "failure-observe.txt").exists()


def test_browser_surface_reports_unexpected_confirmation_dialog(tmp_path, demo_origin):
    surface = BrowserSurface.open(f"{demo_origin}/runtime/confirmation")
    try:
        result = ReplayRunner(
            surface,
            GuardrailPolicy.local_demo(demo_origin),
            EvidenceRecorder(tmp_path),
            _state_artifact(demo_origin, "confirmation", confirmation=True),
            inputs={},
        ).run()
    finally:
        surface.close()

    assert result.status is RunStatus.HARD_FAILURE
    assert result.error_code == "UNEXPECTED_DIALOG"


@pytest.mark.parametrize(
    ("action", "locator", "value"),
    [
        (ActionType.CLICK, Locator("css", "#js-nav"), None),
        (ActionType.CLICK, Locator("css", "#form-nav"), None),
        (ActionType.PRESS, Locator("css", "#enter-nav"), "Enter"),
    ],
)
def test_browser_surface_blocks_forbidden_navigation_before_request(
    tmp_path, demo_origin, action, locator, value
):
    surface = BrowserSurface.open(f"{demo_origin}/runtime/hostile")
    artifact = CapabilityArtifact(
        capability_id="hostile-navigation",
        name="Hostile navigation fixture",
        description="Verify the browser route guard.",
        surface_kind="browser",
        target={"url": f"{demo_origin}/runtime/hostile", "origin": demo_origin},
        parameters={},
        outputs={},
        steps=(ActionStep("navigate", action, locator, value),),
        checkpoint=Checkpoint(CheckpointKind.TEXT_PRESENT, "Hostile navigation", "fixture visible"),
    )
    try:
        result = ReplayRunner(
            surface,
            GuardrailPolicy.local_demo(demo_origin),
            EvidenceRecorder(tmp_path),
            artifact,
            inputs={},
        ).run()
    finally:
        current_url = surface.url
        surface.close()

    assert result.status is RunStatus.HARD_FAILURE
    assert result.error_code == "POLICY_BLOCKED"
    assert "/admin" not in current_url


def test_browser_surface_guards_startup_redirects(demo_origin):
    policy = GuardrailPolicy.local_demo(demo_origin)

    with pytest.raises(PolicyViolation, match="route is not allowlisted"):
        BrowserSurface.open(
            f"{demo_origin}/runtime/redirect",
            navigation_guard=policy.check_url,
        )
