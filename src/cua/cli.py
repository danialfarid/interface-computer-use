from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from .demo_app import serve
from .discovery import DiscoveryRunner
from .evidence import EvidenceRecorder
from .handoff import HandoffCoordinator, HandoffServer
from .llm import LLMError, OpenAICompatibleClient
from .models import CapabilityArtifact, RunStatus
from .policy import GuardrailPolicy, PolicyViolation
from .replay import InputValidationError, ReplayRunner, _resolve_value
from .surface import BrowserSurface, SurfaceError
from .templates import member_balance_template


DEFAULT_URL = "http://127.0.0.1:8765/"
DEFAULT_ORIGIN = "http://127.0.0.1:8765"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cua", description="Computer-use discovery and deterministic replay")
    commands = parser.add_subparsers(dest="command", required=True)

    app_parser = commands.add_parser("demo-app", help="serve the local synthetic legacy portal")
    app_parser.add_argument("--host", default="127.0.0.1")
    app_parser.add_argument("--port", type=int, default=8765)

    discover = commands.add_parser("discover", help="run a live LLM discovery session")
    _add_target_options(discover)
    discover.add_argument("--goal", default="look up member {member_id} and read the current savings balance")
    discover.add_argument("--member-id", required=True)
    discover.add_argument("--evidence-dir", type=Path, default=Path("evidence"))
    discover.add_argument("--model", default=None)
    discover.add_argument("--max-steps", type=int, default=8)
    discover.add_argument("--handoff", action="store_true", help="start the localhost operator handoff API")
    discover.add_argument("--handoff-wait", type=float, default=300.0)
    discover.add_argument("--headed", action="store_true")

    replay = commands.add_parser("replay", help="replay a saved artifact without an LLM")
    replay.add_argument("--artifact", type=Path, required=True)
    replay.add_argument("--input", action="append", default=[], metavar="NAME=VALUE")
    replay.add_argument("--evidence-dir", type=Path, default=Path("evidence"))
    replay.add_argument("--handoff", action="store_true", help="start the localhost operator handoff API")
    replay.add_argument("--handoff-wait", type=float, default=300.0)
    replay.add_argument("--headed", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "demo-app":
            return _run_demo_app(args.host, args.port)
        if args.command == "discover":
            return _run_discover(args)
        if args.command == "replay":
            return _run_replay(args)
    except (LLMError, OSError, SurfaceError, PolicyViolation, ValueError) as exc:
        print(json.dumps({"status": "hard_failure", "error": str(exc)}), file=sys.stderr)
        return 1
    return 2


def _add_target_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target-url", default=DEFAULT_URL)


def _run_demo_app(host: str, port: int) -> int:
    server = serve(host, port)
    print(f"synthetic portal listening at http://{host}:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


def _run_discover(args: argparse.Namespace) -> int:
    target_url = args.target_url
    evidence = EvidenceRecorder(args.evidence_dir)
    parsed = urlparse(target_url)
    policy = GuardrailPolicy.local_demo(f"{parsed.scheme}://{parsed.netloc}")
    policy.check_url(target_url)
    demo_server = _ensure_demo_server(target_url)
    browser = BrowserSurface.open(
        target_url,
        headless=not args.headed,
        navigation_guard=policy.check_url,
    )
    template = member_balance_template(target_url)
    handoff_server = None
    try:
        coordinator = HandoffCoordinator(browser, policy, evidence) if args.handoff else None
        if coordinator is not None:
            handoff_server = HandoffServer(coordinator)
            handoff_server.start()
            print(f"operator handoff: {handoff_server.url}/interventions", flush=True)
        client = OpenAICompatibleClient(model=args.model)
        goal = args.goal.replace("{member_id}", args.member_id)
        result, artifact = DiscoveryRunner(
            browser,
            client,
            policy,
            evidence,
            template,
            parameter_values={"member_id": args.member_id},
            max_steps=args.max_steps,
            handoff=coordinator,
            handoff_wait_s=args.handoff_wait,
        ).run(goal)
        if artifact is not None:
            print(f"artifact: {evidence.directory / 'artifact.json'}")
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0 if result.status in {RunStatus.SUCCESS, RunStatus.BUSINESS_OUTCOME} else 1
    finally:
        if handoff_server is not None:
            handoff_server.close()
        browser.close()
        if demo_server is not None:
            demo_server.shutdown()
            demo_server.server_close()


def _run_replay(args: argparse.Namespace) -> int:
    artifact = CapabilityArtifact.from_dict(json.loads(args.artifact.read_text(encoding="utf-8")))
    inputs = _coerce_inputs(_parse_inputs(args.input), artifact)
    target_url = artifact.target["url"]
    try:
        target_url = _resolve_value(target_url, inputs, url=True) or target_url
    except InputValidationError:
        # ReplayRunner still returns the normal structured INVALID_INPUT result.
        pass
    if artifact.target.get("origin") != DEFAULT_ORIGIN:
        raise ValueError(f"replay target origin must be the approved demo origin: {DEFAULT_ORIGIN}")
    policy = GuardrailPolicy.local_demo(DEFAULT_ORIGIN)
    try:
        policy.check_url(target_url)
    except PolicyViolation as exc:
        raise ValueError(f"replay target is not an approved local demo URL: {target_url}") from exc
    demo_server = _ensure_demo_server(target_url)
    evidence = EvidenceRecorder(args.evidence_dir)
    browser = BrowserSurface.open(
        target_url,
        headless=not args.headed,
        navigation_guard=policy.check_url,
    )
    handoff_server = None
    try:
        coordinator = HandoffCoordinator(browser, policy, evidence) if args.handoff else None
        if coordinator is not None:
            handoff_server = HandoffServer(coordinator)
            handoff_server.start()
            print(f"operator handoff: {handoff_server.url}/interventions", flush=True)
        result = ReplayRunner(
            browser,
            policy,
            evidence,
            artifact,
            inputs=inputs,
            handoff=coordinator,
            handoff_wait_s=args.handoff_wait,
        ).run()
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0 if result.status in {RunStatus.SUCCESS, RunStatus.BUSINESS_OUTCOME} else 1
    finally:
        if handoff_server is not None:
            handoff_server.close()
        browser.close()
        if demo_server is not None:
            demo_server.shutdown()
            demo_server.server_close()


def _parse_inputs(items: list[str]) -> dict[str, str]:
    result = {}
    for item in items:
        name, separator, value = item.partition("=")
        if not separator or not name:
            raise ValueError(f"input must be NAME=VALUE: {item}")
        result[name] = value
    return result


def _coerce_inputs(inputs: dict[str, str], artifact: CapabilityArtifact) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in inputs.items():
        spec = artifact.parameters.get(name)
        if spec is not None and spec.type == "integer":
            try:
                result[name] = int(value)
            except ValueError as exc:
                raise ValueError(f"{name} must be an integer") from exc
        else:
            result[name] = value
    return result


def _ensure_demo_server(target_url: str):
    parsed = urlparse(target_url)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        return None
    probe_url = f"{parsed.scheme}://{parsed.netloc}/"

    class _NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, _request, _fp, _code, _msg, _headers, _new_url):
            return None

    try:
        with build_opener(_NoRedirect).open(Request(probe_url), timeout=0.5):
            return None
    except HTTPError:
        # Any HTTP response proves that the local server is already listening;
        # do not follow a route or redirect merely to check availability.
        return None
    except (OSError, URLError):
        if parsed.port is None:
            raise OSError("local target URL must include a port when starting the demo app")
        return serve(parsed.hostname, parsed.port)


if __name__ == "__main__":
    raise SystemExit(main())
