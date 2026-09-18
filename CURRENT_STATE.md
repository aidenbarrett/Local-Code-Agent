# Current state

Product direction: **conversation gateway and self-engineering loop**.
Date: 2026-09-18. Branch: `feature/conversation-gateway`.
Baseline: GitHub main `26fd55f0fc9705801e104d26afee377dac5db077`.

## Implemented prototype

The terminal session joins conversation to the existing controlled worker.
Natural-language turns can delegate repository inspection or configured builds
and tests. `/check` runs the Python self-check deterministically without asking a
model to choose commands. Separate chat/worker endpoint options exist.

Repository source edits, staging and commits are disabled on this path. Builds
and tests execute trusted checkout code and may themselves have side effects;
this is an operation policy boundary, not an OS sandbox.

## Run it

From an installed source checkout on Windows, prepare the existing owned server:

```powershell
.\chat.ps1 qwen3-8b-npu --ensure-only
.\local-code-agent.ps1 session --repo .
```

Allow configured commands in a trusted checkout:

```powershell
.\local-code-agent.ps1 session --repo . --allow-execution
```

Then try `Find internal/personas/aiden.toml and review it`, `What changed on this
branch?`, or `/check`. The profile must already have a reachable model server.
Server startup is still separate in v0; a unified lifecycle is milestone M1.

Model-free check with a nonzero exit code on failed/blocked verification:

```powershell
.\local-code-agent.ps1 session --repo . --allow-execution --check
```

Portable source-checkout invocation:

```sh
PYTHONPATH=internal python -m local_agent.session.cli --repo . --allow-execution --check
```

Two existing endpoints (explicit deployment choices, not automatic loading):

```powershell
.\local-code-agent.ps1 session --profile ptl-npu-8b --base-url http://127.0.0.1:8000/v1 --worker-profile nuc-llama-30b --worker-base-url http://127.0.0.1:8001/v1
```

Use the actual ports and model IDs configured by your servers. A friendly profile
name does not prove which device served a request. A cloud profile explicitly
sends conversation/repository content to its configured service; there is no
automatic cloud fallback.

## Limits

- Sequential turns; cannot type steering into a running task yet.
- No GUI, device utilisation collector, durable session or reconnect service.
- Ctrl-C exits; cooperative cancellation and process-tree cleanup are unfinished.
- Worker contexts reset. Conversation keeps bounded recent text, not durable facts
  or permissions. Ambiguous follow-ups may need clarification.
- Read results/answers may contain repository text. Do not enable an untrusted
  endpoint or checkout. Activity events omit raw tool arguments/output by default.
- Self-check proof covers tracked/nonignored file bytes and index/HEAD at the
  start/end of the check. It does not attest ignored dependencies, transient
  changes restored mid-run, external services, or operating system integrity.
- Python self-check has a separate result contract. The old configured `run_test`
  path remains CTest-oriented; use `/check` for LCA itself. No old result is rescored.
- Approval, workspace, scheduler, storage, telemetry and transport modules contain
  interfaces and development notes only. No mutation feature is hidden behind them.

## Validation and next action

Baseline CI was inspected: native Linux/Windows pytest and the compatibility job
failed at the persona temperature test. The test bypassed startup by mocking
`converse`; this branch exercises real startup and preserves both assertions.
Serving and instrument-integrity jobs on the baseline were green.

See [branch validation](internal/docs/conversation-branch-validation.md) for the
checks actually run. Recheck exact branch-head CI before describing it as green.
There is no claim of a successful live NPU conversation from this environment.

Next: laptop smoke of this prototype, then implement M1/M2 before enabling edits.
Measurement collection is paused. Frozen evidence remains untouched.
