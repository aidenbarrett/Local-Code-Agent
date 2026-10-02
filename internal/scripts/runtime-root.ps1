# Where runtime state and model weights live, for PowerShell entry points.
#
# This mirrors serving/model_store.py default_runtime_root() exactly: LCA_RUNTIME_ROOT,
# then LOCALAPPDATA\LocalCodeAgent, then <home>\LocalCodeAgent. Setup runs before any
# Python environment exists, so it cannot ask the Python owner; a test runs both under
# the same environment and requires the same answer. Dot-source this file; it defines
# one function and changes nothing.
function Resolve-LcaRuntimeRoot {
    if ($env:LCA_RUNTIME_ROOT) { return $env:LCA_RUNTIME_ROOT }
    $base = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { $HOME }
    return (Join-Path $base 'LocalCodeAgent')
}
