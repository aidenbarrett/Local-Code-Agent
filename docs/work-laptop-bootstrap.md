# Work laptop bootstrap

Windows-first bootstrap for Local Code Agent + Intel NPU bring-up.

## Normal use

From the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap-work-laptop.ps1
```

This performs user-space setup (repo venv, pinned OpenVINO packages, runtime directories and OVMS). It does **not** silently install machine-level prerequisites or enable Windows features.

Non-mutating preflight:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap-work-laptop.ps1 -CheckOnly
```

Optional switches:

```powershell
-InstallMissing      # explicitly allow WinGet installs for missing Git/Python
-SkipOvms
-AttemptWslInstall  # explicitly allow Microsoft wsl --install; may require admin/reboot
-OpenDriverPage
```

`-InstallMissing` and `-AttemptWslInstall` are intentionally explicit. The default path does not enable Windows features, install/update the NPU driver, or install machine-level Git/Python. On a managed laptop, prefer the default first run and only opt into prerequisite installation if policy allows it.

## Layout

The Git checkout contains source and `.venv-workstation`. Runtime files live outside the checkout:

```text
%LOCALAPPDATA%\LocalCodeAgent
├─ cache\
├─ logs\
├─ models\
├─ reports\
└─ tools\
   └─ ovms-2026.3.0\
```

## Checks

The script checks Windows version/architecture, CPU and machine model, admin state, WinGet, Git, Python >=3.11, WSL presence/usability, Intel NPU PnP visibility and driver version, VC++ runtime, editable Local Code Agent install, OpenVINO import/version, `Core().available_devices`, NPU visibility to OpenVINO, OVMS presence, and reachability of every official upstream URL used by the bootstrap.

It writes a timestamped JSON report under `%LOCALAPPDATA%\LocalCodeAgent\reports`.

## Version policy

The NPU bring-up stack is pinned to the OpenVINO 2026.3 generation for reproducibility:

- `openvino==2026.3.0`
- `openvino-tokenizers==2026.3.0.0`
- `openvino-genai==2026.3.0.0`
- OVMS `2026.3.0`, Windows `python_on` binary package

The OVMS archive is verified against the SHA-256 digest published with the pinned GitHub release before extraction (`e83ecc5dc47af390567b03c8ad1bf109ea6cdd88ef374ee7933fc303459b3ced`).

The project is installed editable because the current wheel intentionally does not package the top-level `skills/` directory.
