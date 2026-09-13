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

# Canonical work-laptop entry point.
# From an already-cloned repo, this drives machine bootstrap -> model pull ->
# NPU serving -> Local Code Agent protocol qualification -> C++ fixture smoke.
# It intentionally stops before any scored evaluation run.

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BaseBootstrap = Join-Path $PSScriptRoot "bootstrap-work-laptop.ps1"
$Controller = Join-Path $RepoRoot "measurement\serve.py"
$Profile = "ptl-npu-8b"
$VenvPython = Join-Path $RepoRoot ".venv-workstation\Scripts\python.exe"
$ToolDir = Join-Path $RuntimeRoot "tools"
$ModelDir = Join-Path $RuntimeRoot "models"
$CacheDir = Join-Path $RuntimeRoot "cache"
$LogDir = Join-Path $RuntimeRoot "logs"
$ReportDir = Join-Path $RuntimeRoot "reports"
$RuntimeDir = Join-Path $RuntimeRoot "runtime"
$PidDir = Join-Path $RuntimeDir "pid"
$ProfileDir = Join-Path $RuntimeDir "profiles"
$OvmsDir = Join-Path $ToolDir "ovms-2026.3.0"
$RuntimeProfile = Join-Path $ProfileDir "ptl-npu-8b.json"
$QualificationJson = Join-Path $ReportDir "qualification-ptl-npu-8b.json"
$QualificationDump = Join-Path $ReportDir "qualification-ptl-npu-8b-failures"
$ModelManifest = Join-Path $ReportDir "model-qwen3-8b-int4-cw-ov-manifest.json"
$ReportPath = Join-Path $ReportDir ("work-laptop-ready-{0}.json" -f (Get-Date -Format "yyyyMMdd-HHmmss"))

function Say([string]$Text) { Write-Host "[one-shot] $Text" }
function Fail([string]$Text) { Write-Host "[one-shot] FAIL: $Text"; exit 2 }
function EnsureDir([string]$Path) { if (!(Test-Path $Path)) { New-Item -ItemType Directory -Force -Path $Path | Out-Null } }
function Has([string]$Name) { return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue) }
function RefreshPath { $env:Path = ([Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")) }

function ImportVsDevEnvironment {
    $roots = @()
    if (${env:ProgramFiles(x86)}) { $roots += (Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe") }
    if ($env:ProgramFiles) { $roots += (Join-Path $env:ProgramFiles "Microsoft Visual Studio\Installer\vswhere.exe") }
    $vswhere = $roots | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (!$vswhere) { return $false }
    $install = (& $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null | Select-Object -First 1)
    if (!$install) { return $false }
    $devcmd = Join-Path $install "Common7\Tools\VsDevCmd.bat"
    if (!(Test-Path $devcmd)) { return $false }
    $lines = & cmd.exe /d /s /c "`"$devcmd`" -no_logo -arch=x64 -host_arch=x64 && set"
    if ($LASTEXITCODE -ne 0) { return $false }
    foreach ($line in $lines) {
        $i = $line.IndexOf('=')
        if ($i -gt 0) { Set-Item -Path ("Env:" + $line.Substring(0,$i)) -Value $line.Substring($i+1) }
    }
    return $true
}

function FindCompiler {
    foreach ($c in @("cl.exe","clang-cl.exe","clang++.exe","g++.exe")) { if (Has $c) { return $c } }
    if (ImportVsDevEnvironment) {
        foreach ($c in @("cl.exe","clang-cl.exe","clang++.exe","g++.exe")) { if (Has $c) { return $c } }
    }
    return $null
}

function InstallBuildTools {
    if (!(Has "winget")) { throw "WinGet unavailable" }
    & winget install --id Microsoft.VisualStudio.2022.BuildTools -e --source winget --accept-package-agreements --accept-source-agreements --override "--quiet --wait --norestart --nocache --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
    if ($LASTEXITCODE -ne 0) { throw "Build Tools install failed ($LASTEXITCODE)" }
    RefreshPath
}

function WriteModelManifest([string]$Root) {
    Say "Hashing local model payload for provenance..."
    $files = @(Get-ChildItem $Root -Recurse -File | Sort-Object FullName | ForEach-Object {
        [pscustomobject]@{
            path=$_.FullName.Substring($Root.Length).TrimStart('\')
            bytes=$_.Length
            sha256=(Get-FileHash -Algorithm SHA256 $_.FullName).Hash.ToLowerInvariant()
        }
    })
    [pscustomobject]@{generated_at=(Get-Date).ToString("o");source_model=$ModelId;root=$Root;files=$files} |
        ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 $ModelManifest
}

Say "repo: $RepoRoot"
Say "runtime: $RuntimeRoot"

if (!(Test-Path $BaseBootstrap)) { Fail "missing $BaseBootstrap" }
if (!(Test-Path $Controller)) { Fail "missing $Controller" }

# Stage 1 owns Windows/Python/OpenVINO/NPU/OVMS inventory and user-space setup.
$baseArgs = @("-RuntimeRoot",$RuntimeRoot)
if ($CheckOnly) { $baseArgs += "-CheckOnly" }
if ($InstallMissing) { $baseArgs += "-InstallMissing" }
if ($AttemptWslInstall) { $baseArgs += "-AttemptWslInstall" }
if ($OpenDriverPage) { $baseArgs += "-OpenDriverPage" }
Say "running base machine/bootstrap stage"
& $BaseBootstrap @baseArgs
$baseExit = $LASTEXITCODE
if ($CheckOnly) {
    Say "preflight complete (base stage exit $baseExit). No model/server/evaluation state was created."
    exit $baseExit
}
if ($baseExit -ne 0) { Fail "base bootstrap returned $baseExit; fix its FAIL items and rerun" }

foreach ($d in @($ToolDir,$ModelDir,$CacheDir,$LogDir,$ReportDir,$RuntimeDir,$PidDir,$ProfileDir)) { EnsureDir $d }
if (!(Test-Path $VenvPython)) { Fail "repo venv missing after base bootstrap: $VenvPython" }

# Full experimental readiness also needs the C++ fixture toolchain.
if (!(Has "cmake") -and $InstallMissing) {
    Say "installing CMake because -InstallMissing was supplied"
    & winget install --id Kitware.CMake -e --source winget --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { Fail "CMake install failed ($LASTEXITCODE)" }
    RefreshPath
}
if (!(Has "cmake") -or !(Has "ctest")) { Fail "CMake/CTest missing. Rerun with -InstallMissing or install the approved package." }

$compiler = FindCompiler
if (!$compiler -and $InstallMissing) {
    Say "installing Visual Studio 2022 C++ Build Tools because -InstallMissing was supplied"
    InstallBuildTools
    $compiler = FindCompiler
}
if (!$compiler) { Fail "no C++ compiler available. Use -InstallMissing if approved, or enter an approved MSVC/Intel/LLVM environment and rerun." }
Say "C++ compiler: $compiler"

# Base bootstrap installs OVMS. Locate it and import its runtime environment.
$ovmsExe = Get-ChildItem $OvmsDir -Recurse -Filter ovms.exe -ErrorAction SilentlyContinue | Select-Object -First 1
$setupVars = Get-ChildItem $OvmsDir -Recurse -Filter setupvars.ps1 -ErrorAction SilentlyContinue | Select-Object -First 1
if (!$ovmsExe -or !$setupVars) { Fail "OVMS 2026.3.0 installation is incomplete under $OvmsDir" }
. $setupVars.FullName

# Serving configuration has exactly one owner: MODEL_PRESETS via serve.py.
# Existing virtual environments must also have the controller dependency.
& $VenvPython -c "import psutil"
if ($LASTEXITCODE -ne 0) { Fail 'Update the checkout environment: python -m pip install -e ".[dev]"' }
$serveArgs = @("--profile",$Profile,"--runtime-root",$RuntimeRoot,"--executable",$ovmsExe.FullName)
$planText = & $VenvPython $Controller start @serveArgs --dry-run
if ($LASTEXITCODE -ne 0) { Fail "could not resolve serving preset" }
$plan = ($planText -join "`n") | ConvertFrom-Json
$ModelId = $plan.model_configuration.model
$BaseUrl = $plan.base_url
$RestPort = $plan.port
$MaxPromptLen = $plan.model_configuration.server_max_prompt_length
$PidFile = $plan.state_file
$StdoutLog = $plan.stdout
$StderrLog = $plan.stderr
Say "target: $ModelId on $($plan.model_configuration.device), port $RestPort, max prompt $MaxPromptLen"
$modelRoot = $plan.model_dir
if (!(Test-Path (Join-Path $modelRoot "openvino_model.xml"))) {
    Say "pulling model after controller disk check"
    & $VenvPython $Controller pull @serveArgs
    if ($LASTEXITCODE -ne 0) { Fail "model pull failed" }
    $planText = & $VenvPython $Controller start @serveArgs --dry-run
    if ($LASTEXITCODE -ne 0) { Fail "could not resolve pulled model" }
    $plan = ($planText -join "`n") | ConvertFrom-Json
    $modelRoot = $plan.model_dir
}
WriteModelManifest $modelRoot
Say "starting server through controller; first NPU compile may take several minutes"
$runtimeText = & $VenvPython $Controller start @serveArgs --wait-seconds 900
if ($LASTEXITCODE -ne 0) { Fail "server refused or not ready. Inspect $StdoutLog and $StderrLog" }
$runtimeText | Set-Content -Encoding UTF8 $RuntimeProfile
Say "OVMS endpoint ready: $BaseUrl"

# Protocol qualification is plumbing, not a scored experiment.
EnsureDir $QualificationDump
Say "running Local Code Agent server qualification"
Push-Location $RepoRoot
try {
    & $VenvPython measurement\qualify_server.py --profile $Profile --base-url $BaseUrl --model $ModelId --context-probes "1000,4000,7000" --json $QualificationJson --dump-dir $QualificationDump
    $qualExit = $LASTEXITCODE
} finally { Pop-Location }
if ($qualExit -ne 0) { Fail "server qualification failed ($qualExit). See $QualificationJson and $QualificationDump" }

# Prove the exact local benchmark toolchain can configure/build/test before any
# scored row is allowed to exist.
$fixtureSource = Join-Path $RepoRoot "benchmark_fixture\cpp_project"
$fixtureSmoke = Join-Path $CacheDir "fixture-smoke"
if (Test-Path $fixtureSmoke) { Remove-Item $fixtureSmoke -Recurse -Force }
Copy-Item $fixtureSource $fixtureSmoke -Recurse
Say "smoke-testing C++ fixture"
Push-Location $fixtureSmoke
try {
    & cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug
    if ($LASTEXITCODE -ne 0) { Fail "fixture configure failed" }
    & cmake --build build --parallel 4
    if ($LASTEXITCODE -ne 0) { Fail "fixture build failed" }
    & ctest --test-dir build --output-on-failure
    if ($LASTEXITCODE -ne 0) { Fail "fixture test failed" }
} finally { Pop-Location }

$gitSha = (& git -C $RepoRoot rev-parse HEAD 2>$null | Select-Object -First 1)
[pscustomobject]@{
    generated_at=(Get-Date).ToString("o")
    repo_root=$RepoRoot
    repo_commit=$gitSha
    runtime_root=$RuntimeRoot
    model=$ModelId
    model_root=$modelRoot
    model_manifest=$ModelManifest
    endpoint=$BaseUrl
    profile="ptl-npu-8b"
    target_device="NPU"
    max_prompt_len=$MaxPromptLen
    ovms="2026.3.0"
    qualification=$QualificationJson
    fixture_smoke="pass"
    compiler=$compiler
    server_pid_file=$PidFile
    stdout_log=$StdoutLog
    stderr_log=$StderrLog
} | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 $ReportPath

Write-Host ""
Write-Host "============================================================"
Write-Host "WORKSTATION READY"
Write-Host "============================================================"
Write-Host "NPU:           PASS"
Write-Host "Model:         $ModelId"
Write-Host "OVMS:          PASS ($BaseUrl)"
Write-Host "Qualification: PASS"
Write-Host "C++ fixture:   PASS"
Write-Host "Report:        $ReportPath"
Write-Host ""
Write-Host "No scored evaluation was run. That boundary is deliberate."
Write-Host "The machine is now ready for an explicitly declared Generation 2 run."
Write-Host ""
Write-Host "Example single-arm smoke only (not automatically executed):"
Write-Host ('& "{0}" evaluation\run_evaluation.py --profile ptl-npu-8b --condition skill --workdir "{1}" --out "{2}"' -f $VenvPython,(Join-Path $CacheDir "eval-work"),(Join-Path $ReportDir "gen2-ptl-npu-8b-skill.json"))
exit 0
