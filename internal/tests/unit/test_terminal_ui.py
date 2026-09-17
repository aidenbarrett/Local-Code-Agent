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


def test_answer_panel_wraps_content_and_keeps_shared_width():
    stream = StringIO()
    term = ui(stream=stream, colour=False)
    term.panel(
        "LOCAL MODEL ANSWER",
        "This is a deliberately long answer that should wrap inside the panel "
        "instead of running across the terminal and hiding the result hierarchy.",
    )
    lines = stream.getvalue().splitlines()

    assert lines[0].startswith("╔ LOCAL MODEL ANSWER ")
    assert lines[-1] == "╚" + "═" * (WIDTH - 2) + "╝"
    assert all(len(line) == WIDTH for line in lines)
    assert "deliberately long answer" in " ".join(lines)


def test_answer_panel_has_readable_ascii_fallback():
    raw = BytesIO()
    stream = TextIOWrapper(raw, encoding="cp1252", errors="strict")
    term = ui(stream=stream, colour=False)
    term.panel("ANSWER", "A short result")
    stream.flush()

    output = raw.getvalue().decode("cp1252")
    assert "+ ANSWER " in output
    assert "|  A short result" in output
    assert "╔" not in output
    assert "║" not in output


def test_footer_note_wraps_secondary_copy_without_terminal_spill():
    stream = StringIO()
    term = ui(stream=stream, colour=False)
    term.footer_note(
        "CONTROL BOUNDARY · The model proposes answers and tool calls. "
        "Local Code Agent decides what may execute, tracks repository state, "
        "and decides what evidence is current enough to count as verified."
    )
    lines = [line for line in stream.getvalue().splitlines() if line]

    assert len(lines) >= 2
    assert all(len(line) <= WIDTH for line in lines)
    assert "CONTROL BOUNDARY" in lines[0]
    assert "count as verified" in " ".join(lines)


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
