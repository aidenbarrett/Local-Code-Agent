param(
    [ValidateSet("CPU", "GPU", "NPU", "ALL")]
    [string]$Device = "NPU",
    [double]$Seconds = 45
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path (Split-Path -Parent $ScriptDir) ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = "python"
}

& $Python (Join-Path $ScriptDir "demo-accelerator.py") --device $Device --seconds $Seconds
exit $LASTEXITCODE
