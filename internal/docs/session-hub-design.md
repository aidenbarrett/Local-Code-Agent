# Session hub: accepted design for implementation PRs

2026-09-18. Design-only follow-up to `d4a4cfa641dfc4b74a61e233abb4290e95a4636e`.
Live main inspected: `26fd55f0fc9705801e104d26afee377dac5db077`.

This document and `session-contract/README.md` supersede conflicting choices in
the earlier conversation-product architecture/backlog. The existing prototype
remains unchanged and is **not** an implementation of this contract. No new
runtime, schedule, terminal configuration or measurement is enabled by this PR.

## Five decisions

| Question | Decision | Why |
|---|---|---|
| Process ownership | Textual, gateway and controller in one interactive process | Direct typed observer subscription; no protocol server, UI IPC or web stack |
| Is a task a turn? | No. Immutable task/result artifacts are siblings referenced by turns | Resume displays facts without replaying effects; task lifetime is independent of a chat reply |
| Minimum useful activity | Route, admission, state, endpoint wait/lease, tool start/end, verdict, task close | These explain what was selected, what ran, where it waits and what was proved |
| Watch delta identity | Stable job UUID plus hashed job revision and execution contract | Compare the same procedure, not merely two outputs with the same display name |
| User task vs model proposal | Different typed origins, with a direct Work action that bypasses classification | User intent remains recoverable; model reformulation cannot acquire authority |

## 1. Build order and boundaries

1. Wire existing `scripts/chat_context.py` into plain `scripts/chat.py`.
2. Add a headless fixed-job watch runner, using existing deterministic verification.
3. Implement the gateway and the event/result contract specified here.
4. Implement the in-process Textual client and mandatory watch/activity panes.
5. Add operational telemetry, with measurement exclusion enforced first.

Later isolated editing, exact-diff approvals and Remote SSH remain goals. Do not
make them prerequisites for the first fixed watch job, and do not enable writes
just because the earlier branch includes approval/workspace interfaces.

No Electron, browser engine, web frontend, terminal emulator, AI-generated UI,
vector store, model-authored durable facts or automatic push in these workflows.
Windows Terminal is the target terminal host; Textual is the Python application
inside it. A shell, terminal host and inference server are different components.

## 2. In-process ownership, with an honest crash boundary

`SessionService` owns admission and durable state. `ConversationGateway` composes
chat context and proposes work. `TaskController` owns grants, routing, execution
and results. Textual owns presentation and user input only.

The Textual event loop never blocks on inference, subprocess waits or disk-heavy
inspection. The existing synchronous worker executes in a managed thread.
Observer records enter a bounded queue; one service writer validates, assigns a
sequence, commits and then publishes them. UI updates cross the thread boundary
through Textual messages/call_from_thread. Worker threads never mutate widgets.
This follows [Textual's worker/thread guidance](https://textual.textualize.io/guide/workers/).

Do not mistake cancelling a UI worker for killing inference or child processes.
Controller cancellation has its own token, execution epoch and process handles.
One in-process controller cannot forcibly and safely kill an arbitrary Python
thread. If a worker cannot be reconciled after cancellation, fence its epoch,
quarantine its workspace/endpoint, record NO_VERDICT and require process restart
before further work there. Do not announce a clean cancellation.

A fatal process crash loses the interactive controller as well as the UI. We
accept that tradeoff for the first client; durable records support recovery, not
uninterrupted execution. Restart marks active tasks interrupted and reconciles
children/leases before new admission. No hidden auto-resume of effects.

The scheduled runner is a **separate headless invocation**, launched by the OS
scheduler. It uses the same controller/verifiers and writes the same durable task
records. Textual reads committed watch summaries; it does not own scheduling or
require the terminal to remain open. This is not UI-to-controller IPC. Headless
and interactive invocations share repository/endpoint locks when applicable.

## 3. Finish direct-chat persistence first

Keep `chat_context.Session` schema v1 for ordinary user/assistant text, runtime
segments and persona hash. Do not turn it into a task runner or stuff tool calls,
verdicts, mutable permissions or system messages into its turns.

Plain-chat wiring must:

- Hold the existing conversation lock for the session and make new/resume/reset
  explicit. Reset creates a new conversation identity when task references exist.
- Make `history` turns only. Pass the current derived contract and optional persona
  separately to `compose()`. Never persist or replay an old system prompt.
- Call `ensure_runtime` when the selected endpoint configuration changes. Persist
  the persona hash as attribution, never as authority.
- Run `ensure_append_fits` before inference for the user turn and before committing
  the complete exchange. Save atomically; do not append a half-exchange on failure.
- Preserve raw user/model prose locally under the existing storage contract. No
  silent change to redaction/retention policy. The UI labels prose as prose, never
  durable repository facts. Export/redaction remains a distinct operation.
- Use whole-exchange prompt trimming without mutating stored turn ordinals. Handle
  disk-cap refusal visibly rather than silently deleting earlier conversation.

Tests before UI work: no doubled contract/persona; resume exact text; runtime
change; persona attribution; empty/failed response; disk full/cap; lock contention;
Unicode size; interrupted save; unsupported schema. No changes to the measured
agent prompt are needed.

## 4. Tasks are sibling artifacts, not assistant turns

`SessionRepository` wraps the existing chat store and a product task database.
There is one service owner for an interactive conversation. The context module
continues to own its file format; it does not import the gateway or controller.

Schema-v1 turns have no UUID. A stable reference is `(conversation_id, turn_index,
sha256(canonical stored Turn))`; stored ordinals are append-only. Context trimming
only changes a composed prompt, never these references. A sidecar association
binds an originating user turn to zero or more task IDs and displays result cards
after that turn. Headless tasks have `origin.kind=watch` and no invented user turn.

Task files contain the admitted request, execution contract, events, result,
evidence handles and hashes. Conversation files contain actual conversation.
The chat composer may retrieve selected bounded observations when answering a
follow-up, but resume does not insert terminal results as model-authored turns.
Historical cards say when/where they applied; current-tree proof is revalidated
at the action boundary, never resurrected from a stored green label.

Two stores cannot be committed atomically with a JSON rename and a SQLite commit.
Use an explicit admission handshake: reserve a pending sidecar association under
the conversation lock; save the user turn atomically; check its reference/digest;
mark task admitted plus its event in one DB transaction. Execute only after that
transaction. Recovery reconciles pending associations against actual saved turns;
unmatched records are abandoned, never executed. Store task completion entirely
in the task database; a later chat-save failure cannot erase completed work.

Migration rule: do not edit old conversation v1 files merely to add tasks. If
native turn UUIDs are needed later, introduce schema v2 with explicit migration
and preserved v1 files. Existing prototype EventBuffer schema_version=1 is an
in-memory implementation detail; it is NOT the `lca.session.events/1` contract.

## 5. Deterministic-first admission

Order: control commands -> explicit Work/Chat mode -> anchored deterministic
rules with resolved references -> model proposal fallback -> clarification.

| Input | Route | Disclosure |
|---|---|---|
| `What changed on my branch?` | work / git-review by named rule | Rule ID and selected skill |
| `Build it` with one active repo | work / build-and-test by named rule | Repo and configured verification scope |
| `Why did that fail?` | work only if exactly one eligible task reference | Referenced task and diagnostic skill |
| `Fix it` | explicit mutation intent, subject to existing write policy | Refuse while mutation workflow is unavailable |
| `work` / `chat` while a route proposal is pending | correct that proposal | One-word correction; revision increments |
| Work action plus arbitrary text | direct user task, no classification model | Original text preserved byte-for-byte |
| Ambiguous general text | fallback proposal, then visible selection or clarification | `source=model_proposal`, no invented confidence |

Use anchored rules, not “contains build” keyword matches. Quoted examples,
negations and “explain what build means” must not start a build. Strong direct
user task declarations need no intent call; deterministic skill routing may still
abstain. An unknown route is not permission for every tool.

Show a route chip before execution. Rule/direct requests within standing grants
may admit immediately. Model-fallback work proposals remain pending for accept or
one-word correction. Once an admitted task has begun, `chat`/`work` requests cancel
or fence that task before replacing it; it cannot undo a tool already running.
Always associate a correction with the displayed route ID. If more than one is
pending, select the route first; never guess from a global last-message pointer.

`TaskIntent` (untrusted proposal) carries original `TurnRef`, objective, proposed
reference IDs and origin. It cannot contain a root path, argv, allowlist, approval,
verification policy, experimental condition or success flag. `AdmittedTask` is a
different controller-created type with resolved repo ID, skill/tool scope, trusted
configuration hashes, budgets, execution epoch and immutable verification plan.

Personas only affect optional chat prose. They never enter intent rules, task
contracts, worker policy or the measured prompt. Model fallback sees bounded
context, not authority to decide what counts as verified.

## 6. Structurally separate verdicts from prose

Required return boundary:

```text
GatewayReply = ConversationReply | TaskAccepted
ConversationReply = { turn_ref, prose, classification: "conversation_only" }
TaskAccepted = { turn_ref?, task_id, route_id, event_cursor }
TaskCompletion = { task_id, status, verdict_block, worker_artifact_ref?, result_ref }
VerdictBlock = { verdict, reason_code, scope, evidence_ids, tree_digest?, rendered_lines }
```

Only the controller creates `TaskCompletion` and `VerdictBlock`. The deterministic
renderer creates `rendered_lines` from typed verifier output; clients render those
lines verbatim, plain text, in their own result widget. Canonical evidence IDs are
included even when empty (explicit `Evidence: none`). The block has no model prose
slot and the chat model is never asked to supply, include, rewrite or summarise it.

Requiring an LLM to include a fixed string is insufficient: it could still write
“sorted” above REFUSED. For the first product release, **no model-generated
completion narration** surrounds a task verdict. Worker analysis is an explicitly
labelled inspectable artifact, not the task result heading. Later explanatory
conversation stays in the conversation channel and cannot emit result events,
verification styling or status cards. There is no claim that a prompt can make
arbitrary free-form model prose incapable of false factual statements.

| Verdict | Exact meaning |
|---|---|
| VERIFIED | Required verification scope passed against the recorded state |
| FAILED | Verification produced a substantive failing result |
| REFUSED | Controller/policy/user/environment prevented the authorised operation |
| NO_VERDICT | No reliable final verification: timeout, interruption, missing evidence or unresolved cleanup |
| NOT_REQUIRED | Task completed with observations; no build/test claim was requested |

Lifecycle and verdict are separate. `timed_out + NO_VERDICT` is different from
`completed + FAILED`. A read-only successful result is NOT_REQUIRED, never a green
build badge. An earlier observed failure remains in evidence when a later hang
causes NO_VERDICT. Stop, error, disconnect and empty report cannot become VERIFIED.

## 7. One OVMS instance, two isolated contexts

Own endpoint admission outside the server. At most one request per endpoint lease
is dispatched, even if the server would queue extras. Chat and worker contexts
are separate message arrays; neither receives the other's conversation history,
persona or scratch reasoning. Shared weights do not imply shared KV contexts.

User-visible states: `Worker using Qwen3-8B`; `Chat queued for NPU, position 1`;
`Worker waiting while chat replies`; `Endpoint unavailable; result unknown`.
Elapsed wait is measured; no invented estimated completion time. Composer and
Cancel remain responsive while waiting. Chat queues a maximum of four pending
turns per session; reject the fifth visibly, never silently discard it.

Scheduling: active calls are not preempted; after each call, give one waiting
interactive chat request a slot, then the waiting worker a slot. Apply deadlines
and fair FIFO within each class so either side cannot starve. Control operations
do not consume model capacity. The initial fixed watch job uses no model at all.
Two role profiles naming the same physical endpoint share one lease. Across
processes use a shared OS-backed lease plus recorded ownership/epoch; process death
does not prove the server cancelled its request, so reconcile before reuse.

If inference times out, close the client operation, fence the task and quarantine
the lease until the request is known stopped or the owned runtime is safely
restarted. Do not start a new request just because a Python future was cancelled.
Do not stop unmanaged servers. A separate chat endpoint is a later explicit option,
not an automatic response to a busy NPU.

## 8. Cancellation, hangs and on-disk state

First Ctrl-C targets the displayed active task, records `task.cancel_requested`,
revokes its future tool-dispatch epoch and shows CANCELLING. On an idle prompt it
opens exit confirmation; exiting while work is active requests cancellation and
waits for reconciliation. A forced termination is INTERRUPTED, never clean cancel.

| Where cancellation lands | Required action | Evidence/result |
|---|---|---|
| Queued | Remove queued work atomically | cancelled / NO_VERDICT; no tool started |
| Waiting for inference | Cancel request if backend supports it; otherwise quarantine endpoint | late reply discarded by epoch; NO_VERDICT |
| During subprocess tool | Terminate owned process group/job; bounded grace then kill; reap descendants | partial stdout/stderr and exit/signal; NO_VERDICT |
| During file mutation (later) | Stop future writes; reconcile journal/preimage; never blind rollback | paths/partial action recorded; NO_VERDICT if uncertain |
| During finalisation | Single writer arbitrates commit of terminal result vs cancel | whichever terminal transition commits first wins |

Cancellation timeout defaults: 5 seconds graceful subprocess termination, then
5 seconds kill/reap deadline. Expiry fences the workspace and requires recovery.
Normal task deadline starts at admission, including queue time; endpoint wait,
HTTP call and tool deadlines are bounded by the remaining task time. Declare
configured budgets in the admitted request, not inferred from progress text.

Worker heartbeat is produced by the controller, not by model tokens. The UI can
show stalled/no progress after a configured interval while the actual deadline
decides timeout. Watchdog runs outside the worker thread. A blocked/GIL-stuck
process still cannot guarantee responsive Python UI; document this in qualification
and prefer bounded C/system calls. Recover terminal state from durable evidence.

On disk: retain partial logs, command identity, start state and observed child
identity; never fabricate an exit code. Do not restore user edits automatically.
One task gets exactly one terminal verdict/result. Events from a stale epoch may
be retained as diagnostics but cannot change terminal state or release a new
owner's lease. No retry of unknown effects on restart.

Instrumentation hooks should wrap the product registry/runner and guard handler
dispatch; do not modify `agent/context.py` or prompt-hashed contract methods to
obtain cancellation. If a future implementation cannot preserve base-prompt
identity, stop and request a generation decision before making that change.

## 9. Watch jobs and comparable deltas

A watch is a fixed configured job, not an autonomous prompt on a timer. Initial
job: check a trusted repository's configured build/tests using existing controller
verification. No intent model, no worker model needed, no automatic edits, commits,
fetch or push. Missing tools/configuration produce a typed refusal/no-verdict.

`job_id` is a persistent UUID. Rename changes its display label only.
`job_revision` is SHA256 of canonical JSON containing: repository identity/host,
local ref or fixed-root selection policy, dirty-tree policy, command/profile and
environment allowlist, skill/verification contract identities if used, scope,
timeouts, endpoint policy if any, and delta schema version. Canonical JSON is UTF-8,
sorted keys, no insignificant whitespace, finite numbers; secrets are references
plus secret-version identity, never plaintext. Reject unknown fields.

Schedule identity is separate: UTC interval/anchor, misfire policy and enabled flag
have their own revision. Moving from hourly to daily does not pretend the checking
procedure changed. Each attempt has run UUID, due time, actual times, trigger
identity, resolved HEAD/content digest and effective execution-contract digest.

Compare only runs with equal job ID, job revision, execution-contract digest and
result schema. Changing the controller/verifier/dependencies can make two runs
incomparable even if the JSON job file did not change. Watched source HEAD is an
observation, excluded from job revision, so source changes can be reported.

Delta has two references: last attempt (including refused/interrupted) for the
watch pane, and last comparable complete run for substantive regression/recovery.
The pane never conceals a failed recent attempt behind the last passing run.
First run = BASELINE; changed contract = INCOMPARABLE; incomplete previous attempt
is labelled, not interpreted as a failure recovery. Store both compared run IDs.

Structured delta fields: added/removed/changed file paths from source snapshots,
added/removed test IDs, newly failing/recovered/still failing tests, build status
transition and command duration difference where timing scope matches. Stable
test identity includes suite/profile + runner node ID, not a summary line. Missing
test discovery means unknown, not all removed or recovered. A path change alongside
a failure is correlation; the delta does not assert causation or source blame.

Concurrency defaults: one active run per job and one execution lease per target
worktree. Overlap is skipped with an explicit occurrence record, not a second run.
OS wake/misfire coalesces missed intervals into at most one catch-up; no replay
storm. `(job_id, schedule_revision, scheduled_utc)` is an idempotency key, protected
by a unique DB constraint. Manual runs get a separate user-request UUID. Time zones
are display-only initially; use UTC intervals to avoid cron/DST ambiguity.

OS scheduler owns launch, runner owns a single attempt. Do not create scheduled
tasks merely by opening the hub. Job enable/disable and scheduler registration
are explicit user actions. The watch pane reads latest committed records, showing
job label, last attempt/due time, elapsed age, verdict, next due and delta summary.

## 10. Mandatory layout and named palette

Wide layout (target at least 120 columns): left 55% conversation plus composer;
right 45% stacked activity/evidence and watch (minimum 6 watch rows); bottom status
and optional telemetry. Activity always shows route, skill, dispatched tool and
controller verdict. Watch always shows a compact latest-attempt row, even with no
jobs (`No watches configured`). No file tree, git pane or scrolling raw-log tail.

At 80-119 columns stack conversation above activity, retaining visible active-task
and watch summary rows. Below 80 columns show a compact layout warning plus those
mandatory summaries and composer; do not hide activity behind a user toggle.
Full evidence can open in a separate detail screen. Telemetry alone is hideable.
Use labels/icons and explicit statuses so colour is additive, never the only cue.

Single palette source proposed: `internal/ui/themes/lca.json`, with `neon` and
`high_contrast` variants. Named roles: background, surface, foreground, accent,
secondary, warn, fail, verified, muted, focus. Initial neon values reuse current
terminal_ui: accent #FF36E2, secondary #00FFF0, warn #FFB840, fail #FF526E,
verified #50FF96, muted #8791A5, foreground #EBF0F6; dark backgrounds are selected
and checked for contrast in the UI PR. High-contrast keeps statuses identifiable
in monochrome and uses brighter text/no dimming. No literal RGB at call sites.

`terminal_ui.py` and Textual load roles from that source. An export command later
generates Windows Terminal schemes JSON from it, never hand-maintained duplicate
palettes. Export is a file for review/import, not automatic user-settings mutation.
Microsoft documents [JSON colour schemes](https://learn.microsoft.com/en-us/windows/terminal/customize-settings/color-schemes).

Do not identify the user's host from unseen screenshots. Check WT_SESSION locally,
TTY/encoding/TERM/COLORTERM and an optional visual colour/glyph check. WT_SESSION
may be absent through SSH and is not itself proof of end-to-end rendering. Console
host is not categorically incapable of RGB: Microsoft documents extended colour
[VT sequences](https://learn.microsoft.com/en-us/windows/console/console-virtual-terminal-sequences).
Terminal version/configuration matters; retain NO_COLOR and readable fallback.
Cascadia Code is the target font, subject to a real glyph-width check on the laptop.

## 11. Telemetry exclusion before telemetry implementation

`telemetry_visible` and `sampling_enabled` are different fields. Hiding the strip
must not leave collectors consuming resources. Collector construction requires
an operational-mode permit. A measured/scored run defaults to no UI process, no
sampler, no scheduled background work on the involved host/endpoint.

Enforce an exclusive measured-run lease across LCA processes: stop collectors,
wait for acknowledgement, pause watch dispatch, quiesce UI redraw/animations or
close the hub, then capture the manifest before timing starts. If quiet state
cannot be confirmed, refuse a clean measured-run admission. No best-effort “off”.
Passive controller facts used by the experiment's existing instrumentation are
not the new device poller; do not redefine the instrument in this design stream.

Future-generation manifest fields: requested/effective product sampling boolean,
visibility, sampler IDs/version/poll interval, UI active/quiet status, watch
dispatch state, exclusion-lease identity and any external unverified interference.
Any approved nonstandard telemetry-on run must explicitly record it and cannot be
mixed into an off-policy cohort. Missing legacy fields mean UNKNOWN, not off.

**Do not add these fields to frozen manifests now.** No evaluation module imports
gateway, context, persona, UI or samplers. A future measurement-generation PR owns
the launcher/manifest handshake; until then the new sampler cannot be used alongside
measured/scored workflows. Structural import tests plus host-level exclusion tests
are required before telemetry ships. Merely hiding a widget does not pass this gate.

## 12. Differences from the existing prototype

| Prototype at d4a4cfa | Required target |
|---|---|
| Chat model classifies almost every turn | Rule/direct first, fallback proposals visible and correctable |
| In-memory EventBuffer with arbitrary payload dictionaries | Versioned exhaustive event schema, durable critical events |
| Worker prose followed by controller suffix | Separate deterministic verdict/evidence widget; no completion narration |
| In-memory conversation history | Existing chat_context wired first; sibling task store |
| No watch workflow | Fixed headless job and structured comparable delta |
| Synchronous turns and Ctrl-C exits | Responsive UI, real cancellation and NO_VERDICT |
| Browser/desktop transport roadmap | In-process Textual, no web frontend in this scope |
| Future telemetry without exclusion handshake | Collectors blocked until measurement quiet-state policy exists |

The brief's quoted +0.433/+0.060 figures were not revalidated against frozen
artifacts in this design pass and are not adopted as new findings. Deterministic
admission is justified by enforceable policy boundaries independently of those
numbers. Existing experimental claims and data remain unchanged.
