[CmdletBinding()]
param(
    [double]$Seconds = 20,
    [string]$RuntimeRoot = "$env:LOCALAPPDATA\LocalCodeAgent",
    [switch]$KeepServer
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$entry = Join-Path $root 'internal\scripts\demo-accelerator.ps1'
if (-not (Test-Path $entry)) { throw "Demo implementation is missing: $entry" }

& $entry -Device CPU -Seconds $Seconds -RuntimeRoot $RuntimeRoot -KeepServer:$KeepServer
exit $LASTEXITCODE
