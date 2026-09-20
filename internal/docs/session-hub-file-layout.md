# File layout and PR boundaries

Design only; proposed files below are not claims that implementations exist.
Where an implementation already exists, this table uses its current explicit path rather
than preserving an obsolete prototype name. Future controller changes belong in the
hashed source surface deliberately, not in untracked scripts to dodge provenance.

## Implementation destinations

| Path | Responsibility | Identity impact |
|---|---|---|
| `internal/scripts/chat.py` (existing) | Wire existing context lifecycle; turns-only history | Outside current globs; do not touch measured prompt |
| `internal/scripts/chat_context.py` (existing) | Remain schema-v1 direct-chat store/composer | Outside current globs; future contract changes explicit |
| `internal/scripts/session-hub.py` (existing) | Thin public Session Hub composition root: own the canonical conversation, construct/recover the durable service, emit `session.opened`; no controller routing/admission policy | Outside current globs; product composition only |
| `internal/local_agent/session/conversation_gateway.py` (existing) | Conversation turn routing and controller hand-off | Source hash |
| `internal/local_agent/session/session_event_service.py` (existing) | Single durable admission/event writer and publication boundary | Source hash |
| `internal/local_agent/session/intents.py` | Anchored route rules, direct Work path, corrections | Source hash |
| `internal/local_agent/session/contracts.py` (existing; split carefully) | Intent/admission/result types and product projections | Source hash |
| `internal/local_agent/session/event_contract.py` (existing) | Build and validate normative durable `lca.session.events/1` envelopes | Source hash |
| `internal/local_agent/session/event_buffer.py` (existing) | Bounded process-local activity buffer used by the synchronous prototype path | Source hash |
| `internal/local_agent/session/results.py` | Deterministic verdict/evidence renderer | Source hash |
| `internal/local_agent/session/session_store.py` (existing) | SQLite durable task/event state and replay | Source hash |
| `internal/docs/endpoint-scheduler-design.md` | Endpoint lease/fairness design; no source implementation exists yet | No source hash change |
| `internal/local_agent/session/cancellation.py` | Execution epoch, cancel token, terminal arbitration | Source hash |
| `internal/local_agent/session/task_controller.py` (existing) | Existing orchestrator and verifier adapter | Source hash |
| `internal/local_agent/session/self_check.py` (existing) | Deterministic repository self-check path | Source hash |
| `internal/local_agent/watch/jobs.py` | Validated fixed job and canonical revision hashing | Source hash |
| `internal/local_agent/watch/runner.py` | One fixed attempt, no conversation model or intent routing | Source hash |
| `internal/local_agent/watch/delta.py` | Stable test/file comparisons and explicit incomparable state | Source hash |
| `internal/local_agent/watch/store.py` | Job/attempt identity, occurrence idempotency, summary projection | Source hash |
| `internal/local_agent/watch/cli.py` | Explicit once/enable/disable/status control | Source hash |
| `internal/scripts/watch.py` | Thin root-command adapter, no policy | Outside current globs |
| `internal/ui/textual_app.py` | Future Textual app and direct service subscription; create this source area only when implementation begins | Presentation only; classify explicitly when added |
| `internal/ui/messages.py` | Future thread-safe UI notifications; no controller state invention | Presentation only; classify explicitly when added |
| `internal/ui/widgets/{conversation,activity,verdict,watch,status}.py` | Future mandatory panes and deterministic result view | Presentation only; classify explicitly when added |
| `internal/ui/styles/hub.tcss` | Future responsive layout, focus and accessibility | Presentation only; classify explicitly when added |
| `internal/ui/themes/lca.json` | Future role palette with neon/high-contrast variants | Presentation only; classify explicitly when added |
| `internal/ui/export_terminal_theme.py` | Future generated Windows Terminal scheme JSON | Presentation only; classify explicitly when added |
| `internal/terminal_ui.py` (existing) | Consume same named palette for plain terminal mode | Presentation only |
| `internal/local_agent/session/telemetry.py` (design-only source placeholder; disposition separately) | Collector permit and attributed host samples | Source hash |
| `internal/docs/session-contract/v1/` (existing) | Normative runtime Session Hub v1 schema | Source hash |

The root `local-code-agent.ps1 session` command is the user-facing route into
`internal/scripts/session-hub.py`. That script may compose existing authorities and own
lifecycle setup, but it is not a place to hide controller policy. Durable task admission,
routing, result semantics, permissions or verifier changes belong in the hashed Session
Hub source listed above.

The runtime schema is already loaded and validated from the versioned contract under
`internal/docs/session-contract/v1/`, and those JSON bytes are deliberately included in
`source_sha256`. Any future packaging change must preserve that provenance and test the
built artifact rather than creating an untracked copy of the contract.

Evaluation imports none of `session`, `watch`, UI, persona or chat_context.
Presentation modules cannot import execution internals except the public typed
service facade. No changes inside `agent/context.py`; base_prompt_sha256 must remain
identical. If adapting runner/registry hooks changes a hashed prompt method, stop
for a generation decision instead of silently updating that identity.

## Reviewable PRs against the product branch

The historical A-G slice labels below describe the original implementation design, not
the current refactor PR lettering. `CURRENT_STATE.md` is authoritative for what is live.

| PR | Scope | Required gate | Depends on |
|---|---|---|---|
| A | Plain chat_context wiring only | Resume/locking/disk-cap/contract-persona composition tests | Existing code |
| B | Shared contracts/store and fixed watch once-run/delta | No model calls; same-job identity; real verifier result; refusal and incomplete runs | This design accepted |
| C | OS schedule registration and watch lifecycle | Explicit enable; overlap/misfire/idempotency; terminal closed still runs | B |
| D | Deterministic-first gateway and worker observer adapter | Direct path, correction, strict result rendering, task sibling recovery | A, B |
| E | Cancellation and endpoint arbitration | Real child cleanup; hangs/late result; same endpoint two contexts | D |
| F | In-process Textual panes/theme | Mandatory activity/watch; no badge from prose; resize/NO_COLOR/SSH | C, D, E |
| G | Telemetry permit/samplers | Quiet-state admission + future-generation manifest plan approved first | F; separate measurement governance |

Work on A and B can proceed independently after contract review. C and D can
follow independently once B stabilises. The Textual implementation waits for plain
storage and cancellation gates; no one should debug those through a new UI.

## Tests to add in implementation PRs

- `internal/tests/unit/test_chat_persistence_wiring.py`
- `internal/tests/unit/test_session_hub_product_path.py` (existing; public composition/recovery boundary)
- `internal/tests/unit/test_session_event_contract.py`
- `internal/tests/unit/test_session_intents.py`
- `internal/tests/unit/test_session_verdict_rendering.py`
- `internal/tests/unit/test_watch_identity_and_delta.py`
- `internal/tests/integration/test_session_recovery.py`
- `internal/tests/integration/test_session_cancel_process_tree.py`
- `internal/tests/integration/test_shared_endpoint_lease.py`
- `internal/tests/integration/test_watch_scheduler_misfire.py`
- `internal/tests/integration/test_product_import_isolation.py`
- `internal/tests/ui/test_textual_hub.py` (new optional Textual test dependency/job;
  decide how it fits the offline compatibility subset before introducing it)

Keep tests meaningful: assert forbidden effects did not occur and actual verifier
evidence drives state. Snapshotting colourful text alone does not test this system.
