<#
.SYNOPSIS
  Prepare and validate this machine for Local Code Agent.

.DESCRIPTION
  This is the public setup entrypoint. It delegates to the existing deterministic
  workstation bootstrap and validation path under internal/.

  -CheckOnly is read-only.
  Bare invocation shows the approved install plan and asks before allowing
  machine-level prerequisite installation.
  -InstallMissing is the explicit non-interactive approval for those installs.
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
$runtimePreflight = Join-Path $root 'internal\scripts\runtime-preflight.py'
$managedPython = Join-Path $root '.venv-workstation\Scripts\python.exe'
$pyproject = Join-Path $root 'pyproject.toml'

if (-not (Test-Path $entry)) {
    throw "Local Code Agent installation files are incomplete: $entry is missing."
}
if (-not (Test-Path $runtimePreflight) -or -not (Test-Path $pyproject)) {
    throw 'Local Code Agent installation files are incomplete: runtime preflight contract is missing.'
}
if ($CheckOnly -and ($InstallMissing -or $AttemptWslInstall -or $OpenDriverPage)) {
    throw "-CheckOnly is read-only and cannot be combined with -InstallMissing, -AttemptWslInstall or -OpenDriverPage."
}

function Show-InstallPlan {
    Write-Host ''
    Write-Host 'SETUP PLAN'
    Write-Host ''
    Write-Host 'If missing, Local Code Agent is allowed to install these machine-level prerequisites:'
    Write-Host '  Git.Git'
    Write-Host '  Python.Python.3.12'
    Write-Host '  Kitware.CMake'
    Write-Host '  Microsoft.VisualStudio.2022.BuildTools (C++ workload)'
    Write-Host ''
    Write-Host 'Package source: WinGet source "winget".'
    Write-Host ''
    Write-Host 'Setup also creates/updates the project virtual environment, installs the pinned'
    Write-Host 'local runtime dependencies, prepares OVMS under the managed runtime directory,'
    Write-Host 'and may download the configured Qwen3-8B model if it is not already present.'
    Write-Host ''
    Write-Host 'WSL installation is never attempted unless -AttemptWslInstall is supplied.'
    Write-Host 'The NPU driver is never installed automatically; -OpenDriverPage only opens its page.'
    Write-Host ''
    Write-Host 'On a managed work machine, company policy may block one of these installs.'
    Write-Host 'If that happens, the setup will stop and name what needs IT approval.'
    Write-Host ''
}

$allowInstallMissing = $InstallMissing
if (-not $CheckOnly) {
    Show-InstallPlan
    if (-not $InstallMissing) {
        $inputRedirected = $false
        try { $inputRedirected = [Console]::IsInputRedirected } catch { $inputRedirected = $false }
        if (-not [Environment]::UserInteractive -or $inputRedirected) {
            Write-Host 'Bare .\install.ps1 requires an interactive confirmation before machine-level changes.'
            Write-Host 'Use .\install.ps1 -CheckOnly for a read-only preflight, or'
            Write-Host '.\install.ps1 -InstallMissing for non-interactive setup after prerequisite installation has been approved.'
            exit 2
        }

        $answer = Read-Host 'Continue with setup? [y/N]'
        if ($null -eq $answer -or @('y','yes') -notcontains $answer.Trim().ToLowerInvariant()) {
            Write-Host ''
            Write-Host 'No setup changes were made.'
            Write-Host 'Run .\install.ps1 -CheckOnly for a read-only preflight.'
            Write-Host 'Run .\install.ps1 -InstallMissing to proceed non-interactively after approval.'
            Write-Host ''
            exit 0
        }
        $allowInstallMissing = $true
    } else {
        Write-Host '-InstallMissing supplied: approved prerequisite installs may proceed without a prompt.'
        Write-Host ''
    }
}

Write-Host ''
Write-Host 'LOCAL CODE AGENT'
Write-Host ''
Write-Host $(if ($CheckOnly) { 'Checking this machine without making setup changes...' } else { 'Preparing and validating the local runtime...' })
Write-Host ''

$forward = @{ RuntimeRoot = $RuntimeRoot }
if ($CheckOnly) { $forward.CheckOnly = $true }
if ($allowInstallMissing) { $forward.InstallMissing = $true }
if ($AttemptWslInstall) { $forward.AttemptWslInstall = $true }
if ($OpenDriverPage) { $forward.OpenDriverPage = $true }

try {
    & $entry @forward
    $rc = $LASTEXITCODE
} catch {
    Write-Host ''
    Write-Host 'Setup stopped before the machine was ready.'
    Write-Host ("  {0}" -f $_.Exception.Message)
    Write-Host ''
    Write-Host 'If company policy blocked a WinGet package, ask IT to approve or install the'
    Write-Host 'package named above, then run .\install.ps1 -CheckOnly and retry setup.'
    Write-Host ''
    exit 2
}

if ($rc -ne 0) {
    Write-Host ''
    Write-Host 'Setup did not complete successfully.'
    Write-Host 'If company policy blocked a WinGet package, ask IT to approve or install the'
    Write-Host 'package named in the failure above, then run .\install.ps1 -CheckOnly.'
    Write-Host ''
    exit $rc
}

# The bootstrap historically proved only `import local_agent`, which cannot detect a
# checkout whose newly-declared runtime dependency is absent from an older venv.  Validate
# the same managed interpreter used by the public launcher against this checkout before
# either setup or -CheckOnly is allowed to report readiness.
if (-not (Test-Path $managedPython)) {
    Write-Host ''
    Write-Host 'Runtime integrity check failed: the managed checkout environment is missing.'
    Write-Host ("Expected interpreter: {0}" -f $managedPython)
    Write-Host $(if ($CheckOnly) { 'Run .\install.ps1 to create/update it.' } else { 'Setup did not produce the required managed environment.' })
    Write-Host ''
    exit 2
}

$previousPythonPath = if (Test-Path Env:PYTHONPATH) { $env:PYTHONPATH } else { $null }
$internal = Join-Path $root 'internal'
$env:PYTHONPATH = if ($previousPythonPath) { "$internal;$previousPythonPath" } else { $internal }
try {
    & $managedPython $runtimePreflight --pyproject $pyproject
    $runtimeRc = $LASTEXITCODE
} finally {
    if ($null -eq $previousPythonPath) { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }
    else { $env:PYTHONPATH = $previousPythonPath }
}
if ($runtimeRc -ne 0) {
    Write-Host ''
    Write-Host 'The managed Python environment does not satisfy this checkout.'
    Write-Host $(if ($CheckOnly) { 'Run .\install.ps1 to update it.' } else { 'Setup completed its earlier stages but runtime validation failed.' })
    Write-Host ''
    exit 2
}

if (-not $CheckOnly) {
    Write-Host ''
    Write-Host 'Next:'
    Write-Host '  .\local-code-agent.ps1'
    Write-Host '  .\local-code-agent.ps1 chat qwen3-8b-npu'
    Write-Host '  .\local-code-agent.ps1 capabilities'
    Write-Host ''
}
exit 0
