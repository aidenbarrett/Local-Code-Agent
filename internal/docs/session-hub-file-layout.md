# File layout and PR boundaries

Design only; proposed files below are not claims that implementations exist.
The contract artifacts live under docs so this design-only follow-up changes no
source/prompt/outcome hash. Future controller changes belong in the hashed source
surface deliberately, not in untracked scripts to dodge provenance.

## Implementation destinations

| Path | Responsibility | Identity impact |
|---|---|---|
| `internal/scripts/chat.py` (existing) | Wire existing context lifecycle; turns-only history | Outside current globs; do not touch measured prompt |
| `internal/scripts/chat_context.py` (existing) | Remain schema-v1 direct-chat store/composer | Outside current globs; future contract changes explicit |
| `internal/local_agent/session/service.py` | Single state/admission owner and event publication | Source hash |
| `internal/local_agent/session/intents.py` | Anchored route rules, direct Work path, corrections | Source hash |
| `internal/local_agent/session/contracts.py` (replace prototype carefully) | Intent/admission/result types; strict schema validation | Source hash |
| `internal/local_agent/session/events.py` (replace prototype carefully) | Exhaustive typed envelope and observer projection | Source hash |
| `internal/local_agent/session/results.py` | Deterministic verdict/evidence renderer | Source hash |
| `internal/local_agent/session/storage.py` (implement skeleton) | SQLite state/event outbox, sidecar TurnRefs and artifact caps | Source hash |
| `internal/local_agent/session/scheduler.py` (implement skeleton) | Endpoint leases, bounded admission and role fairness | Source hash |
| `internal/local_agent/session/cancellation.py` | Execution epoch, cancel token, terminal arbitration | Source hash |
| `internal/local_agent/session/controller.py` (adapt prototype) | Existing orchestrator and verifier adapter | Source hash |
| `internal/local_agent/watch/jobs.py` | Validated fixed job and canonical revision hashing | Source hash |
| `internal/local_agent/watch/runner.py` | One fixed attempt, no conversation model or intent routing | Source hash |
| `internal/local_agent/watch/delta.py` | Stable test/file comparisons and explicit incomparable state | Source hash |
| `internal/local_agent/watch/store.py` | Job/attempt identity, occurrence idempotency, summary projection | Source hash |
| `internal/local_agent/watch/cli.py` | Explicit once/enable/disable/status control | Source hash |
| `internal/scripts/watch.py` | Thin root-command adapter, no policy | Outside current globs |
| `internal/ui/textual_app.py` | Textual app and direct service subscription | Presentation only; outside globs |
| `internal/ui/messages.py` | Thread-safe UI notifications; no controller state invention | Presentation only |
| `internal/ui/widgets/{conversation,activity,verdict,watch,status}.py` | Mandatory panes and deterministic result view | Presentation only |
| `internal/ui/styles/hub.tcss` | Responsive layout, focus and accessibility | Presentation only |
| `internal/ui/themes/lca.json` | One role palette with neon/high-contrast variants | Presentation only |
| `internal/ui/export_terminal_theme.py` | Generate reviewable Windows Terminal scheme JSON | Presentation only |
| `internal/terminal_ui.py` (existing) | Consume same named palette for plain terminal mode | Presentation only |
| `internal/local_agent/session/telemetry.py` (implement last) | Collector permit and attributed host samples | Source hash |
| `internal/docs/session-contract/v1/` (exists as design schema) | Normative review contract/fixtures before runtime packaging | No hash change now |

Implementations must package/version the runtime schema or generate validators
from it with a checked digest and drift tests. Do not read a mutable docs file at
runtime as an untracked authority. Any packaged schema bytes must enter source
identity explicitly: current provenance globs cover Python but not arbitrary JSON
under the package. Either generated Python validator/data with a schema digest,
or deliberate provenance inclusion for schema resources. Test the built artifact.

Evaluation imports none of `session`, `watch`, UI, persona or chat_context.
Presentation modules cannot import execution internals except the public typed
service facade. No changes inside `agent/context.py`; base_prompt_sha256 must remain
identical. If adapting runner/registry hooks changes a hashed prompt method, stop
for a generation decision instead of silently updating that identity.

## Reviewable PRs against the product branch

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
Reviewers can open child PRs targeting `feature/conversation-gateway`. Do not merge
the older prototype simply because this design follow-up is docs-only.

## Tests to add in implementation PRs

- `internal/tests/unit/test_chat_persistence_wiring.py`
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
