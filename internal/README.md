# internal/

The repository root is the product surface. `internal/` contains implementation, tests, operator tooling and deeper documentation.

## What lives where

| Directory | Purpose |
|---|---|
| `local_agent/` | deterministic controller, model client, tools, policy, durable session/runtime logic |
| `serving/` | owned local-runtime process control used by product entrypoints |
| `perf/` | neutral OpenAI-compatible endpoint performance harness and example profile |
| `skills/` | model-facing task procedures loaded on demand |
| `scripts/` | product composition roots, operator utilities and demos |
| `tests/` | unit and integration regression coverage |
| `devtools/` | repository engineering and CI support utilities |
| `docs/` | architecture, product planning, verification and acceptance documentation |
| `personas/` | optional raw-chat persona configuration |
| `ui/` | presentation assets |
| `benchmark_fixture/` | retained synthetic C++ fixture used by some engineering tests |
| `evaluation/` | retained evaluator code used by existing engineering/test paths; not a historical-results store |

Historical research artifacts and the old experiment measurement stack are intentionally not present on the active product branch. The exact pre-cleanup tree is preserved on `archive/legacy-experiments-2026-09-26` at `ad07af071a62acfa4d0168c7a89d7110a52088f6`.

## Product source identity

`local_agent.provenance.source_sha256()` hashes the live product surfaces that can affect user-visible behavior or runtime composition. It includes the agent, serving code, product scripts, skills, the runtime session schema and root launch/install metadata. Test code and archived research are not part of that identity.

A packaged build may also record the exact Git commit and dirty state in `PACKAGE.json`. Unknown provenance fails closed rather than producing a clean-looking package stamp.

## Running the suite

Native pytest is authoritative:

```text
python -m pytest -q
```

The repository's own `.local-agent.toml` uses that same command, so the agent does not depend on a second compatibility test runner.

## Endpoint performance work

Use `perf/endpoint_harness.py` for runtime comparison. Backend-specific commands, telemetry URLs and device identities belong in local profile files under an ignored local path such as `.local-agent/perf/`.

Do not add backend-specific branching to the harness. Optional backend telemetry must remain explicitly labelled as backend-reported; client timings stay client-observed.
