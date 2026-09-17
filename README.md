# Computer-Use Automation System

This is a small, safety-first computer-use system for the interface.ai take-home assignment. It uses a synthetic, intentionally legacy-looking member-services portal as its live target. The system has one discovery path (LLM observes the browser and chooses constrained actions) and one production path (a saved capability artifact replays without an LLM).

Start with the [five-minute walkthrough](#five-minute-reviewer-walkthrough-after-setup), then the [requirement-to-evidence map](REVIEW.md). Design decisions and explicit limitations are in [REPORT.md](REPORT.md).

## Setup

The runtime is Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[browser,dev]'
.venv/bin/python -m playwright install chromium
```

Run the offline tests:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
```

[CI](https://github.com/danialfarid/interface-computer-use/actions/workflows/tests.yml) installs the project and Chromium on a clean Ubuntu/Python 3.11 runner and runs the same suite, including real-browser tests. It does not require secrets or call a model provider.

## Five-minute reviewer walkthrough (after setup)

Run these commands from the repository root. Keep port **8765** free, or run only this project's demo portal there; if another application owns it, stop your own conflicting application or use a clean environment. Do not widen the allowlist to work around a port conflict. The CLI starts and stops the portal for each replay when the port is free.

The following uses the actual committed provider-discovered artifact, remapped from its original ephemeral loopback port to the approved demo origin. **No API key or model call is needed for replay.**

```bash
ARTIFACT=evidence/live-discovery-2/9f81ee66f1b94f878c4579b3a9773a7d/artifact.json
for MEMBER in 1001 1002 9999; do
  env -u OPENAI_API_KEY .venv/bin/python -m cua.cli replay \
    --artifact "$ARTIFACT" --remap-to-approved-demo \
    --input member_id="$MEMBER" --evidence-dir tmp/reviewer-replay
done
```

| Input | Expected JSON result |
| --- | --- |
| `1001` | `status: success`, `outputs.current_savings_balance: "$1,240.50"` |
| `1002` | `status: success`, `outputs.current_savings_balance: "$85.19"` |
| `9999` | `status: business_outcome`, `outcome_code: MEMBER_NOT_FOUND` |

The two records are seeded in `src/cua/demo_app.py`; no database or admin creation step is needed. Values are synthetic. Caller-facing results contain the requested balance; persisted JSONL redacts it. Add `--headed` to watch the real browser fill, click, and extract.

Check the required runtime failure paths and same-session handoff without provider access:

```bash
.venv/bin/python -m pytest -o addopts='' -q \
  tests/test_browser_runtime_states.py tests/test_runtime_states.py \
  tests/test_replay.py tests/test_handoff.py
```

For genuine LLM discovery provenance, inspect the [artifact and discovery events](evidence/live-discovery-2/9f81ee66f1b94f878c4579b3a9773a7d/) and [matching replay evidence](evidence/live-replay/f4af7646c7d14a7c8f82f8b80778c8c9/). Their manifests identify the source revision and matching artifact SHA-256. These are recorded runs, not claims that offline tests call an LLM. The next section shows how to run your own discovery.

## Live discovery

Live discovery uses an OpenAI-compatible chat-completions endpoint. Set the key in the shell only; it is never accepted as a CLI argument, written to evidence, or committed.

```bash
export OPENAI_API_KEY='your-key'
# Optional:
export CUA_LLM_MODEL='gpt-4o-mini'
export CUA_LLM_ENDPOINT='https://api.openai.com/v1/chat/completions'
```

## Demo path

The CLI starts the local portal automatically when the default port is free. To run it separately instead:

```bash
PYTHONPATH=src .venv/bin/python -m cua.cli demo-app
```

Run a live discovery goal. The command prints the evidence directory and the saved artifact path.

```bash
PYTHONPATH=src .venv/bin/python -m cua.cli discover \
  --member-id 1001 \
  --goal 'look up member 1001 and read the current savings balance' \
  --evidence-dir evidence/live-discovery-2
```

Replay the resulting artifact. This command does not construct or call an LLM client.

```bash
PYTHONPATH=src .venv/bin/python -m cua.cli replay \
  --artifact evidence/live-discovery-2/<run-id>/artifact.json \
  --input member_id=1001 \
  --evidence-dir evidence/live-replay
```

Evidence captured on an ephemeral loopback port can be explicitly normalized to
the fixed approved demo origin with `--remap-to-approved-demo`; the flag accepts
only a credential-free loopback root URL and never broadens the allowlist.

The expected business outcome path is also deterministic:

```bash
PYTHONPATH=src .venv/bin/python -m cua.cli replay \
  --artifact evidence/example/discovery/artifact.json \
  --input member_id=9999 \
  --evidence-dir evidence/not-found-replay
```

The committed [example evidence](evidence/example/README.md) was made with the real local browser surface and an offline scripted decision client so the repository can be tested without a paid service. The committed `evidence/live-discovery-2/` and `evidence/live-replay/` directories contain redacted provider-backed discovery/replay evidence; each current fixture has a manifest with its code revision and artifact hash. No key or raw screenshot is persisted.

## Handoff

Pass `--handoff` to `discover` to start a localhost operator API. Handoff automatically opens a headed browser so the authorized operator can see the live page while acting; the page content is not written to evidence. When discovery reaches its step budget, it creates an intervention request, saves a redacted structural state snapshot, pauses, and waits for the operator to take control.

```bash
PYTHONPATH=src .venv/bin/python -m cua.cli discover \
  --member-id 1001 --handoff --handoff-wait 300
```

The printed endpoint supports `GET /interventions`, then `POST /interventions/{id}/take-control`, `POST /interventions/{id}/action`, and `POST /interventions/{id}/resume`. Actions go through the same live browser surface and guardrail policy. The localhost API is intentionally minimal and is not an internet-facing operator console. If the operator sends an `extract` action, the typed value is returned to the paused runner in memory; it is never written as raw evidence.

Every request needs the printed `X-CUA-Handoff-Token` header. Missing or invalid tokens return HTTP 401. The token grants access to this live session; do not save the startup output or token in shared logs. It is not a production operator-identity system.

### Repeatable authenticated handoff exercise

In terminal A, deliberately set the decision budget to zero so the runner pauses before any model request. The dummy key only satisfies client initialization; this exercise uses no paid API calls and is **not** LLM discovery evidence. It requires a local graphical display for the headed browser.

```bash
OPENAI_API_KEY=offline-handoff-demo .venv/bin/python -m cua.cli discover \
  --member-id 1001 --max-steps 0 --handoff --handoff-wait 300 \
  --evidence-dir tmp/reviewer-handoff
```

In terminal B, copy the base URL (without `/interventions`) from terminal A into `HANDOFF_URL`. Enter the printed token at the hidden prompt so its value is not saved in shell history. Run within the five-minute handoff deadline:

```bash
HANDOFF_URL=http://127.0.0.1:REPLACE_WITH_PRINTED_PORT
HANDOFF_TOKEN=$(.venv/bin/python -c 'import getpass; print(getpass.getpass("Handoff token: "))')
INTERVENTION=$(curl --fail --silent --show-error \
  -H "X-CUA-Handoff-Token: $HANDOFF_TOKEN" "$HANDOFF_URL/interventions" \
  | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["interventions"][0]["intervention_id"])')
handoff_post() {
  curl --fail --silent --show-error \
    -H "X-CUA-Handoff-Token: $HANDOFF_TOKEN" -H 'Content-Type: application/json' \
    --data "$2" "$HANDOFF_URL/interventions/$INTERVENTION/$1"
}
handoff_post take-control '{"operator":"reviewer"}'
handoff_post action '{"action":{"action":"fill","target":{"strategy":"label","value":"Member ID"},"value":"1001"}}'
handoff_post action '{"action":{"action":"click","target":{"strategy":"role","value":"button:Search"}}}'
handoff_post action '{"action":{"action":"extract","target":{"strategy":"css","value":"[id=\"balance-value\"]"},"value":"current_savings_balance"}}'
handoff_post resume '{}'
unset HANDOFF_TOKEN
```

Watch the same browser session change as the API actions run. Terminal A should report `success` and an assisted artifact path; JSONL records pause, human actions, and resume without raw values. If listing is empty, wait until terminal A has paused and list again. An expired request requires a fresh run. Use the API for actions in this exercise so they are recorded, rather than untracked manual clicks in the browser.

Replay can expose the same handoff boundary when a fixed step is blocked or the surface fails. It also opens a headed browser when `--handoff` is present:

```bash
PYTHONPATH=src .venv/bin/python -m cua.cli replay \
  --artifact evidence/live-discovery-2/<run-id>/artifact.json \
  --input member_id=1001 --handoff --handoff-wait 300
```

After taking control, the operator must complete or repair the blocked step in the same browser session before resuming.

## Project layout

- `src/cua/models.py` - versioned capability, locator, checkpoint, and result contracts.
- `src/cua/surface.py` - browser surface adapter; semantic locators precede CSS fallbacks.
- `src/cua/discovery.py` - observe/decide/act loop and artifact recording.
- `src/cua/replay.py` - model-free deterministic executor and outcome taxonomy.
- `src/cua/handoff.py` - pause, ownership transfer, same-session operator actions, and resume.
- `src/cua/policy.py` and `src/cua/redaction.py` - allowlist, risk confirmation, and persistence-boundary safety.
- `src/cua/demo_app.py` - local synthetic target with search, detail, not-found, and explicit runtime-state fixtures.
- `schemas/capability.schema.json` - reviewable JSON Schema for the saved artifact.
- `REPORT.md` - design decisions, trade-offs, heterogeneity story, safety model, and cuts.
- `evidence/` - redacted run evidence and saved artifacts.
