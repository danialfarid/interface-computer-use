from pathlib import Path

from cua.evidence import EvidenceRecorder
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
from cua.replay import ReplayRunner
from cua.surface import SurfaceObservation


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
                "string", "Current savings balance", Locator("text", "Current savings balance")
            )
        },
        steps=(
            ActionStep("fill", ActionType.FILL, Locator("label", "Member ID"), "{{member_id}}"),
            ActionStep("search", ActionType.CLICK, Locator("role", "button:Search")),
            ActionStep("read", ActionType.EXTRACT, Locator("text", "Current savings balance"), "balance"),
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
