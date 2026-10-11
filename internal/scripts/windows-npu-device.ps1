# Name matching is inventory evidence, not proof of device health or execution.
# Shared by bootstrap, serving driver inventory and physical acceptance capture.
function Test-LcaNpuDeviceName {
    param([AllowNull()][AllowEmptyString()][string]$Name)
    return [bool]($Name -match '\bNPU\b|\bNeural Processing\b|\bAI Boost\b')
}
