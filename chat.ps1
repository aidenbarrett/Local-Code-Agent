<#
.SYNOPSIS
  Talk directly to a local model. No agent, no tools, no repository access.

.EXAMPLE
  .\chat.ps1
  .\chat.ps1 qwen3-8b-npu
  .\chat.ps1 qwen3-8b-gpu
#>
# Not named $Args: that is a PowerShell automatic variable.
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$internal = Join-Path $root 'internal'

$localAppData = $env:LOCALAPPDATA
if (-not $localAppData) {
    $localAppData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
}
if (-not $localAppData) {
    $localAppData = Join-Path $HOME '.local'
}
$runtimeRoot = Join-Path $localAppData 'LocalCodeAgent'

$python = @(
    (Join-Path $root '.venv-workstation\Scripts\python.exe'),
    (Join-Path $root '.venv\Scripts\python.exe')
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $python) {
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $python) {
    Write-Host ''
    Write-Host 'No Python environment found.'
    Write-Host 'Run .\install.ps1 first, or install Python 3.11+.'
    Write-Host ''
    exit 2
}

if (-not $Rest) { $Rest = @('list') }

# Direct scripts live under internal/, while the root remains the product-facing
# surface. Keep this process-local so nothing is written into the user's shell.
$existingPythonPath = if (Test-Path Env:PYTHONPATH) { $env:PYTHONPATH } else { $null }
$env:PYTHONPATH = if ($existingPythonPath) { "$internal;$existingPythonPath" } else { $internal }
$env:LCA_RUNTIME_ROOT = $runtimeRoot

# OpenVINO Model Server is installed by install.ps1 under the managed runtime
# root rather than assumed to be globally on PATH. Import its child runtime
# environment without poisoning the Python interpreter that runs the controller.
if ($Rest[0] -ne 'list') {
    $ovmsDir = Join-Path $runtimeRoot 'tools\ovms-2026.3.0'
    $ovmsExe = Get-ChildItem $ovmsDir -Recurse -Filter ovms.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    $setupVars = Get-ChildItem $ovmsDir -Recurse -Filter setupvars.ps1 -ErrorAction SilentlyContinue | Select-Object -First 1

    if ($ovmsExe -and $setupVars) {
        $hadPythonHome = Test-Path Env:PYTHONHOME
        $pythonHomeBefore = if ($hadPythonHome) { $env:PYTHONHOME } else { $null }
        $pythonPathBeforeSetup = $env:PYTHONPATH

        . $setupVars.FullName
        if (Test-Path Env:PYTHONHOME) { $env:LCA_OVMS_PYTHONHOME = $env:PYTHONHOME }
        else { Remove-Item Env:LCA_OVMS_PYTHONHOME -ErrorAction SilentlyContinue }
        if (Test-Path Env:PYTHONPATH) { $env:LCA_OVMS_PYTHONPATH = $env:PYTHONPATH }
        else { Remove-Item Env:LCA_OVMS_PYTHONPATH -ErrorAction SilentlyContinue }

        if ($hadPythonHome) { $env:PYTHONHOME = $pythonHomeBefore }
        else { Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue }
        $env:PYTHONPATH = $pythonPathBeforeSetup
        $env:LCA_OVMS_EXECUTABLE = $ovmsExe.FullName
    }
}

& $python (Join-Path $internal 'scripts\chat.py') @Rest
exit $LASTEXITCODE
