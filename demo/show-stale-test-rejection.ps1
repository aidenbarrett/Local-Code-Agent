[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$entry = Join-Path $root 'local-code-agent.ps1'
if (-not (Test-Path $entry)) { throw "Local Code Agent entrypoint is missing: $entry" }

# Splatting a null $Rest forwards one empty string, which argparse rejects.
if ($Rest) { & $entry verification-demo @Rest } else { & $entry verification-demo }
exit $LASTEXITCODE
