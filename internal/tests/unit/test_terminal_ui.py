from io import BytesIO, StringIO, TextIOWrapper
from pathlib import Path
import sys


INTERNAL = Path(__file__).resolve().parents[2]
if str(INTERNAL) not in sys.path:
    sys.path.insert(0, str(INTERNAL))

from terminal_ui import ASCII_LCA_LOGO, LCA_LOGO, WIDTH, device_label, ui  # noqa: E402


def test_lca_logo_is_mechanically_aligned():
    assert len(LCA_LOGO) == 6
    widths = {len(line) for line in LCA_LOGO}
    assert widths == {26}


def test_ascii_fallback_logo_is_mechanically_aligned():
    assert len(ASCII_LCA_LOGO) == 6
    assert len({len(line) for line in ASCII_LCA_LOGO}) == 1


def test_plain_banner_has_no_escape_sequences_and_keeps_the_identity():
    stream = StringIO()
    term = ui(stream=stream, colour=False)
    term.banner(
        "LOCAL AI HARDWARE DEMO",
        "Same local Qwen3-8B model. Only the hardware target changes.",
    )
    output = stream.getvalue()

    assert "\x1b" not in output
    for line in LCA_LOGO:
        assert line in output
    assert "LOCAL CODE AGENT" in output
    assert "LOCAL AI HARDWARE DEMO" in output
    assert "Same local Qwen3-8B model. Only the hardware target changes." in output


def test_section_rail_is_exactly_the_shared_width():
    stream = StringIO()
    term = ui(stream=stream, colour=False)
    term.section("LIVE INFERENCE")
    rail = stream.getvalue().splitlines()[0]
    assert len(rail) == WIDTH
    assert rail.startswith("╠═[ LIVE INFERENCE ]")


def test_device_labels_explain_the_hardware_to_unknown_users():
    assert device_label("NPU") == "NPU · laptop AI accelerator"
    assert device_label("GPU") == "GPU · graphics processor"
    assert device_label("CPU") == "CPU · general-purpose processor"


def test_colour_is_additive_not_required_for_status_meaning():
    stream = StringIO()
    term = ui(stream=stream, colour=False)
    term.status("ok", "Result independently verified")
    term.status("warn", "Passing output is stale")
    term.status("fail", "Rebuild failed")
    output = stream.getvalue()

    assert "✓ Result independently verified" in output
    assert "! Passing output is stale" in output
    assert "× Rebuild failed" in output
    assert "\x1b" not in output


def test_cp1252_redirect_uses_ascii_identity_instead_of_crashing():
    raw = BytesIO()
    stream = TextIOWrapper(raw, encoding="cp1252", errors="strict")
    term = ui(stream=stream, colour=False)

    term.banner(
        "LOCAL AI HARDWARE DEMO",
        "Same local Qwen3-8B model. Only the hardware target changes.",
    )
    term.section("LIVE INFERENCE")
    term.status("ok", "Result independently verified")
    term.status("active", "Inference running")
    stream.flush()

    output = raw.getvalue().decode("cp1252")
    assert "LOCAL CODE AGENT" in output
    assert "LOCAL AI HARDWARE DEMO" in output
    assert "LLLLLL" in output
    assert "--[ LIVE INFERENCE ]" in output
    assert "+ Result independently verified" in output
    assert "* Inference running" in output
    assert "██" not in output
    assert "╠" not in output
    assert "✓" not in output
