<#
.SYNOPSIS
  Canonical user-facing entrypoint for Local Code Agent.

.DESCRIPTION
  Running this script with no command opens the Textual Session Hub.
  Raw model chat and lower-level automation/debug surfaces remain available as
  explicit subcommands behind the same product entrypoint.
#>
param(
    [string]$Command = 'session',
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$internal = Join-Path $root 'internal'

$python = @(
    (Join-Path $root '.venv-workstation\Scripts\python.exe'),
    (Join-Path $root '.venv\Scripts\python.exe')
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $python) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) { $python = $pythonCommand.Source }
}
if (-not $python) {
    Write-Host ''
    Write-Host 'No Python environment found.'
    Write-Host 'Run .\install.ps1 first, or install Python 3.11+.'
    Write-Host ''
    exit 2
}

$existingPythonPath = if (Test-Path Env:PYTHONPATH) { $env:PYTHONPATH } else { $null }
$env:PYTHONPATH = if ($existingPythonPath) { "$internal;$existingPythonPath" } else { $internal }

# Validate the exact interpreter selected above before any public command can import
# product code, start OVMS, or prepare a model. Startup is diagnostic-only: it never
# runs pip or reaches the network to repair a stale checkout environment.
$preflight = Join-Path $internal 'scripts\runtime-preflight.py'
$pyproject = Join-Path $root 'pyproject.toml'
if (-not (Test-Path $preflight) -or -not (Test-Path $pyproject)) {
    Write-Host ''
    Write-Host 'Local Code Agent checkout is incomplete.'
    Write-Host 'Run git status, restore the missing product files, then run .\install.ps1.'
    Write-Host ''
    exit 2
}

& $python $preflight --pyproject $pyproject
if ($LASTEXITCODE -ne 0) {
    Write-Host ''
    Write-Host 'Local Code Agent will not start with this Python environment.'
    Write-Host ("Selected interpreter: {0}" -f $python)
    Write-Host 'Run .\install.ps1 to update the checkout environment, then retry.'
    Write-Host ''
    exit 2
}

function Show-Help {
    & $python (Join-Path $internal 'scripts\product-help.py')
}

function Set-ManagedOvmsEnvironment {
    # Preserve the old direct-chat wrapper's managed OVMS setup while keeping one
    # product launcher. setupvars.ps1 may set PYTHONHOME/PYTHONPATH for OVMS; capture
    # those for the child server and restore the controller Python environment.
    $localAppData = $env:LOCALAPPDATA
    if (-not $localAppData) {
        $localAppData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
    }
    if (-not $localAppData) {
        $localAppData = Join-Path $HOME '.local'
    }
    $runtimeRoot = Join-Path $localAppData 'LocalCodeAgent'
    $env:LCA_RUNTIME_ROOT = $runtimeRoot

    $ovmsDir = Join-Path $runtimeRoot 'tools\ovms-2026.3.0'
    $ovmsExe = Get-ChildItem $ovmsDir -Recurse -Filter ovms.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    $setupVars = Get-ChildItem $ovmsDir -Recurse -Filter setupvars.ps1 -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $ovmsExe -or -not $setupVars) { return }

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

switch ($Command.ToLowerInvariant()) {
    'session' {
        Set-ManagedOvmsEnvironment
        & $python (Join-Path $internal 'scripts\session-hub.py') @Rest
        exit $LASTEXITCODE
    }
    'chat' {
        if (-not $Rest -or $Rest.Count -eq 0) { $Rest = @('list') }
        if ($Rest[0] -ne 'list') { Set-ManagedOvmsEnvironment }
        & $python (Join-Path $internal 'scripts\chat.py') @Rest
        exit $LASTEXITCODE
    }
    'help' {
        Show-Help
        exit $LASTEXITCODE
    }
    'capabilities' {
        & $python (Join-Path $internal 'scripts\capabilities.py') @Rest
        exit $LASTEXITCODE
    }
    'run-task' {
        if (-not $Rest -or $Rest.Count -eq 0) {
            Write-Host ''
            Write-Host 'A task is required.'
            Write-Host 'Example:'
            Write-Host '  .\local-code-agent.ps1 run-task "Inspect this repository and summarize how it builds" --skill repo-navigation'
            Write-Host ''
            exit 2
        }

        & $python (Join-Path $internal 'scripts\run-task-ui.py') @Rest
        exit $LASTEXITCODE
    }
    'verification-demo' {
        & $python (Join-Path $internal 'scripts\demo-trust-boundary.py') @Rest
        exit $LASTEXITCODE
    }
    'advanced' {
        & $python -m local_agent.cli @Rest
        exit $LASTEXITCODE
    }
    default {
        Write-Host ''
        Write-Host "Unknown command: $Command"
        Show-Help
        exit 2
    }
}