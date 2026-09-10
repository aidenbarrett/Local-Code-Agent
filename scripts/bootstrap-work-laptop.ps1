[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [switch]$InstallMissing,
    [switch]$SkipOvms,
    [switch]$AttemptWslInstall,
    [switch]$OpenDriverPage,
    [string]$RuntimeRoot = "$env:LOCALAPPDATA\LocalCodeAgent"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# Local Code Agent - Windows work-laptop bootstrap
# Windows-first, WSL optional, safe for managed/corporate laptops.
# Runtime files stay outside the Git checkout.

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvDir = Join-Path $RepoRoot ".venv-workstation"
$ToolDir = Join-Path $RuntimeRoot "tools"
$ModelDir = Join-Path $RuntimeRoot "models"
$CacheDir = Join-Path $RuntimeRoot "cache"
$LogDir = Join-Path $RuntimeRoot "logs"
$ReportDir = Join-Path $RuntimeRoot "reports"
$OvmsDir = Join-Path $ToolDir "ovms-2026.3.0"

$OpenVinoVersion = "2026.3.0"
$OpenVinoTokenizersVersion = "2026.3.0.0"
$OpenVinoGenAiVersion = "2026.3.0.0"
$OvmsUrl = "https://github.com/openvinotoolkit/model_server/releases/download/v2026.3/ovms_windows_2026.3.0_python_on.zip"
$OvmsSha256Url = "$OvmsUrl.sha256"
$OvmsExpectedSha256 = "e83ecc5dc47af390567b03c8ad1bf109ea6cdd88ef374ee7933fc303459b3ced"
$IntelNpuDriverUrl = "https://www.intel.com/content/www/us/en/download/794734/intel-npu-driver-windows.html"
$OpenVinoPipUrl = "https://docs.openvino.ai/2026/get-started/install-openvino/install-openvino-pip.html"
$OpenVinoNpuUrl = "https://docs.openvino.ai/2026/openvino-workflow-generative/inference-with-genai/inference-with-genai-on-npu.html"
$OvmsDocsUrl = "https://docs.openvino.ai/2026/model-server/ovms_docs_deploying_server_baremetal.html"
$WslUrl = "https://learn.microsoft.com/windows/wsl/install"
$PythonUrl = "https://www.python.org/downloads/windows/"
$VcRedistUrl = "https://aka.ms/vc14/vc_redist.x64.exe"

$Results = New-Object System.Collections.Generic.List[object]

function Section([string]$Title) {
    Write-Host ""
    Write-Host ("=" * 76)
    Write-Host $Title
    Write-Host ("=" * 76)
}

function Result([string]$Check,[string]$Status,[string]$Detail,[string]$Action="") {
    $Results.Add([pscustomobject]@{Check=$Check;Status=$Status;Detail=$Detail;Action=$Action})
    Write-Host ("[{0}] {1,-26} {2}" -f $Status,$Check,$Detail)
    if ($Action) { Write-Host ("       Action: {0}" -f $Action) }
}

function Has([string]$Name) { return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue) }
function EnsureDir([string]$Path) { if (!(Test-Path $Path)) { New-Item -ItemType Directory -Force -Path $Path | Out-Null } }

function RefreshPath {
    $env:Path = ([Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User"))
}

function InstallWinget([string]$Id) {
    if (!(Has "winget")) { throw "WinGet is unavailable. Install manually or ask IT." }
    & winget install --id $Id -e --source winget --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "winget install failed for $Id ($LASTEXITCODE)" }
    RefreshPath
}

function FindPython {
    foreach ($cmd in @("py","python","python3")) {
        if (!(Has $cmd)) { continue }
        try {
            if ($cmd -eq "py") {
                $v = & py -3.12 -c "import sys;print('.'.join(map(str,sys.version_info[:3])))" 2>$null
                if ($LASTEXITCODE -eq 0 -and $v) { return @{Exe="py";Prefix=@("-3.12");Version=$v.Trim()} }
                $v = & py -3 -c "import sys;print('.'.join(map(str,sys.version_info[:3])))" 2>$null
                if ($LASTEXITCODE -eq 0 -and $v) { return @{Exe="py";Prefix=@("-3");Version=$v.Trim()} }
            } else {
                $v = & $cmd -c "import sys;print('.'.join(map(str,sys.version_info[:3])))" 2>$null
                if ($LASTEXITCODE -eq 0 -and $v) { return @{Exe=$cmd;Prefix=@();Version=$v.Trim()} }
            }
        } catch {}
    }
    return $null
}

function PythonOk($py) {
    if (!$py) { return $false }
    $p = $py.Version.Split('.')
    return ([int]$p[0] -gt 3) -or (([int]$p[0] -eq 3) -and ([int]$p[1] -ge 11))
}

function RunPython($py,[string[]]$Args) {
    & $py.Exe @($py.Prefix) @Args
    if ($LASTEXITCODE -ne 0) { throw "Python command failed ($LASTEXITCODE)" }
}

function CheckUrl([string]$Name,[string]$Url) {
    try {
        $r = Invoke-WebRequest -Uri $Url -Method Head -MaximumRedirection 8 -UseBasicParsing -TimeoutSec 20
        Result "URL $Name" "PASS" ("HTTP {0}" -f [int]$r.StatusCode)
    } catch {
        try {
            $r = Invoke-WebRequest -Uri $Url -Method Get -Headers @{Range="bytes=0-0"} -MaximumRedirection 8 -UseBasicParsing -TimeoutSec 20
            Result "URL $Name" "PASS" ("reachable, HTTP {0}" -f [int]$r.StatusCode)
        } catch {
            Result "URL $Name" "WARN" $_.Exception.Message "Check proxy/VPN/firewall or open manually."
        }
    }
}

Section "Local Code Agent work-laptop bootstrap"
Write-Host "Repo root:    $RepoRoot"
Write-Host "Runtime root: $RuntimeRoot"
Write-Host "Mode:         $(if ($CheckOnly) {'CHECK ONLY'} else {'SETUP'})"
Write-Host "Install prereqs: $(if ($InstallMissing) {'YES (explicit)'} else {'NO'})"
Write-Host ""
Write-Host "NPU drivers and Windows features are never changed silently."

Section "1. Host and basic tooling"
$os = Get-CimInstance Win32_OperatingSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$cs = Get-CimInstance Win32_ComputerSystem
Result "Windows" $(if ($os.Caption -match "Windows 11") {"PASS"} else {"WARN"}) "$($os.Caption) build $($os.BuildNumber)"
Result "Architecture" $(if ([Environment]::Is64BitOperatingSystem) {"PASS"} else {"FAIL"}) $env:PROCESSOR_ARCHITECTURE
Result "CPU" "INFO" $cpu.Name
Result "Machine" "INFO" "$($cs.Manufacturer) $($cs.Model)"

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Result "Administrator" "INFO" $(if ($isAdmin) {"yes"} else {"no"}) "Normal setup is user-space; driver/WSL changes may require IT/admin."
Result "WinGet" $(if (Has "winget") {"PASS"} else {"WARN"}) $(if (Has "winget") {((& winget --version) | Select-Object -First 1)} else {"not found"})

if (Has "git") { Result "Git" "PASS" ((& git --version) -join " ") }
elseif ($CheckOnly -or !$InstallMissing) { Result "Git" "WARN" "not found" "Rerun with -InstallMissing or install approved Git manually (WinGet id: Git.Git)." }
else {
    try { InstallWinget "Git.Git"; Result "Git" "PASS" ((& git --version) -join " ") }
    catch { Result "Git" "WARN" "automatic install failed" $_.Exception.Message }
}

$py = FindPython
if (PythonOk $py) { Result "Python" "PASS" "$($py.Version) via $($py.Exe)" }
else {
    Result "Python" "WARN" $(if ($py) {"$($py.Version) is too old"} else {"not found"}) "Need Python >=3.11; target 3.12."
    if (!$CheckOnly -and $InstallMissing) {
        try { InstallWinget "Python.Python.3.12"; $py = FindPython }
        catch { Result "Python install" "WARN" "automatic install failed" "$($_.Exception.Message) Manual: $PythonUrl" }
    }
}
if (!(PythonOk $py)) { Result "Python usable" "FAIL" "Python >=3.11 unavailable" "Install Python 3.12 and rerun." }

Section "2. WSL status (optional)"
$wslUsable = $false
if (Has "wsl.exe") {
    Result "WSL command" "PASS" "wsl.exe present"
    try {
        $status = (& wsl.exe --status 2>&1) -join " | "
        Result "WSL status" $(if ($LASTEXITCODE -eq 0) {"PASS"} else {"WARN"}) $status
        $distros = @(& wsl.exe --list --quiet 2>$null | ForEach-Object { $_.Trim([char]0).Trim() } | Where-Object { $_ })
        if ($distros.Count -gt 0) {
            Result "WSL distros" "PASS" ($distros -join ", ")
            & wsl.exe -e sh -lc "printf WSL_OK" *> $null
            if ($LASTEXITCODE -eq 0) { $wslUsable=$true; Result "WSL execution" "PASS" "Linux command execution works" }
            else { Result "WSL execution" "WARN" "distro exists but execution failed" }
        } else { Result "WSL distros" "INFO" "none installed" "WSL is not required for Windows-native NPU bring-up." }
    } catch { Result "WSL" "WARN" $_.Exception.Message "Windows-native path remains valid." }
} else { Result "WSL" "INFO" "not installed" "Optional. Official docs: $WslUrl" }

if ($AttemptWslInstall -and !$wslUsable) {
    if (!$isAdmin) { Result "WSL install" "WARN" "requested but shell is not elevated" "Use approved elevated PowerShell or ask IT." }
    else {
        Write-Host "Explicit -AttemptWslInstall supplied; invoking Microsoft's documented 'wsl --install'."
        & wsl.exe --install
        Result "WSL install" $(if ($LASTEXITCODE -eq 0) {"PASS"} else {"WARN"}) "exit $LASTEXITCODE; reboot may be required"
    }
}

Section "3. Intel NPU and driver"
$npuDevices = @()
try { $npuDevices = @(Get-PnpDevice -PresentOnly | Where-Object { $_.FriendlyName -match "NPU|Neural Processing|AI Boost" }) }
catch { Result "PnP query" "WARN" "Get-PnpDevice failed" $_.Exception.Message }

if ($npuDevices.Count -gt 0) {
    foreach ($d in $npuDevices) {
        Result "NPU device" "PASS" "$($d.FriendlyName) [$($d.Status)]"
        try {
            $drv = Get-CimInstance Win32_PnPSignedDriver | Where-Object { $_.DeviceID -eq $d.InstanceId } | Select-Object -First 1
            if ($drv) { Result "NPU driver" "INFO" "version $($drv.DriverVersion), provider $($drv.DriverProviderName)" }
        } catch {}
    }
} else {
    Result "NPU device" "WARN" "no present NPU matched" "Check Device Manager and Intel driver: $IntelNpuDriverUrl"
    if ($OpenDriverPage) { Start-Process $IntelNpuDriverUrl }
}

Section "4. Clean runtime layout"
foreach ($dir in @($RuntimeRoot,$ToolDir,$ModelDir,$CacheDir,$LogDir,$ReportDir)) {
    if (!$CheckOnly) { EnsureDir $dir }
    Result "Directory" "INFO" $dir
}

Section "5. Local Code Agent environment"
if (PythonOk $py) {
    $venvPython = Join-Path $VenvDir "Scripts\python.exe"
    if (!(Test-Path $venvPython)) {
        if ($CheckOnly) { Result "Agent venv" "INFO" "not created" "Run without -CheckOnly." }
        else { RunPython $py @("-m","venv",$VenvDir); Result "Agent venv" "PASS" $VenvDir }
    } else { Result "Agent venv" "PASS" $VenvDir }

    if (Test-Path $venvPython) {
        if (!$CheckOnly) {
            & $venvPython -m pip install --upgrade pip
            if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
            # Editable install is deliberate: current wheels do not package top-level skills/ yet.
            & $venvPython -m pip install -e "${RepoRoot}[dev]"
            if ($LASTEXITCODE -ne 0) { throw "editable project install failed" }
        }
        & $venvPython -c "import local_agent; print('import-ok')"
        Result "Agent import" $(if ($LASTEXITCODE -eq 0) {"PASS"} else {"FAIL"}) "local_agent"
    }
}

Section "6. OpenVINO and NPU visibility"
$venvPython = Join-Path $VenvDir "Scripts\python.exe"
if (Test-Path $venvPython) {
    if (!$CheckOnly) {
        & $venvPython -m pip install "openvino==$OpenVinoVersion" "openvino-tokenizers==$OpenVinoTokenizersVersion" "openvino-genai==$OpenVinoGenAiVersion"
        if ($LASTEXITCODE -ne 0) { throw "OpenVINO install failed" }
    }
    $ovVersion = (& $venvPython -c "import openvino as ov; print(ov.__version__)" 2>&1) -join " "
    Result "OpenVINO" $(if ($LASTEXITCODE -eq 0) {"PASS"} else {"FAIL"}) $ovVersion
    $devices = (& $venvPython -c "from openvino import Core; print(','.join(Core().available_devices))" 2>&1) -join ""
    if ($LASTEXITCODE -eq 0) {
        Result "OpenVINO devices" "INFO" $devices
        Result "OpenVINO NPU" $(if (($devices -split ',') -contains 'NPU') {"PASS"} else {"WARN"}) $(if (($devices -split ',') -contains 'NPU') {"NPU available"} else {"NPU not listed"}) "Check Intel NPU driver/device policy if absent."
    } else { Result "OpenVINO devices" "FAIL" $devices }
} else { Result "OpenVINO" "FAIL" "agent venv unavailable" }

Section "7. Microsoft VC++ runtime"
$vcFound = $false
foreach ($key in @("HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64","HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64")) {
    if (Test-Path $key) {
        $p = Get-ItemProperty $key
        if ($p.Installed -eq 1) { $vcFound=$true; Result "VC++ runtime" "PASS" "version $($p.Version)"; break }
    }
}
if (!$vcFound) { Result "VC++ runtime" "WARN" "not detected" "OVMS requires it. Official x64: $VcRedistUrl" }

Section "8. OpenVINO Model Server"
$ovmsReady = $false
if ($SkipOvms) {
    Result "OVMS" "INFO" "skipped by -SkipOvms"
} elseif (Test-Path $OvmsDir) {
    $existingExe = Get-ChildItem $OvmsDir -Recurse -Filter ovms.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    $existingSetupVars = Get-ChildItem $OvmsDir -Recurse -Filter setupvars.ps1 -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($existingExe -and $existingSetupVars) {
        $ovmsReady = $true
        Result "OVMS" "PASS" $OvmsDir
    } else {
        Result "OVMS" "WARN" "directory exists but installation is incomplete" "Remove $OvmsDir and rerun setup."
    }
}

if (!$SkipOvms -and !$ovmsReady -and !(Test-Path $OvmsDir)) {
    if ($CheckOnly) {
        Result "OVMS" "INFO" "not installed" "Run without -CheckOnly to fetch the pinned official Windows package."
    } else {
        EnsureDir $ToolDir
        $zip = Join-Path $ToolDir "ovms_windows_2026.3.0_python_on.zip"
        $stage = Join-Path $ToolDir "ovms-2026.3.0.staging"
        Invoke-WebRequest -Uri $OvmsUrl -OutFile $zip -MaximumRedirection 8 -UseBasicParsing
        $actualSha = (Get-FileHash -Algorithm SHA256 $zip).Hash.ToLowerInvariant()
        if ($actualSha -ne $OvmsExpectedSha256) {
            throw "OVMS SHA256 mismatch. Expected $OvmsExpectedSha256, got $actualSha. Archive left at $zip for inspection."
        }
        Result "OVMS archive hash" "PASS" $actualSha
        if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
        EnsureDir $stage
        Expand-Archive -Path $zip -DestinationPath $stage -Force
        Move-Item $stage $OvmsDir
        $ovmsReady = $true
        Result "OVMS" "PASS" $OvmsDir
    }
}

if (!$SkipOvms -and (Test-Path $OvmsDir)) {
    $ovmsExe = Get-ChildItem $OvmsDir -Recurse -Filter ovms.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    $setupVars = Get-ChildItem $OvmsDir -Recurse -Filter setupvars.ps1 -ErrorAction SilentlyContinue | Select-Object -First 1
    Result "OVMS executable" $(if ($ovmsExe) {"PASS"} else {"WARN"}) $(if ($ovmsExe) {$ovmsExe.FullName} else {"ovms.exe not found"})
    Result "OVMS setupvars" $(if ($setupVars) {"PASS"} else {"WARN"}) $(if ($setupVars) {$setupVars.FullName} else {"setupvars.ps1 not found"})
}

Section "9. Verify official upstream endpoints from this laptop"
CheckUrl "Intel NPU driver" $IntelNpuDriverUrl
CheckUrl "OpenVINO install docs" $OpenVinoPipUrl
CheckUrl "OpenVINO NPU docs" $OpenVinoNpuUrl
CheckUrl "OVMS docs" $OvmsDocsUrl
CheckUrl "OVMS archive" $OvmsUrl
CheckUrl "OVMS SHA256" $OvmsSha256Url
CheckUrl "VC++ x64" $VcRedistUrl
CheckUrl "WSL docs" $WslUrl
CheckUrl "Python Windows" $PythonUrl

Section "10. Final report"
if (!$CheckOnly) { EnsureDir $ReportDir }
$report = [pscustomobject]@{
    generated_at=(Get-Date).ToString("o")
    repo_root=$RepoRoot
    runtime_root=$RuntimeRoot
    computer=$env:COMPUTERNAME
    os=$os.Caption
    os_build=$os.BuildNumber
    cpu=$cpu.Name
    wsl_usable=$wslUsable
    install_missing=$InstallMissing.IsPresent
    versions=@{openvino=$OpenVinoVersion;openvino_tokenizers=$OpenVinoTokenizersVersion;openvino_genai=$OpenVinoGenAiVersion;ovms="2026.3.0"}
    official_sources=@{intel_npu_driver=$IntelNpuDriverUrl;openvino_pip=$OpenVinoPipUrl;openvino_npu=$OpenVinoNpuUrl;ovms_windows=$OvmsDocsUrl;ovms_archive=$OvmsUrl;ovms_sha256=$OvmsSha256Url;wsl=$WslUrl;python=$PythonUrl;vc_redist_x64=$VcRedistUrl}
    results=@($Results)
}
if (!$CheckOnly) {
    $reportPath = Join-Path $ReportDir ("work-laptop-bootstrap-{0}.json" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
    $report | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 $reportPath
    Write-Host "Report: $reportPath"
}

$fail = @($Results | Where-Object Status -eq "FAIL").Count
$warn = @($Results | Where-Object Status -eq "WARN").Count
$pass = @($Results | Where-Object Status -eq "PASS").Count
Write-Host "PASS: $pass  WARN: $warn  FAIL: $fail"
if ($fail -eq 0) { Write-Host "Ready for NPU runtime qualification."; exit 0 }
Write-Host "Blocking failures remain. Fix FAIL items and rerun."; exit 2
