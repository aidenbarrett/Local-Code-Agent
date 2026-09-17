from io import StringIO
from pathlib import Path
import sys


INTERNAL = Path(__file__).resolve().parents[2]
if str(INTERNAL) not in sys.path:
    sys.path.insert(0, str(INTERNAL))

from terminal_ui import LCA_LOGO, WIDTH, device_label, ui  # noqa: E402


def test_lca_logo_is_mechanically_aligned():
    assert len(LCA_LOGO) == 6
    widths = {len(line) for line in LCA_LOGO}
    assert widths == {26}


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
