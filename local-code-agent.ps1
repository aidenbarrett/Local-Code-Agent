<#
.SYNOPSIS
  User-facing entrypoint for the controlled repository agent.

.DESCRIPTION
  Direct model chat lives in chat.ps1.
  Local Code Agent adds controlled repository access, approved tools, skills,
  policy and independent verification.
#>
param(
    [string]$Command = 'help',
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
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
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

function Show-Help {
    & $python (Join-Path $internal 'scripts\product-help.py')
}

switch ($Command.ToLowerInvariant()) {
    'session' {
        & $python -m local_agent.session.cli @Rest
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

        # Product presentation lives outside the measured agent source. The
        # presenter still delegates server ownership to chat.ps1 and execution
        # through the controlled developer path using the provisioned ptl-npu-8b profile.
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
