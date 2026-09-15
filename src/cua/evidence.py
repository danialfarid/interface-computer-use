from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
import uuid

from .redaction import redact_value


class EvidenceRecorder:
    """Append-only run evidence with redaction at the persistence boundary."""

    def __init__(self, root: Path, *, run_id: str | None = None):
        self.run_id = run_id or uuid.uuid4().hex
        self.directory = root / self.run_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.log_path = self.directory / "events.jsonl"

    def event(self, kind: str, **payload: Any) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "kind": kind,
            "payload": redact_value(kind, payload),
        }
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")

    def json_file(self, name: str, payload: Any) -> Path:
        path = self.directory / name
        path.write_text(json.dumps(redact_value(name, payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def artifact_file(self, artifact: Any) -> Path:
        """Persist an artifact after the caller has built its typed contract."""

        path = self.directory / "artifact.json"
        path.write_text(artifact.to_json(), encoding="utf-8")
        return path

    def failure_snapshot(self, surface: Any, label: str) -> dict[str, str]:
        try:
            screenshot, snapshot = surface.capture(self.directory, label)
            self.event(
                "failure_snapshot",
                screenshot=str(screenshot.name) if screenshot is not None else None,
                snapshot=str(snapshot.name),
            )
        except Exception as exc:
            self.event("failure_snapshot_unavailable", error=str(exc))
            screenshot = None
            snapshot = None
        return {
            "screenshot": str(screenshot) if screenshot is not None else "",
            "snapshot": str(snapshot) if snapshot is not None else "",
        }
