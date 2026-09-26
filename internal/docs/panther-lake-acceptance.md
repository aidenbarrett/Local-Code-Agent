# Panther Lake physical acceptance

This is the evidence procedure for the first-release hardware gate. It exercises the current public product path on the physical Windows target machine with external networking unavailable. Passing CI, a hardware demo, a configured device profile or an older smoke run does not establish this gate.

## 1. Prepare while networking is available

Use the public setup path to provision the approved runtime, model and local toolchain:

```powershell
.\install.ps1
```

Update the checkout and dependencies before disconnecting. Do not collect the final acceptance bundle from a dirty checkout.

## 2. Disconnect external networking

Disable the interfaces/routes that provide WAN connectivity while retaining the local inference path required by the product. Record the exact isolation method used. Unknown network state is not offline evidence.

## 3. Capture preflight

From the repository root:

```powershell
.\.venv-workstation\Scripts\python.exe internal\scripts\panther-lake-acceptance.py preflight `
  --repo . `
  --runtime-root "$env:LOCALAPPDATA\LocalCodeAgent" `
  --output "$env:LOCALAPPDATA\LocalCodeAgent\reports\panther-lake-preflight.json"
```

Preflight records or verifies the exact Git HEAD, clean checkout, offline gate, present device/driver facts, configured runtime profile, local model identity and public Session Hub self-check.

A preflight result is not hardware acceptance by itself.

## 4. Rehearse the public journeys

Run the public Session Hub:

```powershell
.\local-code-agent.ps1 session --repo . --profile ptl-npu-8b
```

Exercise the current journeys in `TRICKS.md`, including repository inspection/source navigation, branch review, build/test proof, failure follow-up, Stop, exit/restart and retained-result recovery. Capture logs/screenshots or terminal recordings that preserve task identity, verdict/evidence detail and runtime facts.

Physical runtime/device evidence must come from an observation that can actually identify the executing device. A configured profile name is not enough.

## 5. Measure cold/warm runtime behavior

Use the generic endpoint harness with a local profile for the exact running endpoint. Keep backend-specific commands, identity probes and optional telemetry in that local profile.

```powershell
python internal/perf/endpoint_harness.py `
  --profile .local-agent/perf/target.toml `
  --cold-start `
  --context-sizes 2048,8192,32768 `
  --output "$env:LOCALAPPDATA\LocalCodeAgent\reports\endpoint-performance.json"
```

Retain the JSON beside the acceptance evidence. Client-observed timing and backend-reported telemetry remain separately labelled.

## 6. Finalize the evidence bundle

Pass the actual journey evidence plus measured cold/warm latency values required by the existing capture script:

```powershell
.\.venv-workstation\Scripts\python.exe internal\scripts\panther-lake-acceptance.py finalize `
  --preflight "$env:LOCALAPPDATA\LocalCodeAgent\reports\panther-lake-preflight.json" `
  --output "$env:LOCALAPPDATA\LocalCodeAgent\reports\panther-lake-acceptance.json" `
  --cold-latency-ms 1234 `
  --warm-latency-ms 456 `
  --evidence C:\path\to\cold-session.log `
  --evidence C:\path\to\warm-session.log `
  --evidence "$env:LOCALAPPDATA\LocalCodeAgent\reports\endpoint-performance.json"
```

Finalization ties the supplied evidence to the checkout and offline preflight. It must not self-certify whether screenshots/logs prove physical device utilisation or whether every user journey met its contract. That judgement remains an explicit acceptance review against `TRICKS.md`.

## What remains manual

- interacting with the real Textual Session Hub
- collecting journey evidence where the UI is part of the acceptance surface
- reviewing observed runtime/device proof
- checking failure/restart/Stop behaviour against the user contract
- deciding whether the exact physical configuration satisfies the supported-hardware claim

Those are deliberately not converted into a script that grades its own evidence.
