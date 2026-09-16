from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "scripts" / "bootstrap-work-laptop-core.ps1"


def _text():
    return CORE.read_text(encoding="utf-8")


def test_slow_bootstrap_stages_announce_progress_before_work():
    text = _text()

    expected = [
        "Creating the project Python virtual environment",
        "Installing Local Code Agent and development dependencies",
        "Installing/verifying the pinned OpenVINO packages",
        "Inspecting the existing OVMS tree",
        "Downloading the pinned OVMS Windows archive",
        "Verifying the OVMS SHA256",
        "Extracting the OVMS archive",
        "Checking the official download/documentation endpoints",
    ]
    for message in expected:
        assert message in text

    assert text.index("Inspecting the existing OVMS tree") < text.index(
        "Get-ChildItem $OvmsDir -Recurse -Filter ovms.exe"
    )
    assert text.index("Downloading the pinned OVMS Windows archive") < text.index(
        "Invoke-WebRequest -Uri $OvmsUrl -OutFile $zip"
    )
    assert text.index("Extracting the OVMS archive") < text.index(
        "Expand-Archive -Path $zip"
    )


def test_progress_messages_do_not_change_result_contract():
    text = _text()
    assert 'function ProgressNote([string]$Text)' in text
    assert 'Write-Host ("[working] {0}" -f $Text)' in text
    assert 'results=@($Results | ForEach-Object' in text
    assert 'if ($fail -eq 0) { Write-Host "Ready for NPU runtime qualification."; exit 0 }' in text
