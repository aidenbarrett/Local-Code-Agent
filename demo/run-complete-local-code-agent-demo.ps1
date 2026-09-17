[CmdletBinding()]
param(
    [double]$Seconds = 12,
    [string]$RuntimeRoot = "$env:LOCALAPPDATA\LocalCodeAgent"
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

Write-Host ''
Write-Host 'LOCAL CODE AGENT - COMPLETE DEMO'
Write-Host ''
Write-Host '1/3  Local Qwen3-8B on the NPU'
& (Join-Path $root 'demo\run-qwen-on-npu.ps1') -Seconds $Seconds -RuntimeRoot $RuntimeRoot
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ''
Write-Host '2/3  Controlled agent capabilities'
& (Join-Path $root 'local-code-agent.ps1') capabilities
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ''
Write-Host '3/3  Independent verification rejects stale passing tests'
& (Join-Path $root 'demo\show-stale-test-rejection.ps1')
exit $LASTEXITCODE
