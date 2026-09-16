param(
    [ValidateSet("CPU", "GPU", "NPU", "ALL")]
    [string]$Device = "NPU",
    [double]$Seconds = 45
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$Candidates = @(
    (Join-Path $Root ".venv-workstation\Scripts\python.exe"),
    (Join-Path $Root ".venv\Scripts\python.exe")
)
$Python = $Candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Python) {
    $Python = "python"
}

& $Python (Join-Path $ScriptDir "demo-accelerator.py") --device $Device --seconds $Seconds
exit $LASTEXITCODE
