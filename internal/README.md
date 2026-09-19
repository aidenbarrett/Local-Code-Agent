# internal/

The root of the repository is the product surface: what a first-time user runs. This
directory is everything behind it. A stranger who opens the repository should never be
sent in here to make the product work.

## What lives where

| Directory | What it is | Part of the instrument? |
|---|---|---|
| `local_agent/` | the agent itself: controller, worker, tools, policy, session hub, inference clients | yes, every `.py` |
| `evaluation/` | what makes a result count: task contracts, endpoints, the oracle, the evaluator | yes, every `.py` |
| `measurement/` | qualification, benchmarking, experiment launch, the offline test runner | yes, `.py` and `.sh` |
| `skills/` | model-facing skill documents, loaded on demand | yes, every file |
| `benchmark_fixture/cpp_project/` | the C++ project the agent is measured on | yes, every file |
| `docs/session-contract/v1/` | the versioned wire contract the validator loads at runtime | yes, the JSON |
| `tests/` | unit and integration tests | no |
| `devtools/` | developer utilities that inspect the repository, such as the rename pre-flight | no |
| `scripts/` | operator tools, demos, product help, chat entry points | no |
| `docs/` | design, methodology, review history, bring-up, and future-client/interface notes | no, except `session-contract/v1` |
| `experiments/` | frozen collected evidence | no, and never edited |
| `personas/` | chat persona configuration | no |

Future UI, VS Code and not-yet-implemented Session Hub interfaces live under `docs/`
until real runtime code exists. A documentation-only directory must not masquerade as
an implementation package.

"Part of the instrument" means the file's bytes feed `source_sha256`. Editing one
changes the identity of the thing that produced every measurement. That is not a reason
to avoid editing it; it is a reason to know you did.

The table above is not the authority. `provenance._HASHED` is, and
`internal/tests/unit/test_hashed_surface_membership.py` fails if the two disagree, in
either direction. If you add a directory here, that test will fail until you classify
it. That is the intended behaviour, not an obstacle: a new area silently joining or
leaving the instrument is a bug we have already had.

## The three identities

| Hash | Covers | Moving it means |
|---|---|---|
| `source_sha256` | behavioural source bytes and their canonical paths | update `INSTRUMENT.json` in the same change |
| `base_prompt_sha256` | what the model is told | **ends a generation once rows exist** |
| `outcome_contract_sha256` | what the evaluator calls correct | **ends a generation once rows exist** |

Recompute all three from the repository root. Windows PowerShell or WSL, either works:

```text
python -c "import sys; sys.path.insert(0,'internal'); from local_agent import provenance as p; print(p.source_sha256()); print(p.base_prompt_sha256()); print(p.outcome_contract_sha256())"
```

Generation 2 has zero collected model rows, and both contract axes are already
deliberately pinned. Zero rows does not make them casually editable; it makes an explicit
generation change cheap rather than expensive. Either way, moving one is a decision taken
on purpose and stated in the PR, never a side effect of a rename.

Once the first Generation-2 row exists, moving either contract axis ends Generation 2
for confirmatory comparison and requires an explicit new-generation decision.

## Running the suite

Native pytest is authoritative. From the repository root:

```text
python -m pytest -q
```

The offline runner exists for machines with no package index, and is not authoritative:

```text
python internal/measurement/run_test_suite.py internal/tests
```

That second command is also what `.local-agent.toml` points the agent at when it tests
its own checkout, which is why `internal/tests/unit/test_self_test_path_is_executable.py`
executes it rather than trusting the string.

## Before you rename or move anything in here

```text
python internal/devtools/check_rename_safety.py
```

It reports which of the three identities a rename would move, whether a name collides
with a frozen wire value, what imports a module by bare name because something put a
directory on `sys.path`, and which scripts and configuration files name its path. It
exits non-zero on a real obstacle and on anything it could not analyse.

A clean run means no contract-axis obstacle was detected. It does not mean the tree
still imports, and it does not mean the move is correct. Read its header for what it
cannot see.
