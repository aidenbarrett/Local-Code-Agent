# Delivery backlog: one conversation, useful engineering work

> Superseded implementation sequence. Use [the session hub file/PR layout](session-hub-file-layout.md)
> and [current design](session-hub-design.md). Preserve the material below as first-pass
> history; do not start a web/desktop transport or follow M0-M5 from this document.

Execute in dependency order. This is the short-term product plan, not an
experimental preregistration. Do not collect comparison rows from these workflows.

## M0: branch foundation (implemented here)

Files: `local_agent/session/{contracts,events,gateway,controller,selfcheck,cli}.py`.
Entry: `.\local-code-agent.ps1 session`. Separate chat/worker contexts; fixed repo;
read/configured execution; strict proposals; typed task results; Python self-check.
Tests use real tools, controlled model responses and real pytest subprocesses.

Exit gate: branch CI plus laptop inspection/check smoke. The laptop gate is still
open. Hardware model quality and JSON adherence have not been established here.

## M1: make the terminal path comfortable and reliable

Dependencies: M0.

1. Factor shared server ownership/runtime environment out of `scripts/chat.py`
   into a product runtime service. Direct chat and session both use it. Preserve
   the existing unmanaged-server refusal and device/model compatibility checks.
2. Make `.\local-code-agent.ps1 session` start/reuse the default model once.
   Endpoint overrides explicitly opt out of managed startup. Starting a worker
   profile on an occupied incompatible port must fail with a specific diagnosis.
3. Add a session doctor: config, skills, Python/pytest, endpoint health, served
   model identity, context/tool compatibility and effective permissions. These
   are checks, not benchmark runs. Keep startup repair instructions on root UI.
4. Add separate conversation and worker prompt/model configuration. Persona stays
   conversation-only and cannot modify proposals, controller grants or proof.
5. Implement deterministic follow-up references to task IDs and retained logs.
   Expose “why did that fail?” with compiler/test details rather than only a
   generic previous answer. Budget and page artifacts; label stale results.
6. Add a regression scenario: find persona -> inspect -> check LCA -> explain a
   known failure -> inspect relevant test. Capture transcript only by opt-in.

Exit gate: on the actual laptop, one command starts the owned runtime and supports
the scenario, reports unreachable endpoints cleanly, and never executes a malformed
proposal. Document observed failures rather than loosening verification to pass.

## M2: service, state, scheduling and interruption

Dependencies: M1. Skeletons: `scheduler.py`, `storage.py`, `transport.py`.

1. Introduce `SessionService` with a bounded command queue and single controller
   owner per session. Model/tool execution goes to managed workers; control input
   stays responsive. Control messages must not require a chat-model round trip.
2. Implement the task state machine and revision guards from the architecture.
   Add shared endpoint leases and explicit per-call/time/token budgets.
3. Implement SQLite store plus immutable artifact directories. Record admission
   before effects. Persist transition+event atomically. Build schema migrations,
   retention and explicit opt-in for sensitive transcript content.
4. Add idempotency keyed by request ID + payload hash. Test duplicate submit,
   lost response, reconnection, stale revision and conflicting duplicate payload.
5. Add cancellation tokens to orchestrator dispatch and a process-tree-owning
   runner. Preserve default experimental behaviour; product use opts into the
   new mechanism. Document/recompute source identity if shared code changes.
6. Add stdio JSON-RPC with version handshake, request bounds, event replay and
   capability negotiation. Reject unknown versions/methods; no arbitrary method
   invocation or shell transport.
7. Fault tests: crash before/after command start, disk full, event gap, slow client,
   server stall, cancel while queued/inference/building, restart after interruption.

Exit gate: user can continue typing while a task runs; stop is acknowledged
immediately and completion accurately reports cleanup. Reconnect cannot execute
the same task twice. Two sessions cannot race the same writable workspace.

## M3: LCA can safely help change LCA

Dependencies: M2. Skeletons: `workspaces.py`, `approvals.py`.

1. Implement isolated candidate worktrees with controller checkout pinned apart.
   Record baseline dirty/index state, config/skills, candidate commit and hashes.
2. Add operation-scoped grants and immutable file exclusions in controller state.
   Use path resolution at the actual mutation boundary, including symlinks,
   traversal, case sensitivity and platform separator tests.
3. Implement exact patch proposals, preimage checks and journal-owned rollback.
   Approval binds to exact diff/action, tree, workspace and policy revision.
4. Fence all writes after a scope revision. Test “leave README alone” during
   planning, awaiting approval, before apply and after apply has already started.
5. Generalise verification adapters without changing frozen CTest semantics.
   Add Python targeted vs full checks, deterministic structured failures and
   fresh-source/environment identity. Pin external verification manifests for
   self-engineering so changing tests cannot silently lower the success bar.
6. Add candidate review: diff, source changes, verification changes, test evidence,
   warnings and unverified claims. Disable apply/commit if identity is stale.
7. Add finalisation separate from speculative execution: exact selected paths,
   user index preserved, explicit approval coverage, no automatic push.
8. Define controller update handoff: finish task, review candidate commit, restart
   from approved version, re-read state with compatible schema. Never hot-reload.

Exit gate: a seeded real LCA bug is diagnosed, fixed in a candidate, rejected by
the old test first, passes the unchanged regression plus appropriate suite, and
is presented as a reviewable commit without touching unrelated user changes.
That is the first self-engineering milestone. Do not claim it from M0's skeletons.

## M4: desktop surface and honest telemetry

Dependencies: M2; edit controls additionally require M3.
Skeleton: `telemetry.py`. UI brief: `internal/ui/README.md`.

1. Build terminal panes over the event stream first, proving reconnect/state flow.
2. Implement loopback commands/SSE with per-launch token and Origin restrictions.
   Keep desktop integration replaceable; choose packaging after a Windows spike.
3. Desktop chat, grouped activity, task result cards, diff/log views, pending action
   cards and accessible keyboard navigation. UI state derives from controller state.
4. Add CPU/RAM sampling, then validate NPU/GPU collectors on the laptop. Distinguish
   unavailable/stale/system-wide/task-attributed fields. Export operational data
   only with source/timestamp/host labels, outside frozen measurement artifacts.
5. Test slow render, window close/reopen, daemon crash, disconnected event stream,
   ANSI/control characters in logs and malicious repository HTML/Markdown.

Exit gate: restarting the UI does not lose/duplicate work; verification badges
cannot be created by chat text; missing hardware counters visibly show N/A.

## M5: VS Code Remote SSH and stronger endpoints

Dependencies: M2/M3. Brief: `internal/vscode/README.md`.

1. VS Code extension runs on the host owning the selected workspace. Launch the
   gateway over stdio there. Under Remote SSH, filesystem tools run on Linux.
2. Configure explicit tunnels to the Windows model host with least necessary
   binding, authentication and endpoint health. Never infer local paths as remote.
3. Resolve repo IDs to host+canonical path. Namespace artifacts and telemetry by
   host. Handle disconnect with reconciliation, not blind replay.
4. Add role capability registry and reuse TieredClient through the endpoint
   scheduler. Explicit strong-tier policy, token/time ceilings and eligible causes.
5. Preserve speculative mutation rollback and evidence replay constraints. Test
   restoration failure, changed base, server fallback, budget exhaustion and
   incorrect model/device identity. No cloud request from a local-only session.

Exit gate: inspect/build/test the actual Linux checkout using an explicitly
configured Windows endpoint, with clear host attribution and no path confusion.

## Validation matrix for all milestones

| Area | Required adversarial cases |
|---|---|
| Intent | invalid JSON, extra fields, tool calls, ambiguous pronouns, oversized Unicode |
| Policy | denied tools, repository config broader than session, injected instructions |
| Evidence | stale tree, targeted vs full, zero/all-skipped tests, missing report, unknown ID |
| Writes | symlink escape, preimage changed, exclusions changed, unrelated staged files |
| Concurrency | duplicate requests, two sessions, endpoint aliases, late terminal events |
| Recovery | controller death, failed persistence, reconnect after effect, partial rollback |
| Remote | wrong host, tunnel dropped, stale task state, credential/log redaction |
| Runtime | no endpoint, wrong served model, overflow, stalls, unknown counters |

Re-enable measurement only after the product loop is useful and stable. Freeze a
new experiment definition for any future product-policy/adapter comparison; do
not retrofit these behaviours into historical smoke or pilot artifacts.
