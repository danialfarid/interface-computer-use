# Requirement-to-evidence map

Scope: one synthetic member-balance lookup, real Chromium interaction, provider-backed discovery, model-free replay, and same-session human intervention. No real banking credentials or data are required. See the [walkthrough](README.md#five-minute-reviewer-walkthrough-after-setup) and [trade-offs](REPORT.md).

For each test entry below, run `.venv/bin/python -m pytest -o addopts='' -q` followed by the listed paths. Tests use fake providers or deterministic surfaces where indicated; they do not incur provider charges.

| Brief requirement | Implementation / status | Verification |
| --- | --- | --- |
| 3.1 Goal + target; real observe/decide/act | `src/cua/cli.py`, `discovery.py`, `llm.py`, `surface.py`. Implemented for the approved demo and balance task, not arbitrary applications. | `tests/test_discovery.py tests/test_llm.py`; genuine provider [discovery events](evidence/live-discovery-2/9f81ee66f1b94f878c4579b3a9773a7d/events.jsonl) and [manifest](evidence/live-discovery-2/9f81ee66f1b94f878c4579b3a9773a7d/manifest.json). |
| 3.2 Typed, versioned capability; inputs, outputs, targeting, checkpoint | `src/cua/models.py`, `schemas/capability.schema.json`. Implemented and validated at persistence/load boundaries. | `tests/test_models.py`; inspect the [saved artifact](evidence/live-discovery-2/9f81ee66f1b94f878c4579b3a9773a7d/artifact.json). |
| 3.3 Deterministic replay and balance extraction | `src/cua/replay.py`. Fixed steps, input substitution, typed outputs, checkpoint, no model client. | `tests/test_replay.py tests/test_cli.py tests/test_surface_locators.py`; replay the same artifact with three inputs in the walkthrough; [recorded replay](evidence/live-replay/f4af7646c7d14a7c8f82f8b80778c8c9/events.jsonl). |
| 3.3 Runtime errors, not only happy path | Explicit business outcomes, bounded safe retries, hard failures, optional handoff. Implemented; detection is demo-specific. | `tests/test_browser_runtime_states.py tests/test_runtime_states.py tests/test_replay.py`; scenario matrix below. |
| 3.4 Allowlist, risky actions, sensitive-data boundaries | `src/cua/policy.py`, `surface.py`, `redaction.py`, `llm.py`. Configurable policy object; CLI intentionally pins the approved demo. Operator actions share policy enforcement. | `tests/test_policy.py tests/test_redaction.py tests/test_llm.py tests/test_browser_runtime_states.py tests/test_handoff.py`; redacted provider and replay events above. |
| 3.5 Structured evidence plus richer failure state | `src/cua/evidence.py`, `surface.py`. JSONL and sanitized structural snapshots; no persisted raw screenshots. | `tests/test_discovery.py tests/test_browser_runtime_states.py tests/test_redaction.py`; failure tests generate and inspect temporary snapshots. Historical failure fixtures are not needed to reproduce these checks. |
| 3.6 Detect stuck, transfer live control, resume with evidence | `src/cua/handoff.py`, discovery/replay integration. Token-protected localhost API, ownership states, owner-thread browser actions, deadline. Implemented; operator UI is bare HTTP plus headed browser. | `tests/test_handoff.py tests/test_discovery.py tests/test_replay.py`; [authenticated live exercise](README.md#repeatable-authenticated-handoff-exercise). Most handoff tests use deterministic fake surfaces; the exercise demonstrates the real browser. |
| 3.7 Heterogeneous surfaces and multi-tenant reuse | **Design only**, as permitted by the brief. No desktop/vision adapter or tenant catalog. Current locators and approved vocabulary remain demo-specific. | [REPORT section 4](REPORT.md#4-heterogeneity--multi-tenant): adapter/schema extension, reviewed tenant specialization, drift handling, and honest no-clean-DOM limitation. No cross-tenant execution is claimed. |

## Runtime scenario matrix

| Scenario | Expected handling | Test evidence |
| --- | --- | --- |
| Record not found | `business_outcome / MEMBER_NOT_FOUND` | `tests/test_replay.py`; real-browser walkthrough with `9999`. |
| Validation error | `business_outcome / VALIDATION_ERROR` | `tests/test_browser_runtime_states.py`, `tests/test_runtime_states.py`. |
| Permission denial | `business_outcome / PERMISSION_DENIED`; no permission bypass | Same browser/runtime suites. |
| Session expiry | `business_outcome / SESSION_EXPIRED`; no automatic login | Same browser/runtime suites. |
| Unexpected confirmation | `hard_failure / UNEXPECTED_DIALOG`; do not silently accept | Same browser/runtime suites. |
| Transient slowness | Safe bounded retries; success on recovery or `recoverable_failure / TIMEOUT` when exhausted | `tests/test_replay.py`, `tests/test_runtime_states.py` use deterministic injected timeouts, not a real slow network. |
| Ambiguous click timeout | No automatic duplicate click | `tests/test_replay.py`. |
| App error | `hard_failure / APP_ERROR` and sanitized failure snapshot | Browser/runtime suites. |

## Provenance and cut lines

The linked discovery and replay manifests identify source revision `2e3a56c6ccc8ca33340ab5e310a1d9716b0d3930`. The matching artifact hash ties replay to that discovery. Later evidence/docs/CI-only commits do not mean the model was rerun at those commits. Other checked-in runs are historical; `evidence/example/` is explicitly scripted, not provider evidence.

CI checks the offline suite on a clean Linux checkout with Chromium; it does not repeat paid discovery. A green CI run is test evidence, not independent design approval or proof of production readiness. No modern UI, CRUD/admin workflow, database, tenant infrastructure, or second adapter was added merely for breadth.
