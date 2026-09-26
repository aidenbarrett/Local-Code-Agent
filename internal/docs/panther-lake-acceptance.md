# Panther Lake physical acceptance

This is the evidence procedure for the final first-release hardware gate. It is not an experiment-generation change and it must not modify frozen historical artifacts.

The acceptance target is the current public `local-code-agent.ps1 session` product path on the physical Windows Panther Lake machine with external networking unavailable. Passing CI, an accelerator demo, a configured `NPU` profile, an observed model name, or an older physical smoke does not establish this gate.

## 1. Prepare while networking is still available

Use the existing work-laptop bootstrap and one-shot flow to provision the approved runtime, model and qualification report. Update the checkout and install dependencies before disconnecting.

Do not collect the acceptance bundle from a dirty checkout. The capture helper intentionally refuses one.

## 2. Disconnect external networking

Disable the interfaces/routes that provide external connectivity. The capture helper fails closed if Windows still reports an alive IPv4 or IPv6 default route on an Up adapter. That is the automated offline gate recorded in the bundle; record any additional corporate-lab isolation evidence separately if relevant.

## 3. Capture preflight

From the repository root, using the checkout that will actually be rehearsed:

```powershell
.\.venv-workstation\Scripts\python.exe internal\scripts\panther-lake-acceptance.py preflight `
  --repo . `
  --runtime-root "$env:LOCALAPPDATA\LocalCodeAgent" `
  --output "$env:LOCALAPPDATA\LocalCodeAgent\reports\panther-lake-preflight.json"
```

Preflight records or verifies:

- exact Git HEAD and a clean checkout;
- absence of an active default route;
- present Windows NPU device and driver facts;
- the managed runtime profile;
- the local model manifest and its hash;
- the server qualification report and its hash;
- the current public Session Hub `--check` path.

A preflight result is deliberately labelled `preflight-only`. It is not NPU acceptance.

## 4. Rehearse the public journeys

Run the real public Session Hub, not a lower-level demo:

```powershell
.\local-code-agent.ps1 session --repo . --profile ptl-npu-8b
```

Exercise the current first-release journeys from `TRICKS.md`, including at minimum repository inspection/source navigation, branch review, build success/failure with task-specific proof, failure follow-up, Stop, exit/restart and retained-result recovery. Capture logs/screenshots or terminal recordings that preserve task identity, verdict/evidence detail and runtime facts.

Perform a cold and a warm rehearsal and record the measured latency values using the same declared measurement point for both. The capture helper stores the values; it does not invent or infer them.

## 5. Finalize the evidence bundle

Pass the actual evidence files plus the measured cold/warm latency:

```powershell
.\.venv-workstation\Scripts\python.exe internal\scripts\panther-lake-acceptance.py finalize `
  --preflight "$env:LOCALAPPDATA\LocalCodeAgent\reports\panther-lake-preflight.json" `
  --output "$env:LOCALAPPDATA\LocalCodeAgent\reports\panther-lake-acceptance.json" `
  --cold-latency-ms 1234 `
  --warm-latency-ms 456 `
  --evidence C:\path\to\cold-session.log `
  --evidence C:\path\to\warm-session.log
```

Finalization re-checks the exact checkout and offline gate, rejects missing evidence or non-positive timings, hashes each supplied evidence file and writes `self_certified: false`.

That last field is intentional. The deterministic capture tool can prove that the evidence bundle is internally tied to the same checkout and offline conditions; it cannot judge whether a screenshot/log demonstrates actual NPU utilization or whether every public journey met the acceptance contract. Review the bundle against `TRICKS.md` before narrowing or expanding any supported-hardware claim.

## What remains manual

- interacting with the Textual Session Hub;
- selecting a consistent latency measurement point and recording it;
- collecting screenshots/terminal logs where the UI itself is the evidence surface;
- reviewing observed device/runtime evidence and journey outcomes;
- deciding whether the physical configuration satisfies the first-release support claim.

Those are deliberately not converted into a self-certifying script.
