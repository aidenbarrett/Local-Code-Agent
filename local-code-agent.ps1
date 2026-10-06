<#
.SYNOPSIS
  Canonical user-facing entrypoint for Local Code Agent.

.DESCRIPTION
  Running this script with no command opens the Textual Session Hub.
  Raw model chat and lower-level automation/debug surfaces remain available as
  explicit subcommands behind the same product entrypoint.
#>
param(
    # Tab completes the product commands. The list must name exactly the commands the
    # switch below dispatches (a test enforces that); unknown words still reach the
    # switch, which prints help.
    [ArgumentCompleter({
        param($commandName, $parameterName, $wordToComplete)
        @('session', 'chat', 'help', 'capabilities', 'run-task', 'acceptance', 'models', 'verification-demo', 'advanced') |
            Where-Object { $_ -like "$wordToComplete*" } |
            ForEach-Object { [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_) }
    })]
    [string]$Command = 'session',
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$internal = Join-Path $root 'internal'
$startupClock = [System.Diagnostics.Stopwatch]::StartNew()

function Write-StartupTrace([string]$Stage, [string]$Detail = '') {
    # Opt-in diagnostics for the public-launcher acceptance boundary. Keep every
    # record single-line and bounded so a timed-out caller retains useful partial
    # evidence without changing normal product output.
    if ($env:LCA_STARTUP_TRACE -ne '1') { return }
    $safeDetail = ($Detail -replace '[\r\n]+', ' ')
    if ($safeDetail.Length -gt 500) { $safeDetail = $safeDetail.Substring(0, 500) }
    [Console]::Error.WriteLine(
        ('[lca-startup] elapsed_ms={0} stage={1} detail={2}' -f
            $startupClock.ElapsedMilliseconds, $Stage, $safeDetail)
    )
}

Write-StartupTrace 'powershell-ready'

$python = @(
    (Join-Path $root '.venv-workstation\Scripts\python.exe'),
    (Join-Path $root '.venv\Scripts\python.exe')
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $python) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) { $python = $pythonCommand.Source }
}
if (-not $python) {
    Write-Host ''
    Write-Host 'No Python environment found.'
    Write-Host 'Run .\install.ps1 first, or install Python 3.11+.'
    Write-Host ''
    exit 2
}
Write-StartupTrace 'interpreter-selected' $python

# The public product must run from the interpreter and source tree selected above, not
# from ambient workstation Python configuration. PYTHONHOME can make even a valid venv
# resolve the wrong stdlib, while inherited PYTHONPATH entries can import unrelated
# packages from the user's shell. OVMS-specific Python state is captured separately by
# Set-ManagedOvmsEnvironment for the owned server process.
Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
$env:PYTHONPATH = $internal

# Validate the exact interpreter selected above before any public command can import
# product code, start OVMS, or prepare a model. Startup is diagnostic-only: it never
# runs pip or reaches the network to repair a stale checkout environment.
$preflight = Join-Path $internal 'scripts\runtime-preflight.py'
$pyproject = Join-Path $root 'pyproject.toml'
if (-not (Test-Path $preflight) -or -not (Test-Path $pyproject)) {
    Write-Host ''
    Write-Host 'Local Code Agent checkout is incomplete.'
    Write-Host 'Run git status, restore the missing product files, then run .\install.ps1.'
    Write-Host ''
    exit 2
}

Write-StartupTrace 'preflight-start' $preflight
& $python $preflight --pyproject $pyproject
Write-StartupTrace 'preflight-finished' ("exit={0}" -f $LASTEXITCODE)
if ($LASTEXITCODE -ne 0) {
    Write-Host ''
    Write-Host 'Local Code Agent will not start with this Python environment.'
    Write-Host ("Selected interpreter: {0}" -f $python)
    Write-Host 'Run .\install.ps1 to update the checkout environment, then retry.'
    Write-Host ''
    exit 2
}

function Show-Help {
    Write-StartupTrace 'help-start'
    & $python (Join-Path $internal 'scripts\product-help.py')
    Write-StartupTrace 'help-finished' ("exit={0}" -f $LASTEXITCODE)
}

function Set-ManagedOvmsEnvironment {
    # Preserve the old direct-chat wrapper's managed OVMS setup while keeping one
    # product launcher. setupvars.ps1 may set PYTHONHOME/PYTHONPATH for OVMS; capture
    # those for the child server and restore the controller Python environment.
    # The same location setup used: LCA_RUNTIME_ROOT when the user set it, otherwise the
    # default. Exported so every child resolves the identical root.
    . (Join-Path $internal 'scripts\runtime-root.ps1')
    $runtimeRoot = Resolve-LcaRuntimeRoot
    $env:LCA_RUNTIME_ROOT = $runtimeRoot

    $ovmsDir = Join-Path $runtimeRoot 'tools\ovms-2026.3.0'
    $ovmsExe = Get-ChildItem $ovmsDir -Recurse -Filter ovms.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    $setupVars = Get-ChildItem $ovmsDir -Recurse -Filter setupvars.ps1 -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $ovmsExe -or -not $setupVars) { return }

    $pythonPathBeforeSetup = $env:PYTHONPATH

    . $setupVars.FullName
    if (Test-Path Env:PYTHONHOME) { $env:LCA_OVMS_PYTHONHOME = $env:PYTHONHOME }
    else { Remove-Item Env:LCA_OVMS_PYTHONHOME -ErrorAction SilentlyContinue }
    if (Test-Path Env:PYTHONPATH) { $env:LCA_OVMS_PYTHONPATH = $env:PYTHONPATH }
    else { Remove-Item Env:LCA_OVMS_PYTHONPATH -ErrorAction SilentlyContinue }

    # Never let runtime setupvars alter the controller interpreter contract.
    Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
    $env:PYTHONPATH = $pythonPathBeforeSetup
    $env:LCA_OVMS_EXECUTABLE = $ovmsExe.FullName
}

switch ($Command.ToLowerInvariant()) {
    'session' {
        Set-ManagedOvmsEnvironment
        & $python (Join-Path $internal 'scripts\session-hub.py') @Rest
        exit $LASTEXITCODE
    }
    'chat' {
        if (-not $Rest -or $Rest.Count -eq 0) { $Rest = @('list') }
        if ($Rest[0] -ne 'list') { Set-ManagedOvmsEnvironment }
        & $python (Join-Path $internal 'scripts\chat.py') @Rest
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

        # run-task prepares the selected model's managed server itself.
        Set-ManagedOvmsEnvironment
        & $python (Join-Path $internal 'scripts\run-task-ui.py') @Rest
        exit $LASTEXITCODE
    }
    'acceptance' {
        # The Session Hub acceptance journeys, one run, every log kept in --output.
        Set-ManagedOvmsEnvironment
        & $python (Join-Path $internal 'scripts\acceptance-journeys.py') @Rest
        exit $LASTEXITCODE
    }
    'models' {
        # Which presets have weights downloaded; `models pull <profile>` fetches one.
        Set-ManagedOvmsEnvironment
        & $python (Join-Path $internal 'scripts\model_weights.py') @Rest
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
