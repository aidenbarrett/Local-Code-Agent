# Development candidate handoff

Candidate: 4c4d6d33b7c262d7a60367759652c60569086dd9 (tree dc0b17a0d03b471b3158749b8d42469286e15ba6).
This is a development candidate. Physical disconnected Panther Lake acceptance,
configuration-specific model competence and enforced merge policy remain open.
#403 remains the release decision; this handoff does not close it.

## Install and open a real repository

```powershell
git clone https://github.com/aidenbarrett/Local-Code-Agent.git
cd Local-Code-Agent
git checkout --detach 4c4d6d33b7c262d7a60367759652c60569086dd9
.\install.ps1 -CheckOnly
.\install.ps1 -InstallMissing -SkipModelDownload
.\local-code-agent.ps1 models
```

The bootstrap installs prerequisites, the managed Python environment and OVMS
before model commands can run. If the selected weights are absent, its expected
exit is 3 with the model folder named; readiness is still pending. Other refusals
need their named prerequisite resolved. Choose the preset for the machine explicitly. The PTL GPU 30B weights are about
17 GB. A selected preset declares a device; it does not prove physical execution.

```powershell
.\local-code-agent.ps1 models use ptl-gpu-30b
.\local-code-agent.ps1 models pull ptl-gpu-30b
.\install.ps1 -InstallMissing
.\local-code-agent.ps1 doctor
$lca = (Resolve-Path .\local-code-agent.ps1).Path
cd C:\src\my-project
& $lca init
```

Review the proposed `.local-agent.toml`, including the real configure/build/test
commands and ignored artifact directories. Then `init --write` creates it without
replacing an existing declaration. Setup or doctor refusal means readiness has
not been established; resolve the named prerequisite before proceeding.

```powershell
& $lca init --write
& $lca doctor --repo .
& $lca acceptance --repo . --allow-build --only R02-build --only R03-tests --output C:\lca-evidence\repo-1
& $lca session --repo . --allow-execution
```

Ask: `Inspect this repository`, `Where is <symbol> used?`, `What changed on my
branch?`, `Build it`, `Run the tests`, `Why did that fail?`, then `Fix it`.
Model prose is unverified detail; controller proof decides completion. Only a controller-verified candidate enables
the mutation sequence: `/diff <task-id>`, `/apply <task-id>`, then either
`/undo <task-id>` or `/commit <task-id>`. Commit does not push. Stale proof and
conflicting later edits refuse. Close and reopen to read retained results.

See [QUICKSTART.md](../../QUICKSTART.md) for setup details and
[CURRENT_STATE.md](../project/CURRENT_STATE.md) for the authoritative capability ledger.

## Evidence and limits

The candidate above is tree-identical to independently reviewed PR #571 head
`1522c53ce9b88c84dc8b7f5b5b26936ad4402d3a`. Full Linux pytest and all five
Windows shards passed in
[tests run 38475379773](https://github.com/aidenbarrett/Local-Code-Agent/actions/runs/38475379773);
native endpoint, serving, product acceptance, import-boundary and integration-eligibility
workflows also passed on that exact head. The native-endpoint Windows archive download
had one DNS failure and passed when rerun without changing the head. Merged main has tree `dc0b17a0d03b471b3158749b8d42469286e15ba6` as that tested head.
This is deterministic CI/controller evidence, not exact-candidate model or physical
qualification.

| Later retained evidence | What it supports | Limit |
|---|---|---|
| CPU8B cxxopts compile repair, #514 | One model-generated identifier repair, full build and independent seven-test suite | The original candidate also changed line endings; retained without rescore. Byte preservation was repaired in #517. No rate or PTL proof |
| CPU30B cxxopts compile repair, retained in #516/#517 | One model-generated identifier repair, independent seven-test suite and subsequent controller diff/apply/exact commit preserving unrelated work | Model repair source `44cec210bbb32107789427559ec2c545e62a935b86387bff2be9dadc2907dca7`; controller follow-through is a separate proof class |
| Fresh Windows archive CPU30B setup, #522 | New managed Python environment, public setup READY TO USE, protocol/tool/context checks and fixture 4/4 tests | Product archive `142a6e041e73e902503598d308915816d3eb7b64`; existing prerequisites/weights/tools/cache reused. R01 second launch was MEASURED:no-evidence; semantic correctness unknown |
| Production count-defect CPU30B attempts, #526 and #528 | Unchanged upstream tests passed 7/7 before the fault and failed 2/7 afterward; one 300s and one separate 900s repair attempt remained UNKNOWN | Different measured product sources; no isolated budget causation, success rate or model-ceiling claim. The 900s trace has two proposals, no application; discarded proposal contents are unavailable |
| Context-assisted CPU30B production repair, #563 | One retained model proposal applied through the product's matching primitive; all seven unchanged upstream tests passed | Operator supplied the relevant source and failure log and narrowed tools; one result is not autonomous repair or a success rate. CPU was declared, execution device unconfirmed |
| Composed PTL qualification summary, #571 | One root command now projects retained J24/J07 execution, lifecycle and user-work-preservation evidence into a compact summary | The projection does not prove a Panther Lake run, observed device, or offline success; those fields remain UNKNOWN until the physical qualification produces matching evidence |

Original records are in [the compile-repair archive](../../internal/docs/rc-model-competence-2026-10-11.json),
[the first-use archive](../../internal/docs/rc-first-use-cpu30b-2026-10-12.json),
[the 300s production-test archive](../../internal/docs/rc-model-production-test-2026-10-12.json),
[the separate 900s evidence PR](https://github.com/aidenbarrett/Local-Code-Agent/pull/528),
[the context-assisted repair PR](https://github.com/aidenbarrett/Local-Code-Agent/pull/563),
and [the composed PTL summary PR](https://github.com/aidenbarrett/Local-Code-Agent/pull/571).
The CPU30B original candidate and independent/controller receipts remain retained in
[#516](https://github.com/aidenbarrett/Local-Code-Agent/pull/516) and
[#517](https://github.com/aidenbarrett/Local-Code-Agent/pull/517).
Raw timed-out repair call/context totals are incomplete; #526 repairs future reporting
without rescoring the original reports. None of this replaces #293's physical GPU30B
J10 `--repeat 3` comparison, disconnected PTL qualification or observed execution-device proof.

### Earlier pinned measurements

The following records describe the earlier candidate
`9950b276fd806e90db2d3d022a1621226ccf35bd` and their own exact sources, rather than
the current candidate above. Their original outcomes and limitations are preserved.

| Evidence | What it supports | What remains unknown |
|---|---|---|
| Reviewed exact-tree Linux/Windows CI for #499, #500, #501 and #502 | Deterministic controller, preservation, public surfaces, compact recovery and first-use repair regressions | Physical offline execution and general model competence |
| Public pinned-main J24 continuous run after #502 | One composed scripted engineering workflow | Real-model performance |
| Public pinned-main cxxopts Q01–Q05, all five PASS | Real build/test, seeded failure truth, scripted repair/apply/undo/commit and Stop with unrelated-work preservation | General repository support |
| Same-source CPU 8B/30B reference measurements | Retained configuration-specific answers, repair outcomes, client timing and sampled RSS | Answer correctness, isolated model-size effect, PTL device behaviour |
| Fresh exported bootstrap and repeated public launch | New Python environment, staged OVMS, C++ validation 4/4; bootstrap exit3 for missing weights; models/use exit0; repeated doctor exit3 naming model download | Full readiness pending; separate prepared-weight CPU30B setup timed out at 7k |

Measured CPU reference source: 3b3fe9cb87f78cd041a5c6dc1c62a39e84f602c5,
source_sha256 d7da2817b0e0d6b0f5196ea725f1c807cfb0376ff3c31ff2137fe4f325a4a699.
Corpus: cxxopts 639cdbad93d45e74fd781e4cf6b0ac798eeec2aa, MIT.
| Trial | Explanation | Seeded model repair | Sampled maximum OVMS RSS |
|---|---|---|---|
| 8B original source | UNKNOWN, 352.1s | UNKNOWN, 311.1s | 10.11 GB |
| 8B scoped-search reference | Commands incomplete, 334.1s | UNKNOWN, 354.6s | 10.08 GB |
| 30B same-source reference | Commands incomplete, 179.2s | UNKNOWN, 327.7s | 34.82 GB |
| 30B completion-fix source | Not selected | UNKNOWN, 308.0s; final build cancelled | 34.58 GB |
| 30B merged-main repeat | Not selected | Inference timeout, 130.6s | 34.26 GB |

No verified real-model repair was produced in those five earlier trials. Human edits to model candidates and
manual Stops during these trials were zero by operator observation. Budget Stops
were automatic. The last row is one deliberate retry after the preceding failed
trial; startup retries and preparation are separately recorded. RSS uses ten-second
samples of the owned process and excludes pre-sampler startup; it is not a peak guarantee.
The last two trials started/replaced the server (34.2s/34.3s); intended warm reuse
in the final trial was not observed because the owner detected environment drift.
The full report fields, source identity, artifact revisions and operator receipts
are retained in [the measurement record](../../internal/docs/rc-evidence-2026-10-11.json).
Authoritative CI: [#501](https://github.com/aidenbarrett/Local-Code-Agent/actions/runs/38149211547)
and [#502](https://github.com/aidenbarrett/Local-Code-Agent/actions/runs/38151450475).
Their merged trees match the reviewed/tested trees. On pinned main, J24 passed
in 23.3s (report SHA-256 2e3e05646bb0360a0a29e4a9ddeaf9f3988ca6219721ec50668d42280f2b0f3f),
and cxxopts Q01–Q05 all passed (report SHA-256
8454a4599c3edd96c58bd96cb2f6c41962c8e9ffc42b19275029ca68788f3aad). Explanation correctness remains unknown.
The 8B repeat overlapped a model download and the final interval of setup/server
startup; these timings do not isolate model-size effects. No aggregate success or
intervention rate is inferred from these small, differently prepared runs.

## Remaining release actions

- Aiden: run the supported physical PTL profile and the disconnected public flow:
  `.\local-code-agent.ps1 qualify --profile ptl-gpu-30b --output C:\lca-evidence\ptl-1`.
  Preparation and reports are automated; the actual target machine and disconnect
  confirmation are required. CPU reference runs cannot substitute for #293/#418.
- Repository owner: configure reviewed-green main protection and re-observe it
  with the existing checker. The 12 October observation found main unprotected,
  no active required-status-check ruleset and no required checks. Administration
  was not changed; manually reviewed green merges do not establish enforcement.
- Retain model/hardware-specific failures and observe the next task after Stop.
  With OVMS an in-flight generation may finish after the client is stopped;
  endpoint-side cancellation remains unproved for these profiles.

Go/no-go for #403: development handoff only; no offline-qualified or production-ready
release claim until the remaining evidence and repository policy are established.
