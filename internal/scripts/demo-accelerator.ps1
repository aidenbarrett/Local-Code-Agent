param(
    [ValidateSet("CPU", "GPU", "NPU", "ALL")]
    [string]$Device = "NPU",
    [double]$Seconds = 45,
    [string]$RuntimeRoot = "$env:LOCALAPPDATA\LocalCodeAgent",
    # Leave the server running after the load ends, so chat.ps1 can use it.
    [switch]$KeepServer
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$InternalRoot = Split-Path -Parent $ScriptDir
$Root = Split-Path -Parent $InternalRoot
$Candidates = @(
    (Join-Path $Root ".venv-workstation\Scripts\python.exe"),
    (Join-Path $Root ".venv\Scripts\python.exe")
)
$Python = $Candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Python) {
    $Python = "python"
}

# Match the canonical work-laptop bootstrap: OVMS is installed under the
# managed runtime root rather than assumed to be on PATH. setupvars.ps1 carries
# the child-runtime DLL/Python environment, but PYTHONHOME/PYTHONPATH must not
# poison the controller venv, so preserve them for the OVMS child only.
$OvmsDir = Join-Path $RuntimeRoot "tools\ovms-2026.3.0"
$OvmsExe = Get-ChildItem $OvmsDir -Recurse -Filter ovms.exe -ErrorAction SilentlyContinue | Select-Object -First 1
$SetupVars = Get-ChildItem $OvmsDir -Recurse -Filter setupvars.ps1 -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $OvmsExe -or -not $SetupVars) {
    throw "OpenVINO Model Server is not installed under $OvmsDir. Run .\install.ps1 first."
}

$HadPythonHome = Test-Path Env:PYTHONHOME
$PythonHomeBefore = if ($HadPythonHome) { $env:PYTHONHOME } else { $null }
$HadPythonPath = Test-Path Env:PYTHONPATH
$PythonPathBefore = if ($HadPythonPath) { $env:PYTHONPATH } else { $null }

. $SetupVars.FullName
if (Test-Path Env:PYTHONHOME) { $env:LCA_OVMS_PYTHONHOME = $env:PYTHONHOME }
else { Remove-Item Env:LCA_OVMS_PYTHONHOME -ErrorAction SilentlyContinue }
if (Test-Path Env:PYTHONPATH) { $env:LCA_OVMS_PYTHONPATH = $env:PYTHONPATH }
else { Remove-Item Env:LCA_OVMS_PYTHONPATH -ErrorAction SilentlyContinue }

if ($HadPythonHome) { $env:PYTHONHOME = $PythonHomeBefore }
else { Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue }
if ($HadPythonPath) { $env:PYTHONPATH = $PythonPathBefore }
else { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }

$ExistingPythonPath = if (Test-Path Env:PYTHONPATH) { $env:PYTHONPATH } else { $null }
$env:PYTHONPATH = if ($ExistingPythonPath) { "$InternalRoot;$ExistingPythonPath" } else { $InternalRoot }

$DemoArgs = @(
    (Join-Path $ScriptDir "demo-accelerator.py")
    "--device", $Device
    "--seconds", $Seconds
    "--runtime-root", $RuntimeRoot
    "--executable", $OvmsExe.FullName
)
if ($KeepServer) { $DemoArgs += "--keep-server" }

& $Python @DemoArgs
exit $LASTEXITCODE
