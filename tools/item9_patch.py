from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one replacement target, found {count}")
    path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


# One persistence-boundary sanitizer shared by rows and manifests. It scans the
# entire structure, not named fields, so a future field cannot quietly reopen
# the leak.
p = ROOT / "local_agent" / "persistence.py"
if p.exists():
    raise SystemExit(f"{p} already exists")
p.write_text(
    r'''"""Privacy-safe persistence helpers for experiment evidence.

Persisted rows and manifests must not contain host-specific absolute filesystem
paths. They can expose usernames, machine layout and internal asset names while
adding no scientific value. Redaction happens at the persistence boundary over
the complete structure so newly-added fields inherit the same rule.
"""

from __future__ import annotations

import hashlib
import os
import re
from functools import wraps
from typing import Any, Callable, TypeVar

_F = TypeVar("_F", bound=Callable[..., Any])

# The patterns intentionally identify path prefixes rather than trying to parse
# every legal filesystem character. Stopping at whitespace is conservative for
# privacy: even a path containing spaces loses the absolute/user-specific root.
_ABSOLUTE_PATH = re.compile(
    r"(?P<unc>(?<!\\)\\\\[^\\/\s]+[\\/][^\s\"'<>|]+)"
    r"|(?P<drive>(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'<>|]+)"
    r"|(?P<posix>(?<![:/A-Za-z0-9])/(?!/)[^\s\"'<>|]+)"
)


def _token(path_text: str) -> str:
    digest = hashlib.sha256(path_text.encode("utf-8", errors="surrogatepass")).hexdigest()[:12]
    return f"<abs-path:{digest}>"


def redact_absolute_paths(text: str) -> str:
    """Replace absolute filesystem paths embedded anywhere in *text*."""
    return _ABSOLUTE_PATH.sub(lambda match: _token(match.group(0)), text)


def sanitize_for_persistence(value: Any) -> Any:
    """Recursively redact absolute paths from JSON-like evidence structures."""
    if isinstance(value, str):
        return redact_absolute_paths(value)
    if isinstance(value, os.PathLike):
        return redact_absolute_paths(os.fspath(value))
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            safe_key = sanitize_for_persistence(key) if isinstance(key, (str, os.PathLike)) else key
            if safe_key in out and safe_key != key:
                raise ValueError("path redaction caused a duplicate persistence key")
            out[safe_key] = sanitize_for_persistence(item)
        return out
    if isinstance(value, list):
        return [sanitize_for_persistence(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_for_persistence(item) for item in value)
    return value


def sanitized_result(fn: _F) -> _F:
    """Wrap a producer so every returned evidence structure is privacy-safe."""
    @wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        return sanitize_for_persistence(fn(*args, **kwargs))

    return wrapped  # type: ignore[return-value]
''',
    encoding="utf-8",
    newline="\n",
)

# run_case itself is part of outcome_contract_sha256. Do not alter its source:
# wrap it at the persistence boundary instead, so this remains a source-only
# identity move rather than an evaluator-contract change.
p = ROOT / "evaluation" / "run_evaluation.py"
replace_once(
    p,
    "from local_agent.provenance import package_identity  # noqa: E402\n",
    "from local_agent.provenance import package_identity  # noqa: E402\n"
    "from local_agent.persistence import sanitized_result  # noqa: E402\n",
)
replace_once(
    p,
    "\n\ndef build_ledger(rows: list[dict], tiered: bool = False) -> dict:\n",
    "\n\n# Privacy is a persistence concern, not an outcome-scoring concern. Keep the\n"
    "# byte-exact run_case contract frozen and sanitize the complete emitted row\n"
    "# after it returns, including error rows and fields added in the future.\n"
    "run_case = sanitized_result(run_case)\n"
    "\n\ndef build_ledger(rows: list[dict], tiered: bool = False) -> dict:\n",
)

# The pre-run manifest is another persistence boundary. Sanitizing the complete
# return value covers executable paths, CMake cache paths and configure output,
# including fields that may be added later.
p = ROOT / "measurement" / "capture_run_manifest.py"
replace_once(
    p,
    "from local_agent.provenance import package_identity  # noqa: E402\n",
    "from local_agent.provenance import package_identity  # noqa: E402\n"
    "from local_agent.persistence import sanitized_result  # noqa: E402\n",
)
replace_once(
    p,
    "\n\ndef write_atomic(path: Path, payload: dict[str, Any]) -> None:\n",
    "\n\n# Persist only the privacy-safe projection. This is deliberately structural,\n"
    "# rather than a list of known path-bearing fields.\n"
    "capture_manifest = sanitized_result(capture_manifest)\n"
    "\n\ndef write_atomic(path: Path, payload: dict[str, Any]) -> None:\n",
)

# The bootstrap report is operator evidence too. Keep useful host/tool status,
# but do not persist the machine name, absolute roots or free-form details that
# can contain paths. Interactive console output remains unchanged.
p = ROOT / "scripts" / "bootstrap-work-laptop.ps1"
replace_once(
    p,
    '''$report = [pscustomobject]@{
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
''',
    '''$report = [pscustomobject]@{
    generated_at=(Get-Date).ToString("o")
    repo_label=(Split-Path -Leaf $RepoRoot)
    runtime_label=(Split-Path -Leaf $RuntimeRoot)
    os=$os.Caption
    os_build=$os.BuildNumber
    cpu=$cpu.Name
    wsl_usable=$wslUsable
    install_missing=$InstallMissing.IsPresent
    versions=@{openvino=$OpenVinoVersion;openvino_tokenizers=$OpenVinoTokenizersVersion;openvino_genai=$OpenVinoGenAiVersion;ovms="2026.3.0"}
    official_sources=@{intel_npu_driver=$IntelNpuDriverUrl;openvino_pip=$OpenVinoPipUrl;openvino_npu=$OpenVinoNpuUrl;ovms_windows=$OvmsDocsUrl;ovms_archive=$OvmsUrl;ovms_sha256=$OvmsSha256Url;wsl=$WslUrl;python=$PythonUrl;vc_redist_x64=$VcRedistUrl}
    # Free-form Detail/Action strings intentionally stay on the interactive
    # console. They may contain local absolute paths and are not experiment data.
    results=@($Results | ForEach-Object { [pscustomobject]@{Check=$_.Check;Status=$_.Status} })
}
''',
)

# Regression: independently walk complete structures. Synthetic cases cover
# POSIX, drive-letter and UNC paths; integration cases prove real run_case and
# capture_manifest outputs satisfy the property.
p = ROOT / "tests" / "integration" / "test_persistence_privacy.py"
p.write_text(
    r'''from __future__ import annotations

import re

from evaluation.run_evaluation import CASES, ModelConfig, run_case
from measurement.capture_run_manifest import capture_manifest
from local_agent.persistence import sanitize_for_persistence


# Independent from the production redactor on purpose. If the implementation
# forgets one path family, this test still has a chance to catch it.
_POSIX = re.compile(r"(?<![:/A-Za-z0-9])/(?!/)[^\s\"'<>|]+")
_DRIVE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'<>|]+")
_UNC = re.compile(r"(?<!\\)\\\\[^\\/\s]+[\\/][^\s\"'<>|]+")


def _absolute_paths(value, at="$"):
    found = []
    if isinstance(value, str):
        for regex in (_POSIX, _DRIVE, _UNC):
            for match in regex.finditer(value):
                found.append((at, match.group(0)))
    elif isinstance(value, dict):
        for key, item in value.items():
            found.extend(_absolute_paths(key, f"{at}.<key>"))
            found.extend(_absolute_paths(item, f"{at}.{key}"))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(_absolute_paths(item, f"{at}[{index}]"))
    return found


def test_sanitizer_covers_all_absolute_path_families_recursively():
    payload = {
        "/home/aiden/key": [
            "prefix /home/aiden/project/private.cpp suffix",
            {"windows": r"C:\Users\Aiden\work\asset.bin"},
            {"unc": r"\\corp\share\secret\model.xml"},
        ]
    }
    safe = sanitize_for_persistence(payload)
    assert _absolute_paths(safe) == []
    assert "/home/aiden" not in repr(safe)
    assert "C:\\Users\\Aiden" not in repr(safe)
    assert "corp\\share" not in repr(safe)


def test_run_case_emits_no_absolute_path_anywhere(tmp_path):
    case = next(case for case in CASES if case.name == "clean-build")
    row = run_case(
        case,
        ModelConfig(),
        tmp_path / "work",
        auto_approve=True,
        rehearse=True,
        transcript_out=tmp_path / "private" / "run.json",
        keep_all_transcripts=True,
        attempt=0,
        condition="skill",
    )
    assert row["transcript"] is not None
    assert _absolute_paths(row) == []


def test_captured_manifest_contains_no_absolute_path_in_any_field():
    manifest = capture_manifest(
        "ptl-npu-8b",
        ["skill"],
        {"clean-build"},
    )
    assert _absolute_paths(manifest) == []
''',
    encoding="utf-8",
    newline="\n",
)

# Record the decision where future reviewers will look for it.
p = ROOT / "docs" / "open-methodology-questions.md"
replace_once(
    p,
    '''**Needed:** one helper recording paths relative to a declared root, or basename
plus a hash of the full path. **This has to land before the first generation-2
row**, because `measurement/*.py` is inside the hashed surface, so doing it later
moves `source_sha256` mid-pilot. It is a source-only move and does not end a
generation.
''',
    '''**Resolved 2026-09-14, before generation-2 row one:** persisted evaluation
rows and pre-run manifests now pass through one recursive persistence-boundary
redactor that replaces POSIX, Windows drive-letter and UNC absolute paths with
opaque path hashes. The regression walks the complete returned structure rather
than named fields, so adding a field later cannot silently reintroduce a path.
The work-laptop bootstrap report also omits the computer name, absolute roots and
free-form result details; those remain interactive console diagnostics only.

This is a source-only generation-2 move. `source_sha256` moves with the fix;
`base_prompt_sha256` and `outcome_contract_sha256` remain frozen.
''',
)

for path in (
    ROOT / "local_agent" / "persistence.py",
    ROOT / "evaluation" / "run_evaluation.py",
    ROOT / "measurement" / "capture_run_manifest.py",
    ROOT / "tests" / "integration" / "test_persistence_privacy.py",
):
    compile(path.read_text(encoding="utf-8"), str(path), "exec")

print("item 9 patch applied")
