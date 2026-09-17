# Experiments

One directory per experiment. Each is self-contained and each is frozen the
moment its data lands.

```
<date>-<model>-<what-was-varied>/
    README.md     what this run was, how to read the files, what makes it comparable
    findings.md   the analysis: what it showed, what it did not, what to do next
    data/         the raw evaluator output, one file per cell
```

Directory names are `YYYY-MM-DD` of the run, the model, and the thing that was
varied. Sorting by name sorts by date.

## Rules

**Data is never edited in place.** Not to fix a typo, not to correct a field
that later turned out wrong, not to tidy a path. If something in a frozen file
is wrong or stale, that is recorded in the directory's `README.md` and the file
is left alone. A dataset that gets touched after collection is not evidence any
more.

**A new run gets a new directory.** Never a new file in an existing one.

**Findings live beside their data.** There is no separate analysis folder,
because an analysis separated from the run it describes eventually gets
attached to the wrong one.

## What makes two runs comparable

Every row in every file carries its own provenance. Two things decide whether
numbers from different runs may be put in the same table:

| Field | Meaning if it differs |
|---|---|
| `base_prompt_sha256` | **different experiment.** The model-facing contract changed. Never pool these, whatever else matches. |
| `source_sha256` | the instrument changed. Possibly recoverable by re-scoring with `measurement/rescore_dataset.py`, but only when the rows recorded enough evidence, which it decides per row and refuses to guess. |

Everything else, model, quant, runtime version, sampler, context budget,
offered tools and tool schema hash, is recorded per row so a difference can be
found rather than argued about.

## Reproducing an instrument hash

`source_sha256` hashes file contents, not git history, so it can be recomputed
from any checkout. Directory renames change it by design. The tree that
produced each dataset is tagged:

| Dataset | `source_sha256` | Tag |
|---|---|---|
| `2026-09-08-30b-three-conditions` | `08d5e0fe...` | `instrument-08d5e0fe` |

```bash
git checkout instrument-08d5e0fe
python -c "import sys; sys.path.insert(0,'src'); from local_agent import provenance; print(provenance.source_sha256())"
```

## Index

| Experiment | Model | Cells | Headline |
|---|---|---|---|
| [`2026-09-08-30b-three-conditions`](2026-09-08-30b-three-conditions/) | Qwen3-Coder-30B on CPU | control, narrow, skill, plus one skill repeat | Restricting the tool set took verified completion from 3/10 to 8/10. Adding the written procedure on top changed it by zero. |
| [`2026-09-08-30b-three-conditions-x3`](2026-09-08-30b-three-conditions-x3/) | Qwen3-Coder-30B on CPU | three repeats of all three conditions, balanced order | Narrowing 9/30 to 22/30, repeated in every repeat. The mechanism is off-contract action: 11 of 30 control rows were scope violations against 0 of 59 treatment rows. The narrow to skill difference stays small and unresolved. |
