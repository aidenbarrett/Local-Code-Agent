[CmdletBinding()]
param(
    [double]$Seconds = 20,
    [string]$RuntimeRoot = "",
    [switch]$KeepServer
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $RuntimeRoot) { . (Join-Path $root 'internal\scripts\runtime-root.ps1'); $RuntimeRoot = Resolve-LcaRuntimeRoot }
$entry = Join-Path $root 'internal\scripts\demo-accelerator.ps1'
if (-not (Test-Path $entry)) { throw "Demo implementation is missing: $entry" }

& $entry -Device CPU -Seconds $Seconds -RuntimeRoot $RuntimeRoot -KeepServer:$KeepServer
exit $LASTEXITCODE
