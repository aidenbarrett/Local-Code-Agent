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
    Write-Host 'Run the workstation setup first, or install Python 3.11+.'
    Write-Host ''
    exit 2
}

function Show-Help {
    Write-Host ''
    Write-Host 'Local Code Agent'
    Write-Host ''
    Write-Host 'Use a local AI model to work with a code repository through approved'
    Write-Host 'tools, task procedures and independent verification.'
    Write-Host ''
    Write-Host 'Direct model conversation is separate:'
    Write-Host '  .\chat.ps1 qwen3-8b-npu'
    Write-Host ''
    Write-Host 'Commands:'
    Write-Host ''
    Write-Host '  capabilities'
    Write-Host '      Show what the agent can and cannot do.'
    Write-Host ''
    Write-Host '  run-task "<task>" [agent options]'
    Write-Host '      Run a controlled engineering task against the current repository.'
    Write-Host ''
    Write-Host '  verification-demo'
    Write-Host '      Show stale passing tests being rejected as invalid evidence.'
    Write-Host ''
    Write-Host '  advanced [arguments]'
    Write-Host '      Pass arguments directly to the existing internal CLI.'
    Write-Host ''
    Write-Host 'Examples:'
    Write-Host '  .\local-code-agent.ps1 capabilities'
    Write-Host '  .\local-code-agent.ps1 run-task "Inspect this repository and summarize how it builds" --skill repo-navigation'
    Write-Host '  .\local-code-agent.ps1 verification-demo'
    Write-Host ''
}

switch ($Command.ToLowerInvariant()) {
    'help' {
        Show-Help
        exit 0
    }
    'capabilities' {
        & $python (Join-Path $root 'scripts\capabilities.py') @Rest
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
        & $python -m local_agent.cli run @Rest
        exit $LASTEXITCODE
    }
    'verification-demo' {
        & $python (Join-Path $root 'scripts\demo-trust-boundary.py') @Rest
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
