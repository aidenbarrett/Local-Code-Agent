# The MSVC C++ compiler that CMake's Visual Studio generator uses, for PowerShell entry
# points. Setup (bootstrap-work-laptop-core.ps1) and `local-code-agent.ps1 doctor` both
# dot-source this one owner. vswhere ships with every Visual Studio 2017+ installation,
# Build Tools included, and only reads its installation catalogue. Dot-sourcing defines
# one variable and one function and changes nothing.
$MsvcComponent = "Microsoft.VisualStudio.Component.VC.Tools.x86.x64"
function FindMsvc {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (!(Test-Path $vswhere)) { return $null }
    $found = @(& $vswhere -products * -latest -requires $MsvcComponent -property installationPath) |
        Where-Object { $_ } | Select-Object -First 1
    if ($found) { return ([string]$found).Trim() }
    return $null
}
