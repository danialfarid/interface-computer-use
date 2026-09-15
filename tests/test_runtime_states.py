from pathlib import Path

import pytest

from cua.evidence import EvidenceRecorder
from cua.models import (
    ActionStep,
    ActionType,
    BusinessOutcome,
    CapabilityArtifact,
    Checkpoint,
    CheckpointKind,
    Locator,
    ParameterSpec,
    RunStatus,
)
from cua.policy import GuardrailPolicy
from cua.replay import ReplayRunner
from cua.surface import SurfaceAppError, SurfaceObservation, SurfaceTimeout, UnexpectedDialog


class RuntimeStateSurface:
    kind = "fake"
    url = "http://127.0.0.1:8765/"

    def __init__(self, text="Lookup", failure=None):
        self.text = text
        self.failure = failure
        self.actions = 0

    def observe(self):
        return SurfaceObservation(self.url, "Demo", self.text, ())

    def perform(self, action, locator=None, value=None, timeout_ms=5000):
        self.actions += 1
        if self.failure is not None:
            raise self.failure

    def extract(self, locator, timeout_ms=5000):
        return "value"

    def capture(self, directory: Path, stem: str):
        snapshot = directory / f"{stem}.txt"
        snapshot.write_text("redacted runtime state", encoding="utf-8")
        return None, snapshot


def runtime_artifact():
    return CapabilityArtifact(
        capability_id="runtime-state-fixture",
        name="Runtime state fixture",
        description="Exercise explicit exceptional states.",
        surface_kind="fake",
        target={"url": "http://127.0.0.1:8765/", "origin": "http://127.0.0.1:8765"},
        parameters={"member_id": ParameterSpec("string", "Synthetic member identifier")},
        outputs={},
        steps=(ActionStep("lookup", ActionType.FILL, Locator("label", "Member ID"), "{{member_id}}"),),
        checkpoint=Checkpoint(CheckpointKind.TEXT_PRESENT, "Lookup", "lookup remains visible"),
        business_outcomes=(
            BusinessOutcome("VALIDATION_ERROR", "The application rejected the input.", "Validation error"),
            BusinessOutcome("MEMBER_NOT_FOUND", "No matching member exists.", "Member not found"),
            BusinessOutcome("PERMISSION_DENIED", "The operator is not permitted to view this record.", "Permission denied"),
            BusinessOutcome("SESSION_EXPIRED", "The application session expired.", "Session expired"),
        ),
    )


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Validation error", "VALIDATION_ERROR"),
        ("Member not found", "MEMBER_NOT_FOUND"),
        ("Permission denied", "PERMISSION_DENIED"),
        ("Session expired", "SESSION_EXPIRED"),
    ],
)
def test_replay_reports_expected_runtime_states_as_business_outcomes(tmp_path, text, code):
    surface = RuntimeStateSurface(text=text)
    result = ReplayRunner(
        surface,
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path),
        runtime_artifact(),
        inputs={"member_id": "1001"},
    ).run()

    assert result.status is RunStatus.BUSINESS_OUTCOME
    assert result.outcome_code == code
    assert surface.actions == 0


def test_replay_reports_unexpected_confirmation_dialog_as_hard_failure(tmp_path):
    result = ReplayRunner(
        RuntimeStateSurface(failure=UnexpectedDialog("confirmation dialog opened")),
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path),
        runtime_artifact(),
        inputs={"member_id": "1001"},
    ).run()

    assert result.status is RunStatus.HARD_FAILURE
    assert result.error_code == "UNEXPECTED_DIALOG"


def test_replay_retries_transient_slowness_and_returns_recoverable_failure(tmp_path):
    surface = RuntimeStateSurface(failure=SurfaceTimeout("slow load"))
    result = ReplayRunner(
        surface,
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path),
        runtime_artifact(),
        inputs={"member_id": "1001"},
    ).run()

    assert result.status is RunStatus.RECOVERABLE_FAILURE
    assert result.error_code == "TIMEOUT"
    assert surface.actions == 3


def test_replay_reports_an_outright_application_error_as_hard_failure(tmp_path):
    result = ReplayRunner(
        RuntimeStateSurface(failure=SurfaceAppError("application error page")),
        GuardrailPolicy.local_demo("http://127.0.0.1:8765"),
        EvidenceRecorder(tmp_path),
        runtime_artifact(),
        inputs={"member_id": "1001"},
    ).run()

    assert result.status is RunStatus.HARD_FAILURE
    assert result.error_code == "APP_ERROR"
