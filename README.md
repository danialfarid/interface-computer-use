# Computer-Use Automation System

This is a small, safety-first computer-use system for the interface.ai take-home assignment. It uses a synthetic, intentionally legacy-looking member-services portal as its live target. The system has one discovery path (LLM observes the browser and chooses constrained actions) and one production path (a saved capability artifact replays without an LLM).

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
  --evidence-dir evidence/live-discovery
```

Replay the resulting artifact. This command does not construct or call an LLM client.

```bash
PYTHONPATH=src .venv/bin/python -m cua.cli replay \
  --artifact evidence/live-discovery/<run-id>/artifact.json \
  --input member_id=1001 \
  --evidence-dir evidence/live-replay
```

The expected business outcome path is also deterministic:

```bash
PYTHONPATH=src .venv/bin/python -m cua.cli replay \
  --artifact evidence/example/discovery/artifact.json \
  --input member_id=9999 \
  --evidence-dir evidence/not-found-replay
```

The committed [example evidence](evidence/example/README.md) was made with the real local browser surface and an offline scripted decision client so the repository can be tested without a paid service. The committed `evidence/live-discovery-2/` and `evidence/live-replay/` directories contain one redacted provider-backed discovery and its deterministic replay. No key or raw screenshot is persisted.

## Handoff

Pass `--handoff` to `discover` to start a localhost operator API. When discovery reaches its step budget, it creates an intervention request, saves a redacted state snapshot, pauses, and waits for the operator to take control.

```bash
PYTHONPATH=src .venv/bin/python -m cua.cli discover \
  --member-id 1001 --handoff --handoff-wait 300
```

The printed endpoint supports `GET /interventions`, then `POST /interventions/{id}/take-control`, `POST /interventions/{id}/action`, and `POST /interventions/{id}/resume`. Actions go through the same live browser surface and guardrail policy. The localhost API is intentionally minimal and is not an internet-facing operator console.

## Project layout

- `src/cua/models.py` - versioned capability, locator, checkpoint, and result contracts.
- `src/cua/surface.py` - browser surface adapter; semantic locators precede CSS fallbacks.
- `src/cua/discovery.py` - observe/decide/act loop and artifact recording.
- `src/cua/replay.py` - model-free deterministic executor and outcome taxonomy.
- `src/cua/handoff.py` - pause, ownership transfer, same-session operator actions, and resume.
- `src/cua/policy.py` and `src/cua/redaction.py` - allowlist, risk confirmation, and persistence-boundary safety.
- `src/cua/demo_app.py` - local synthetic target with search, detail, and not-found states.
- `REPORT.md` - design decisions, trade-offs, heterogeneity story, safety model, and cuts.
- `evidence/` - redacted run evidence and saved artifacts.
