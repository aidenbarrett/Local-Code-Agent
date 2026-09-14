# Work laptop bootstrap

Windows-first, end-to-end bootstrap for Local Code Agent + Intel NPU bring-up.

The canonical entry point is now `scripts/work-laptop-one-shot.ps1`. From an already-cloned repository it drives the machine from prerequisite checks through a qualified, running Qwen3-8B NPU endpoint and a passing C++ benchmark-fixture smoke test.

It deliberately does **not** start a scored Generation 2 evaluation automatically. Setup/qualification and scientific measurement remain separate.

## First use

Open **Windows PowerShell** in the cloned repository root.

Run the non-mutating preflight first:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\work-laptop-one-shot.ps1 -CheckOnly
```

Then run the full setup:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\work-laptop-one-shot.ps1
```

If approved machine prerequisites are missing and corporate policy allows WinGet installation:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\work-laptop-one-shot.ps1 -InstallMissing
```

`-InstallMissing` is explicit because it may install Git/Python through the base bootstrap, CMake, the VC++ runtime, and Microsoft Visual Studio 2022 Build Tools with the C++ workload. The scripts never silently install/update the Intel NPU driver and never silently enable Windows features. `-AttemptWslInstall` is also explicit; WSL is optional for the Windows-native NPU path.

## What the one-shot run does

1. Runs the base Windows bootstrap to inventory Windows/CPU/admin state, Git, Python, WSL, Intel NPU visibility and driver metadata.
2. Creates the repo `.venv-workstation` environment and installs Local Code Agent editable with dev dependencies.
3. Installs the pinned OpenVINO 2026.3 Python packages.
4. Verifies `openvino.Core().available_devices` contains `NPU`.
5. Installs/checks the Microsoft VC++ runtime as allowed by the selected mode.
6. Downloads the pinned OVMS 2026.3.0 Windows `python_on` package and verifies its published SHA-256 before extraction.
7. Requires CMake/CTest and a working C++ compiler. With `-InstallMissing`, it can install CMake and Visual Studio 2022 Build Tools; otherwise it imports an existing MSVC developer environment or accepts an existing Intel/LLVM/GCC compiler in `PATH`.
8. Pulls `OpenVINO/Qwen3-8B-int4-cw-ov` into the managed model repository with OVMS.
9. Hashes the downloaded model files into a local provenance manifest.
10. Starts OVMS on port `18000`, targeting NPU with Hermes3 tool parsing, prefix caching, an 8192-token prompt cap and the NPU prefill attention hint.
11. Waits up to 15 minutes for first-load NPU compilation/model readiness.
12. Runs `measurement/qualify_server.py` against the existing `ptl-npu-8b` Local Code Agent profile and writes the qualification evidence.
13. Copies the benchmark C++ fixture to a disposable cache directory and proves configure/build/test succeeds with the local toolchain.
14. Writes a timestamped workstation-readiness JSON report and leaves OVMS running.

The script prints `WORKSTATION READY` only when NPU serving, protocol qualification, and the C++ fixture smoke have all passed.

## Layout

Source stays in the Git checkout. Runtime/model state lives outside it:

The runtime root keeps weights in `models/`, compiled caches in
`cache/<profile>/`, and qualification/model manifests in `reports/`.
Controller state is in `runtime/<profile>/process.json` and `launch.json`, with
`stdout.log` and `stderr.log` alongside them. The one-shot readiness summary
also links the controller status saved in `runtime/profiles/ptl-npu-8b.json`.
See [serving.md](serving.md) for commands and ownership/refusal behaviour.


The runtime root can be changed with `-RuntimeRoot`.

## Pinned first target

The first Panther Lake path is intentionally one fixed target:

- Local Code Agent profile: `ptl-npu-8b`
- model: `OpenVINO/Qwen3-8B-int4-cw-ov`
- device: `NPU`
- API base URL: `http://127.0.0.1:18000/v3`
- prompt cap: `8192`
- tool parser: `hermes3`
- `openvino==2026.3.0`
- `openvino-tokenizers==2026.3.0.0`
- `openvino-genai==2026.3.0.0`
- OVMS `2026.3.0`, Windows `python_on`

The OVMS archive is checked against the release digest:

```text
e83ecc5dc47af390567b03c8ad1bf109ea6cdd88ef374ee7933fc303459b3ced
```

The model is the OpenVINO NPU-oriented INT4 compressed-weights conversion, not a GGUF and not the 30B GPU model. OVMS pulls it directly into the managed model repository. The local model manifest records SHA-256 for every downloaded model file so the actual payload used on the machine is inspectable.

## Why the C++ toolchain is a hard readiness gate

The evaluation exercises a CMake/CTest C++ repository. A model endpoint can be perfect while the benchmark is impossible to execute because `cmake`, `ctest`, or the compiler is missing. The one-shot script therefore refuses to print `WORKSTATION READY` until the exact local fixture has configured, built and tested successfully.

With `-InstallMissing`, the script may install Microsoft Visual Studio 2022 Build Tools with the C++ workload through WinGet. Without that switch it only detects/imports what is already provisioned. This keeps large machine-level changes explicit on a managed laptop.

## Qualification is not the experiment

`measurement/qualify_server.py` checks the endpoint assumptions Local Code Agent depends on, including transport, streaming, token/accounting behavior, tool calling and context behavior. It writes evidence under the runtime report directory.

The bootstrap does **not** execute `evaluation/run_evaluation.py`. A corporate proxy failure, first NPU compile problem, missing compiler, or setup mistake must never create a row that looks like experimental evidence. Once `WORKSTATION READY` is printed, the script shows an example evaluation command, but the scored run remains an explicit separate action under the experiment protocol.

## Components

- `scripts/bootstrap-work-laptop.ps1` — lower-level machine/user-space bootstrap used by the one-shot entry point.
- `scripts/work-laptop-one-shot.ps1` — canonical end-to-end orchestrator for the work laptop.
- `measurement/serve.py` owns preset resolution, preflight and process lifecycle.
- `scripts/start-ovms-detached.py` is the shared argv-safe process launcher, including Windows PowerShell 5.1 embedded JSON quoting.

The setup is idempotent by intent: existing venv, OVMS package, model repository and healthy running server are reused where safe. Ambiguous live/stale server state is reported instead of being killed automatically.
