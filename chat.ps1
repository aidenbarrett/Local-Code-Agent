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

$python = @(
    (Join-Path $root '.venv-workstation\Scripts\python.exe'),
    (Join-Path $root '.venv\Scripts\python.exe')
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $python) {
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $python) {
    Write-Host ''
    Write-Host '  No Python environment found.'
    Write-Host '  Run the workstation setup first, or install Python 3.11+.'
    Write-Host ''
    exit 2
}

if (-not $Rest) { $Rest = @('list') }
& $python (Join-Path $root 'scripts\chat.py') @Rest
exit $LASTEXITCODE
