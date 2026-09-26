# Public journeys: first useful Local Code Agent release

This is the short acceptance contract for the public `local-code-agent.ps1` Session Hub. `CURRENT_STATE.md` records what is implemented now; `internal/docs/product-roadmap.md` owns longer-term scope.

The first release should let a new user inspect a repository, find code, understand a branch, run configured builds/tests, explain failures and see truthful local runtime/model/device facts. Stop must also be truthful. Source edits, conflict resolution, commits and recurring watches remain later journeys.

## Shared rules

- Users ask naturally. Supported work does not require route/skill vocabulary.
- The deterministic controller owns repository, tools, execution policy and verification before effects.
- Configured and observed runtime/device facts remain distinct. Never infer physical execution from a profile name.
- Results state exactly what was attempted, what passed/failed, what was not run and which repository state the proof covers.
- Build success does not imply tests passed. Stale evidence, zero tests, outages and timeouts cannot become success.
- Conversation and durable task/result identity survive restart without replaying effects.
- Worker prose is inspectable output, not authority over verdict or task state.
- Each delivered journey needs behavioural coverage through the public composition and a meaningful fail-closed case.

## User contract

| ID | User request | Required behaviour |
|---|---|---|
| J1 | Help / What can you do? | Deterministic capabilities available here, including unavailable actions and why. No model required. |
| J2 | What are you running on? | Active repo, configured model/runtime/endpoint and observed device only where evidence exists. Unknown stays unknown. |
| J3 | Inspect this repo / Where is X? | Read-only work starts directly and returns real paths/references plus scope limitations. |
| J4 | What changed on my branch? | Establish base explicitly or by documented policy, then distinguish committed/staged/unstaged changes. |
| J5 | Build it / Run the tests | Use only configured commands when execution is enabled and report build/test proof separately. |
| J6 | Why did that fail? | Select an eligible retained failed task and explain its actual evidence. Ambiguity gets explicit task choices. |
| J7 | Stop | Fence new dispatch immediately; claim stopped only when owned work is reconciled. Unknown cleanup remains unknown. |
| J8 | Close/reopen / endpoint unavailable | Preserve conversation/task truth without replay. Transport/runtime failure is named as such. |

## Delivery order

1. Make J1/J2/J8 completely truthful and useful with the endpoint unavailable.
2. Make J3/J4 natural read-only work without rituals.
3. Make J5/J6 prove the exact requested task and retain evidence cleanly.
4. Finish J7 end-to-end cancellation and owned cleanup.
5. Rehearse the complete public path on the actual offline target machine and retain exact runtime/device identity and failures.

Only then broaden mutation authority.

## Review discipline

A PR should name the user journey or correctness/trust defect it fixes, the owning authority it composes, the negative case that fails closed and the behavioural evidence used to verify it.

Do not keep unreachable implementations merely because they import. Test from composition roots, delete obsolete paths and avoid duplicate authorities.

A demo is a sanity check. Delivery means the behaviour and evidence above survive both the happy path and the failure path.
