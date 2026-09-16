from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_windows_bootstrap_entrypoint_preserves_raw_argument_compatibility():
    text = (ROOT / "scripts" / "bootstrap-work-laptop.ps1").read_text(encoding="utf-8")
    assert "Compatibility entry point for Windows PowerShell 5.1" in text
    assert "for ($i = 0; $i -lt $args.Count; $i++)" in text
    assert '"-RuntimeRoot"' in text
    assert "& $Core @coreArgs" in text


def test_windows_bootstrap_entrypoint_creates_venv_before_core():
    text = (ROOT / "scripts" / "bootstrap-work-laptop.ps1").read_text(encoding="utf-8")
    create = text.index('-m venv (Join-Path $RepoRoot ".venv-workstation")')
    delegate = text.index("& $Core @coreArgs")
    assert create < delegate
    assert 'Test-Path $venvPython' in text


def test_historical_bootstrap_is_retained_as_core_implementation():
    core = ROOT / "scripts" / "bootstrap-work-laptop-core.ps1"
    assert core.is_file()
    text = core.read_text(encoding="utf-8")
    assert "Local Code Agent work-laptop bootstrap" in text
