[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [switch]$InstallMissing,
    [switch]$AttemptWslInstall,
    [switch]$OpenDriverPage,
    [string]$RuntimeRoot = "$env:LOCALAPPDATA\LocalCodeAgent"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BaseBootstrap = Join-Path $PSScriptRoot "bootstrap-work-laptop.ps1"
$Controller = Join-Path $PSScriptRoot "serving\serve.py"
$QualificationScript = Join-Path $PSScriptRoot "serving\qualify_server.py"
$Profile = "ptl-npu-8b"
$VenvPython = Join-Path $RepoRoot ".venv-workstation\Scripts\python.exe"
$ToolDir = Join-Path $RuntimeRoot "tools"
$CacheDir = Join-Path $RuntimeRoot "cache"
$ReportDir = Join-Path $RuntimeRoot "reports"
$OvmsDir = Join-Path $ToolDir "ovms-2026.3.0"
$QualificationJson = Join-Path $ReportDir "qualification-ptl-npu-8b.json"
$ReportPath = Join-Path $ReportDir ("work-laptop-ready-{0}.json" -f (Get-Date -Format "yyyyMMdd-HHmmss"))

function Say([string]$Text) { Write-Host "[one-shot] $Text" }
function Fail([string]$Text) { Write-Host "[one-shot] FAIL: $Text"; exit 2 }
function EnsureDir([string]$Path) { if (!(Test-Path $Path)) { New-Item -ItemType Directory -Force -Path $Path | Out-Null } }

function RestoreEnvVariable([string]$Name,[bool]$Existed,[string]$Value) {
    if ($Existed) { Set-Item -Path ("Env:" + $Name) -Value $Value }
    else { Remove-Item -Path ("Env:" + $Name) -ErrorAction SilentlyContinue }
}

Say "repo: $RepoRoot"
Say "runtime: $RuntimeRoot"

foreach ($required in @($BaseBootstrap,$Controller,$QualificationScript)) {
    if (!(Test-Path $required)) { Fail "missing $required" }
}

$baseArgs = @("-RuntimeRoot",$RuntimeRoot)
if ($CheckOnly) { $baseArgs += "-CheckOnly" }
if ($InstallMissing) { $baseArgs += "-InstallMissing" }
if ($AttemptWslInstall) { $baseArgs += "-AttemptWslInstall" }
if ($OpenDriverPage) { $baseArgs += "-OpenDriverPage" }

Say "running base machine/bootstrap stage"
& $BaseBootstrap @baseArgs
$baseExit = $LASTEXITCODE
if ($CheckOnly) {
    Say "preflight complete (base stage exit $baseExit). No model/server state was created."
    exit $baseExit
}
if ($baseExit -ne 0) { Fail "base bootstrap returned $baseExit; fix its FAIL items and rerun" }
if (!(Test-Path $VenvPython)) { Fail "repo venv missing after base bootstrap: $VenvPython" }

EnsureDir $CacheDir
EnsureDir $ReportDir

$ovmsExe = Get-ChildItem $OvmsDir -Recurse -Filter ovms.exe -ErrorAction SilentlyContinue | Select-Object -First 1
$setupVars = Get-ChildItem $OvmsDir -Recurse -Filter setupvars.ps1 -ErrorAction SilentlyContinue | Select-Object -First 1
if (!$ovmsExe -or !$setupVars) { Fail "managed local runtime installation is incomplete under $OvmsDir" }

# The runtime setup script intentionally points PYTHONHOME/PYTHONPATH at its own
# bundled Python. Preserve those values for runtime children, then restore the
# checkout interpreter environment before invoking controller code.
$hadPythonHome = Test-Path Env:PYTHONHOME
$pythonHomeBefore = if ($hadPythonHome) { $env:PYTHONHOME } else { $null }
$hadPythonPath = Test-Path Env:PYTHONPATH
$pythonPathBefore = if ($hadPythonPath) { $env:PYTHONPATH } else { $null }
. $setupVars.FullName
if (Test-Path Env:PYTHONHOME) { $env:LCA_OVMS_PYTHONHOME = $env:PYTHONHOME }
else { Remove-Item Env:LCA_OVMS_PYTHONHOME -ErrorAction SilentlyContinue }
if (Test-Path Env:PYTHONPATH) { $env:LCA_OVMS_PYTHONPATH = $env:PYTHONPATH }
else { Remove-Item Env:LCA_OVMS_PYTHONPATH -ErrorAction SilentlyContinue }
RestoreEnvVariable "PYTHONHOME" $hadPythonHome $pythonHomeBefore
RestoreEnvVariable "PYTHONPATH" $hadPythonPath $pythonPathBefore

& $VenvPython -c "import psutil"
if ($LASTEXITCODE -ne 0) { Fail 'Update the checkout environment: python -m pip install -e ".[dev]"' }

$serveArgs = @("--profile",$Profile,"--runtime-root",$RuntimeRoot,"--executable",$ovmsExe.FullName)
$planText = & $VenvPython $Controller start @serveArgs --dry-run
if ($LASTEXITCODE -ne 0) { Fail "could not resolve serving profile" }
$plan = ($planText -join "`n") | ConvertFrom-Json
$ModelId = $plan.model_configuration.model
$BaseUrl = $plan.base_url
$modelRoot = $plan.model_dir

if (!(Test-Path (Join-Path $modelRoot "openvino_model.xml"))) {
    Say "pulling configured model after controller preflight"
    & $VenvPython $Controller pull @serveArgs
    if ($LASTEXITCODE -ne 0) { Fail "model pull failed" }
}

# Establish controller ownership before any stop/retry. An unowned endpoint is
# never killed by setup.
$statusText = & $VenvPython $Controller status @serveArgs
$statusExit = $LASTEXITCODE
if ($statusExit -ne 0) { Fail "could not establish managed server ownership before startup" }
$status = ($statusText -join "`n") | ConvertFrom-Json
$hadOwnedProcess = [bool]$status.process_alive

Say "starting configured local model server"
& $VenvPython $Controller start @serveArgs --wait-seconds 900
$startExit = $LASTEXITCODE
if ($startExit -ne 0 -and $hadOwnedProcess) {
    Say "existing controller-owned profile is stale or unhealthy; stopping only that owned process and retrying once"
    & $VenvPython $Controller stop @serveArgs
    if ($LASTEXITCODE -ne 0) { Fail "existing profile could not be proven/stopped as controller-owned" }
    & $VenvPython $Controller start @serveArgs --wait-seconds 900
    $startExit = $LASTEXITCODE
}
if ($startExit -ne 0) { Fail "managed model server did not become ready" }

Say "running protocol conformance checks"
& $VenvPython $QualificationScript --profile $Profile --base-url $BaseUrl --model $ModelId --context-probes "1000,4000,7000" --json $QualificationJson
if ($LASTEXITCODE -ne 0) { Fail "server qualification failed; see $QualificationJson" }

# Keep one real configure/build/test smoke so workstation readiness includes the
# toolchain the product will use.
$fixtureSource = Join-Path $PSScriptRoot "benchmark_fixture\cpp_project"
$fixtureSmoke = Join-Path $CacheDir "fixture-smoke"
if (Test-Path $fixtureSmoke) { Remove-Item $fixtureSmoke -Recurse -Force }
Copy-Item $fixtureSource $fixtureSmoke -Recurse
Say "smoke-testing C++ fixture"
Push-Location $fixtureSmoke
try {
    & cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug
    if ($LASTEXITCODE -ne 0) { Fail "fixture configure failed" }
    & cmake --build build --config Debug --parallel 4
    if ($LASTEXITCODE -ne 0) { Fail "fixture build failed" }
    & ctest --test-dir build -C Debug --output-on-failure
    if ($LASTEXITCODE -ne 0) { Fail "fixture test failed" }
} finally { Pop-Location }

$gitSha = (& git -C $RepoRoot rev-parse HEAD 2>$null | Select-Object -First 1)
[pscustomobject]@{
    generated_at=(Get-Date).ToString("o")
    repo_root=$RepoRoot
    repo_commit=$gitSha
    runtime_root=$RuntimeRoot
    model=$ModelId
    endpoint=$BaseUrl
    profile=$Profile
    qualification=$QualificationJson
    fixture_smoke="pass"
} | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 $ReportPath

Write-Host ""
Write-Host "WORKSTATION VALIDATION"
Write-Host ""
Write-Host "READY TO USE"
Write-Host "  Agent connection: local model endpoint qualified"
Write-Host "  C++ build/test: 4/4 tests passed"
Write-Host "  Report: $ReportPath"
Write-Host ""
Write-Host "Next:"
Write-Host "  .\local-code-agent.ps1"
Write-Host ""
exit 0
