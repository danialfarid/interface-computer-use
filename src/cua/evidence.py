from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
import uuid

from .redaction import redact_artifact_payload, redact_value


def _redact_sensitive_names(value: Any, names: set[str]) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "<REDACTED>" if str(key) in names else _redact_sensitive_names(item, names)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive_names(item, names) for item in value]
    return value


class EvidenceRecorder:
    """Append-only run evidence with redaction at the persistence boundary."""

    def __init__(self, root: Path, *, run_id: str | None = None):
        self.run_id = run_id or uuid.uuid4().hex
        self.directory = root / self.run_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.log_path = self.directory / "events.jsonl"
        self.sensitive_names: set[str] = set()

    def event(self, kind: str, **payload: Any) -> None:
        sensitive_output = payload.get("name") in self.sensitive_names
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "kind": kind,
            "payload": _redact_sensitive_names(redact_value(kind, payload), self.sensitive_names),
        }
        redacted_step = record["payload"].get("step")
        raw_step = payload.get("step")
        if (
            isinstance(redacted_step, dict)
            and isinstance(raw_step, dict)
            and raw_step.get("action") in {"fill", "navigate"}
            and "value" in redacted_step
        ):
            redacted_step["value"] = "<REDACTED>"
        if sensitive_output and "value" in record["payload"]:
            record["payload"]["value"] = "<REDACTED>"
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")

    def json_file(self, name: str, payload: Any) -> Path:
        path = self.directory / name
        path.write_text(json.dumps(redact_value(name, payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def artifact_file(self, artifact: Any) -> Path:
        """Persist an artifact after the caller has built its typed contract."""

        artifact.validate()
        path = self.directory / "artifact.json"
        payload = redact_artifact_payload(artifact.to_dict())
        if payload.get("target", {}).get("url") != artifact.to_dict().get("target", {}).get("url"):
            raise ValueError("artifact target URL contains credentials or an unparameterized secret")
        # Validate the exact payload that will be written. Redaction must never
        # turn a reviewable artifact into a contract the loader cannot accept.
        from .models import CapabilityArtifact

        CapabilityArtifact.from_dict(payload)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
