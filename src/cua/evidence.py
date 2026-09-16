from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus, unquote
import uuid

from .redaction import redact_artifact_payload, redact_runtime_url, redact_url, redact_value


def _redact_sensitive_names(value: Any, names: set[str]) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "<REDACTED>" if str(key) in names else _redact_sensitive_names(item, names)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive_names(item, names) for item in value]
    return value


def _redact_sensitive_values(value: Any, sensitive_values: set[str]) -> Any:
    if isinstance(value, str):
        result = value
        variants = {
            variant
            for item in sensitive_values
            if item
            for variant in (item, quote(item, safe=""), quote_plus(item))
        }
        for secret in sorted(variants, key=len, reverse=True):
            result = result.replace(secret, "<REDACTED>")
        return result
    if isinstance(value, dict):
        return {str(key): _redact_sensitive_values(item, sensitive_values) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_sensitive_values(item, sensitive_values) for item in value]
    return value


def _redact_readable_targets(value: Any) -> Any:
    if isinstance(value, dict):
        result = {str(key): _redact_readable_targets(item) for key, item in value.items()}
        targets = result.get("readable_targets")
        if isinstance(targets, list):
            for target in targets:
                if not isinstance(target, dict):
                    continue
                if "text" in target:
                    target["text"] = "<REDACTED>"
                locator = target.get("locator")
                if isinstance(locator, dict) and locator.get("strategy") == "text":
                    locator["value"] = "<REDACTED>"
        return result
    if isinstance(value, list):
        return [_redact_readable_targets(item) for item in value]
    return value


def _redact_control_metadata(value: Any) -> Any:
    """Keep visible names, labels, and unsafe selector text out of evidence."""

    if isinstance(value, dict):
        result = {str(key): _redact_control_metadata(item) for key, item in value.items()}
        controls = result.get("controls")
        if isinstance(controls, list):
            for control in controls:
                if isinstance(control, dict) and "name" in control:
                    control["name"] = "<REDACTED>"
        if isinstance(result.get("text"), str) and "controls" in result:
            result["text"] = "<REDACTED>"
            if "title" in result:
                result["title"] = "<REDACTED>"
        locators = [result.get("locator"), result if "strategy" in result else None]
        for locator in locators:
            if not isinstance(locator, dict):
                continue
            strategy = locator.get("strategy")
            locator_value = locator.get("value")
            decoded_value = unquote(locator_value) if isinstance(locator_value, str) else ""
            unsafe_url_selector = (
                strategy == "css"
                and isinstance(locator_value, str)
                and "href=" in locator_value
                and ("?" in locator_value or "&" in locator_value)
            )
            if (
                isinstance(locator_value, str)
                and any(ord(character) > 127 for character in decoded_value)
            ) or unsafe_url_selector:
                locator["value"] = "<REDACTED>"
        return result
    if isinstance(value, list):
        return [_redact_control_metadata(item) for item in value]
    return value


def _redact_runtime_urls(value: Any) -> Any:
    if isinstance(value, dict):
        result = {str(key): _redact_runtime_urls(item) for key, item in value.items()}
        for key in ("url", "current_url", "origin"):
            candidate = result.get(key)
            if isinstance(candidate, str) and "://" in candidate:
                result[key] = redact_runtime_url(candidate)
        return result
    if isinstance(value, list):
        return [_redact_runtime_urls(item) for item in value]
    return value


def _redact_freeform_metadata(value: Any) -> Any:
    """Do not persist model/operator prose that may contain regulated data."""

    if isinstance(value, dict):
        result = {str(key): _redact_freeform_metadata(item) for key, item in value.items()}
        for key in ("goal", "reason", "message", "description"):
            if isinstance(result.get(key), str):
                result[key] = "<REDACTED>"
        return result
    if isinstance(value, list):
        return [_redact_freeform_metadata(item) for item in value]
    return value


class EvidenceRecorder:
    """Append-only run evidence with redaction at the persistence boundary."""

    def __init__(self, root: Path, *, run_id: str | None = None):
        self.run_id = run_id or uuid.uuid4().hex
        self.directory = root / self.run_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.log_path = self.directory / "events.jsonl"
        self.sensitive_names: set[str] = set()
        self.sensitive_values: set[str] = set()

    def event(self, kind: str, **payload: Any) -> None:
        sensitive_output = payload.get("name") in self.sensitive_names
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "kind": kind,
            "payload": self.redact_payload(payload),
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

    def redact_payload(self, payload: Any) -> Any:
        return _redact_freeform_metadata(_redact_runtime_urls(_redact_control_metadata(
            _redact_readable_targets(
                _redact_sensitive_values(
                    _redact_sensitive_names(redact_value("payload", payload), self.sensitive_names),
                    self.sensitive_values,
                )
            )
        )))

    def redact_url(self, value: str) -> str:
        return redact_runtime_url(_redact_sensitive_values(redact_url(value), self.sensitive_values))

    def json_file(self, name: str, payload: Any) -> Path:
        path = self.directory / name
        path.write_text(json.dumps(self.redact_payload(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
