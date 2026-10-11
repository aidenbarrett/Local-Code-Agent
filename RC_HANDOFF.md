# Development candidate handoff

Candidate: 9950b276fd806e90db2d3d022a1621226ccf35bd (tree 2689c69f58fe25dc4ca8b55741128a19a9cbbb55).
This is a development candidate. Physical disconnected Panther Lake acceptance,
configuration-specific model competence and enforced merge policy remain open.
#403 remains the release decision; this handoff does not close it.

## Install and open a real repository

```powershell
git clone https://github.com/aidenbarrett/Local-Code-Agent.git
cd Local-Code-Agent
git checkout --detach 9950b276fd806e90db2d3d022a1621226ccf35bd
.\install.ps1 -CheckOnly
.\local-code-agent.ps1 models
```

Choose the preset for the machine explicitly. The PTL GPU 30B weights are about
17 GB. A selected preset declares a device; it does not prove physical execution.

```powershell
.\local-code-agent.ps1 models pull ptl-gpu-30b
.\local-code-agent.ps1 models use ptl-gpu-30b
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

See [QUICKSTART.md](QUICKSTART.md) for setup details and
[CURRENT_STATE.md](CURRENT_STATE.md) for the authoritative capability ledger.

## Evidence and limits

| Evidence | What it supports | What remains unknown |
|---|---|---|
| Reviewed exact-tree Linux/Windows CI for #499, #500, #501 and #502 | Deterministic controller, preservation, public surfaces, compact recovery and first-use repair regressions | Physical offline execution and general model competence |
| Public merged-main J24 continuous run after #499 | One composed scripted engineering workflow | Real-model performance |
| Windows cxxopts build/full CTest run | Real repository build/test and unrelated-work preservation | General repository support |
| Same-source CPU 8B/30B reference measurements | Retained configuration-specific answers, repair outcomes, client timing and sampled RSS | Answer correctness, isolated model-size effect, PTL device behaviour |
| Fresh exported setup with prepared dependencies | New Python environment and runtime/cache startup exercised | Successful readiness: 30B CPU 7k context probe timed out |

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

No verified real-model repair was produced. Human edits to model candidates and
manual Stops during these trials were zero by operator observation. Budget Stops
were automatic. The last row is one deliberate retry after the preceding failed
trial; startup retries and preparation are separately recorded. RSS uses ten-second
samples of the owned process and excludes pre-sampler startup; it is not a peak guarantee.
The last two trials started/replaced the server (34.2s/34.3s); intended warm reuse
in the final trial was not observed because the owner detected environment drift.
The full report fields, source identity, artifact revisions and operator receipts
are retained in [the measurement record](internal/docs/rc-evidence-2026-10-11.json).
Authoritative CI: [#501](https://github.com/aidenbarrett/Local-Code-Agent/actions/runs/38149211547)
and [#502](https://github.com/aidenbarrett/Local-Code-Agent/actions/runs/38151450475).
Their merged trees match the reviewed/tested trees. Explanation correctness remains unknown.
The 8B repeat overlapped a model download and the final interval of setup/server
startup; these timings do not isolate model-size effects. No aggregate success or
intervention rate is inferred from these small, differently prepared runs.

## Remaining release actions

- Aiden: run the supported physical PTL profile and the disconnected public flow:
  `.\local-code-agent.ps1 qualify --profile ptl-gpu-30b --output C:\lca-evidence\ptl-1`.
  Preparation and reports are automated; the actual target machine and disconnect
  confirmation are required. CPU reference runs cannot substitute for #293/#418.
- Repository owner: configure reviewed-green main protection and re-observe it
  with the existing checker. The last successful 11 October read found main unprotected, no active
  required-status-check ruleset and no required checks; the latest recheck is unknown
  because GitHub returned HTTP 403 (rate limit). Administration was not changed.
- Retain model/hardware-specific failures and observe the next task after Stop.
  With OVMS an in-flight generation may finish after the client is stopped;
  endpoint-side cancellation remains unproved for these profiles.

Go/no-go for #403: development handoff only; no offline-qualified or production-ready
release claim until the remaining evidence and repository policy are established.
