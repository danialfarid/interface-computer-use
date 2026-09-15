# 1. Architecture

The system is a small synchronous Python process with four boundaries: a `Surface` adapter, a discovery runner, a persisted capability artifact, and a deterministic replay runner. The concrete surface is Playwright over a local synthetic member-services portal. The portal uses tables, ordinary labels, and no test IDs to approximate the legacy web surfaces in the brief without touching real banking software or a public site.

Discovery receives a goal and target URL. It observes the current page as URL, title, visible text, interactive controls, and readable targets. The LLM returns strict-schema JSON actions that may reference only ephemeral IDs from that observation. The runner resolves those IDs immediately, checks policy, performs the action, and records the resolved stable locator. After a checkpoint is verified, the runner writes a capability artifact that contains no model transcript and no runtime input value.

Replay loads that artifact and never asks a model what to do. It validates typed inputs, substitutes placeholders, checks the target and every step against policy, performs the fixed sequence, extracts declared outputs, and verifies the checkpoint. Evidence is append-only JSONL plus a redacted DOM/accessibility text snapshot on failure. The process is intentionally synchronous: a queue, database, worker fleet, and service-to-service protocol would add operational surface without improving this vertical slice.

# 2. Artifact schema

`schemas/capability.schema.json` describes a versioned `CapabilityArtifact`. Its important fields are:

- `parameters` gives the agent-invocable input contract, including type and requiredness.
- `steps` is an ordered list of typed actions. Each step has a stable locator, rationale, timeout, risk class, and a parameter placeholder such as `{{member_id}}`.
- `outputs` names typed values and points to a stable readable locator. Output locators that contain dynamic digits are rejected during discovery so a displayed value cannot become part of the artifact.
- `checkpoint` is a positive assertion such as `text_present: Member details`.
- `business_outcomes` distinguishes a known caller-visible result such as `MEMBER_NOT_FOUND` from an automation failure.
- `surface_kind`, target origin, `schema_version`, and `artifact_version` make the capability reviewable and give future adapters a compatibility seam.

The artifact records the resolved locator, not the raw LLM transcript. A label locator is preferred for form controls, then an accessible role/name locator, then a named CSS fallback. This makes the recorded decision inspectable while avoiding brittle coordinates and generated DOM positions.

# 3. Determinism & error handling

Replay has one fixed action sequence, one input substitution function, bounded timeouts, and no model in its decision loop. Each locator can include ordered fallbacks; resolution requires a match before an action is attempted. Navigation is checked against the origin and route allowlist. Completion requires the declared checkpoint, so a successful click alone is never treated as success.

The result contract has four useful classes: `success` includes outputs, `business_outcome` includes a known code such as `MEMBER_NOT_FOUND`, `recoverable_failure` reports a bounded timeout retry failure, and `hard_failure` reports the failed step, code, message, and evidence directory. Timeouts are retried twice and then returned as recoverable; locator errors, invalid inputs, policy blocks, and checkpoint failures stop immediately. The local portal demonstrates both success and not-found behavior in `evidence/example/`.

The current implementation treats layout drift as a hard failure after locator fallbacks. In a production system, match telemetry and checkpoint failures would be used to quarantine a capability for review rather than silently recording a new flow.

# 4. Heterogeneity & multi-tenant

The seam is `SurfaceObservation` plus `perform`/`extract`/`capture`; the artifact does not depend on Playwright. A legacy web adapter could implement the same contract with accessibility-tree nodes, frame-aware locators, or screenshot/coordinate actions. A desktop adapter could map the contract to OS accessibility APIs. The current artifact supports a new surface through `surface_kind` and locator strategies without changing the caller-facing parameters, outputs, or checkpoint contract.

For tenant reuse, a production catalog would store a base artifact keyed by vendor application and compatible version, plus a tenant policy and small ordered locator overrides. The tenant-specific target origin and allowed routes stay outside the generic flow. On each replay, locator match failures, checkpoint failures, and observed app/version metadata become drift signals. A specialization is reviewed and versioned; it is not silently learned from a failed run. That is a design story rather than unimplemented multi-tenant infrastructure, as requested.

# 5. Escalation & handoff

Discovery escalates after its bounded step budget, carrying the goal, capability, current step, current observation, and a redacted state snapshot. `HandoffCoordinator` uses an explicit ownership state: `requested -> human_control -> resumed`. `HandoffServer` is a localhost operator surface in the same process. The operator takes control, sends a policy-checked action, and resumes the runner; the action is applied to the same `BrowserSurface` and browser context, not a fresh session. The runner then gets one bounded continuation budget and can finish or escalate again as a clear result.

This is enough to make the control-transfer seam real while avoiding a full co-browsing UI. The current operator API has no authentication because it binds to loopback and is a take-home demonstration; production would add authenticated operator identity, leases, audit retention, and a browser-streaming surface.

# 6. Safety

`GuardrailPolicy` explicitly allowlists origins, route prefixes, and action types. Risk is part of every action step; risky actions require a separate confirmation flag, and a deployment policy can classify action types such as submit or transfer as risky. Human actions use the same policy checks. The demo capability has only read/search behavior and never reaches an irreversible action.

Runtime values are parameterized before artifact persistence. Evidence passes through recursive redaction for identifier-like numbers, currency, emails, phones, tokens, and passwords. Dynamic text locators are rejected for outputs. Failure evidence defaults to a redacted DOM/accessibility snapshot; screenshots are opt-in via `CUA_PERSIST_SCREENSHOTS=1` because pixels can contain regulated data. The demo data is synthetic, and the README never requires real credentials or PII.

# 7. Cuts

I deliberately left out queues, a database, tenant configuration storage, desktop implementation, public-site automation, a multimodal vision model, automatic artifact approval, automatic self-healing, and a full operator console. They do not improve the required one-surface vertical slice. With more time, I would add authenticated operator leases, artifact approval and replay-stability metrics, a second hostile surface adapter, and a bounded single-step recovery mode that is itself policy checked and fully evidenced.
