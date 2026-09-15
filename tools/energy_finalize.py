from __future__ import annotations

import json
import sys
from pathlib import Path


ENERGY_SECTION = """

## 10. Energy arm is separate and instrumented before row one

The energy/device-efficiency arm is a separate observational study that shares workloads with the capability pilot but does not alter E1, E2 or E3. Missing energy telemetry never converts a correct coding task into a model failure; that row simply cannot support an energy claim. The binding measurement protocol is `docs/energy-methodology.md`.

Instrumentation lands before the first generation-2 model row because task energy cannot be reconstructed after execution without rerunning the workload. The required device observations are: joules per completed task, server-up idle energy/power, battery drain in mWh and percentage points kept separate from package energy, and real compile wall time while the agent runs concurrently.

Before any Panther Lake CPU/GPU/NPU energy comparison, physically qualify the selected HWiNFO sensor domain. In particular, `CPU Package Power [W]` must not be assumed to include NPU or iGPU power. Unqualified or proxy-only package telemetry is retained as supporting evidence but is not eligible for a target-device efficiency claim. No Panther Lake energy value has been measured by this project yet.

Dashboard work is deferred until data exists.
"""


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    prereg = root / "docs" / "next-experiment-preregistration.md"
    text = prereg.read_text(encoding="utf-8")
    marker = "## 10. Energy arm is separate and instrumented before row one"
    if marker not in text:
        prereg.write_text(text.rstrip() + ENERGY_SECTION + "\n", encoding="utf-8", newline="\n")

    sys.path.insert(0, str(root))
    from local_agent import provenance as p

    instrument = root / "INSTRUMENT.json"
    declared = json.loads(instrument.read_text(encoding="utf-8"))
    if declared.get("generation") != 2:
        raise SystemExit("energy instrumentation expects generation 2 before row one")
    prompt = p.base_prompt_sha256()
    outcome = p.outcome_contract_sha256()
    if prompt != declared.get("base_prompt_sha256"):
        raise SystemExit(f"base prompt moved unexpectedly: {prompt}")
    if outcome != declared.get("outcome_contract_sha256"):
        raise SystemExit(f"outcome contract moved unexpectedly: {outcome}")
    source = p.source_sha256()
    declared["source_sha256"] = source
    instrument.write_text(
        json.dumps(declared, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print("generation", declared["generation"])
    print("source_sha256", source)
    print("base_prompt_sha256", prompt)
    print("outcome_contract_sha256", outcome)


if __name__ == "__main__":
    main()
