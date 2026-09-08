# 30B three-condition smoke, September 2026

Frozen dataset. These files are evidence, not working files. Nothing in this
directory is regenerated, edited or re-scored in place. A new run gets a new
directory.

| File | Cell |
|---|---|
| `control.json` | no procedure, full 20-tool registry |
| `narrow.json` | no procedure, the skill's toolset |
| `skill.json` | the skill body, the same toolset as narrow |
| `skill-repeat.json` | an unplanned second run of the skill cell |
| `qualify.json` | server qualification record from the same session |

## Provenance

Every row carries its own identity. All five files agree on:

```
source_sha256       08d5e0fe6f2be291a0d62e14d664b4909932ee6041ecf5a78d4160af27a6641a
base_prompt_sha256  37f984092fed31eac321cf724946fcaffff590c1f624fb54777674e0c5cc4994
package_commit      c92a242b88c67bc0080018791df04fc9b69c0f3e
model               qwen3-coder-30b, UD-Q4_K_XL
runtime             llama.cpp b10816-427291b5b, CPU
```

### The commit pointer was rewritten

`package_commit` in these files reads `c92a242b88c67bc0080018791df04fc9b69c0f3e`.
**That commit no longer exists.** On 2026-09-08 the repository history was
rewritten before publication to remove a colleague's name from a commit message
and from an operational document, and some third-party configuration detail
whose provenance could not be confirmed. Rewriting messages changes every
commit hash downstream of the change.

```
old  c92a242b88c67bc0080018791df04fc9b69c0f3e   docs: run card and changelog since 8396ab1e
new  00ea371                                    same subject, same tree
```

The rewrite touched no file in the hashed set, so **`source_sha256` is
unchanged at `08d5e0fe...` and still reproduces from the working tree**. That
is the identity these datasets are reconciled against; the commit is a
convenience pointer and it is recorded here as broken rather than quietly
corrected in the frozen files. Evidence does not get edited to tidy it up.

**`base_prompt_sha256` is the one that decides comparability.** It fingerprints
the model-facing contract. Any dataset carrying a different value was collected
under a different experiment and must not be pooled with these, whatever else
matches. `source_sha256` fingerprints the instrument: a change there means the
evaluator moved, which is recoverable by re-scoring only when the rows recorded
enough evidence to re-score honestly (`devtools/rescore.py` decides that per
row and refuses to guess).

## Headline

```
                   control    narrow     skill
capability            3/10      8/10      8/10
tool calls             138        76        72
wall clock          41.6 m    24.9 m    21.1 m
scope violations         4         0         0
```

The analysis is in `docs/results/2026-09-30b-smoke.md`. Read the caveats there
before quoting any of this: ten synthetic tasks, one run per cell, one model,
no 8B, and therefore no statement about the interaction the project exists to
test.

## Reading the rows

```bash
python3 -c '
import json
d = json.load(open("results/2026-09-30b-smoke/narrow.json"))
for r in d["rows"]:
    print(f"{r[\"case\"]:24s} {r[\"succeeded\"]!s:6s} {r[\"tool_calls\"]:3d} calls  {r[\"elapsed_s\"]:6.1f}s")
'
```

Fields worth knowing:

- `required_checks` — hard gates, every one must pass, never averaged
- `claim_ok` — did the terminal claim match the case contract exactly
- `checks` — weighted quality rubric, threshold 0.75, cannot rescue a failed gate
- `history[].proof` — what the shared classifier made of each tool call
- `offered_tools`, `tool_schema_hash` — what this condition was actually given
- `tiered`, `escalated` — proof the row was served by one pinned model
- `scope_violation` — the model used a tool the task did not call for
