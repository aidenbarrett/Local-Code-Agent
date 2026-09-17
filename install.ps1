<#
.SYNOPSIS
  Prepare and validate this machine for Local Code Agent.

.DESCRIPTION
  This is the public setup entrypoint. It delegates to the existing deterministic
  workstation bootstrap and validation path under internal/.
#>
[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [switch]$InstallMissing,
    [switch]$AttemptWslInstall,
    [switch]$OpenDriverPage,
    [string]$RuntimeRoot = "$env:LOCALAPPDATA\LocalCodeAgent"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$entry = Join-Path $root 'internal\work-laptop-one-shot.ps1'

if (-not (Test-Path $entry)) {
    throw "Local Code Agent installation files are incomplete: $entry is missing."
}

Write-Host ''
Write-Host 'LOCAL CODE AGENT'
Write-Host ''
Write-Host 'Preparing and validating the local runtime...'
Write-Host ''

$forward = @{ RuntimeRoot = $RuntimeRoot }
if ($CheckOnly) { $forward.CheckOnly = $true }
if ($InstallMissing) { $forward.InstallMissing = $true }
if ($AttemptWslInstall) { $forward.AttemptWslInstall = $true }
if ($OpenDriverPage) { $forward.OpenDriverPage = $true }

& $entry @forward
$rc = $LASTEXITCODE
if ($rc -ne 0) { exit $rc }

if (-not $CheckOnly) {
    Write-Host ''
    Write-Host 'Next:'
    Write-Host '  .\chat.ps1 qwen3-8b-npu'
    Write-Host '  .\local-code-agent.ps1 capabilities'
    Write-Host '  .\demo\run-qwen-on-npu.ps1'
    Write-Host ''
}
exit 0
