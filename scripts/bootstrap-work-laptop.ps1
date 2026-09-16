Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# Compatibility entry point for Windows PowerShell 5.1.
# This wrapper intentionally has no param() block so arguments splatted as a
# string array by the one-shot script remain visible in $args and can be parsed
# correctly. It also creates the workstation venv before delegating to the
# historical bootstrap implementation, avoiding the $Args automatic-variable
# collision in that implementation's RunPython helper.

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Core = Join-Path $PSScriptRoot "bootstrap-work-laptop-core.ps1"
$RuntimeRoot = "$env:LOCALAPPDATA\LocalCodeAgent"
$CheckOnly = $false
$InstallMissing = $false
$SkipOvms = $false
$AttemptWslInstall = $false
$OpenDriverPage = $false

for ($i = 0; $i -lt $args.Count; $i++) {
    $token = [string]$args[$i]
    switch ($token) {
        "-RuntimeRoot" {
            if ($i + 1 -ge $args.Count) { throw "-RuntimeRoot requires a value" }
            $i++
            $RuntimeRoot = [string]$args[$i]
        }
        "-CheckOnly" { $CheckOnly = $true }
        "-InstallMissing" { $InstallMissing = $true }
        "-SkipOvms" { $SkipOvms = $true }
        "-AttemptWslInstall" { $AttemptWslInstall = $true }
        "-OpenDriverPage" { $OpenDriverPage = $true }
        default { throw "Unknown bootstrap argument: $token" }
    }
}

# Windows PowerShell 5.1 rejects Range when supplied through -Headers because
# Range is a restricted WebHeaderCollection member. The retained core uses a
# one-byte GET only as a fallback when HEAD is refused. Strip that compatibility
# probe header and perform a normal GET instead; module qualification prevents
# recursion into this wrapper. PowerShell 7 does not need the shim, but keeping
# the behavior here makes the canonical entry point deterministic across both.
function Invoke-WebRequest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true)][string]$Uri,
        [string]$Method = "Get",
        [hashtable]$Headers,
        [int]$MaximumRedirection,
        [switch]$UseBasicParsing,
        [int]$TimeoutSec,
        [string]$OutFile
    )

    $native = @{ Uri = $Uri }
    if ($PSBoundParameters.ContainsKey("Method")) { $native.Method = $Method }
    if ($PSBoundParameters.ContainsKey("MaximumRedirection")) { $native.MaximumRedirection = $MaximumRedirection }
    if ($PSBoundParameters.ContainsKey("UseBasicParsing")) { $native.UseBasicParsing = $UseBasicParsing }
    if ($PSBoundParameters.ContainsKey("TimeoutSec")) { $native.TimeoutSec = $TimeoutSec }
    if ($PSBoundParameters.ContainsKey("OutFile")) { $native.OutFile = $OutFile }

    if ($PSBoundParameters.ContainsKey("Headers") -and $Headers) {
        $safeHeaders = @{}
        foreach ($key in $Headers.Keys) {
            if ([string]$key -ieq "Range") { continue }
            $safeHeaders[$key] = $Headers[$key]
        }
        if ($safeHeaders.Count -gt 0) { $native.Headers = $safeHeaders }
    }

    Microsoft.PowerShell.Utility\Invoke-WebRequest @native
}

function Find-UsablePython {
    $candidates = @(
        @{ Exe = "py"; Prefix = @("-3.12") },
        @{ Exe = "py"; Prefix = @("-3") },
        @{ Exe = "python"; Prefix = @() },
        @{ Exe = "python3"; Prefix = @() }
    )
    foreach ($candidate in $candidates) {
        if ($null -eq (Get-Command $candidate.Exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $version = & $candidate.Exe @($candidate.Prefix) -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')" 2>$null
            if ($LASTEXITCODE -ne 0 -or !$version) { continue }
            $parts = $version.Trim().Split('.')
            if (([int]$parts[0] -gt 3) -or (([int]$parts[0] -eq 3) -and ([int]$parts[1] -ge 11))) {
                return $candidate
            }
        } catch {}
    }
    return $null
}

if (!(Test-Path $Core)) { throw "Missing bootstrap core: $Core" }

if (!$CheckOnly) {
    $venvPython = Join-Path $RepoRoot ".venv-workstation\Scripts\python.exe"
    if (!(Test-Path $venvPython)) {
        $py = Find-UsablePython
        if (!$py -and $InstallMissing) {
            if ($null -eq (Get-Command winget -ErrorAction SilentlyContinue)) {
                throw "Python >=3.11 is missing and WinGet is unavailable. Install Python 3.12 and rerun."
            }
            & winget install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -ne 0) { throw "Python 3.12 install failed ($LASTEXITCODE)" }
            $env:Path = ([Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User"))
            $py = Find-UsablePython
        }
        if (!$py) { throw "Python >=3.11 is required. Install Python 3.12 or rerun with -InstallMissing." }

        Write-Host "[bootstrap-entry] creating .venv-workstation"
        & $py.Exe @($py.Prefix) -m venv (Join-Path $RepoRoot ".venv-workstation")
        if ($LASTEXITCODE -ne 0 -or !(Test-Path $venvPython)) {
            throw "Failed to create workstation virtual environment"
        }
    }
}

$coreArgs = @{ RuntimeRoot = $RuntimeRoot }
if ($CheckOnly) { $coreArgs.CheckOnly = $true }
if ($InstallMissing) { $coreArgs.InstallMissing = $true }
if ($SkipOvms) { $coreArgs.SkipOvms = $true }
if ($AttemptWslInstall) { $coreArgs.AttemptWslInstall = $true }
if ($OpenDriverPage) { $coreArgs.OpenDriverPage = $true }

& $Core @coreArgs
exit $LASTEXITCODE
