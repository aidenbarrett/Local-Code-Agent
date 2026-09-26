from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SELF = Path(__file__).resolve()


def _text_files():
    suffixes = {".py", ".md", ".yml", ".yaml", ".toml", ".ps1", ".json"}
    for path in ROOT.rglob("*"):
        if path.is_file() and path.suffix.lower() in suffixes and path.resolve() != SELF:
            if ".git" not in path.parts and ".local-agent" not in path.parts:
                yield path


def test_legacy_research_paths_have_no_live_references():
    old_measurement = "/".join(("internal", "measurement"))
    old_experiments = "/".join(("internal", "experiments"))
    import_phrase = "from " + "measurement" + " import"
    import_prefix = "from " + "measurement" + "."
    retired_paths = (
        "internal/devtools/" + "check_rename_safety.py",
        "internal/docs/" + "quality-hardening-roadmap.md",
        "internal/docs/" + "review-history.md",
        "internal/docs/" + "bring-up.md",
        "internal/docs/" + "testing-and-tev-framework.md",
        "internal/docs/" + "work-laptop-bootstrap.md",
        "internal/docs/" + "serving-and-accelerators.md",
        "internal/docs/" + "UX_ROOT_REFACTOR_PLAN.md",
    )
    offenders = []
    for path in _text_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for token in (old_measurement, old_experiments, import_phrase, import_prefix, *retired_paths):
            if token in text:
                offenders.append(f"{path.relative_to(ROOT)} -> {token}")
    assert not offenders, "legacy experiment references remain:\n" + "\n".join(offenders)


def test_retired_paths_are_actually_absent():
    retired = (
        ROOT / "internal" / "measurement",
        ROOT / "internal" / "experiments",
        ROOT / "internal" / "devtools" / "check_rename_safety.py",
        ROOT / "internal" / "docs" / "quality-hardening-roadmap.md",
        ROOT / "internal" / "docs" / "review-history.md",
    )
    assert not [path.relative_to(ROOT) for path in retired if path.exists()]


def test_public_docs_do_not_present_archived_generation_results():
    tokens = ["Generation" + " 1", "Gen" + "1", "9" + "/30", "23" + "/29"]
    offenders = []
    for relative in ("README.md", "PROJECT_OVERVIEW.md", "CURRENT_STATE.md"):
        path = ROOT / relative
        text = path.read_text(encoding="utf-8", errors="replace")
        for token in tokens:
            if token in text:
                offenders.append(f"{relative} -> {token}")
    assert not offenders, "archived result claims remain in public docs:\n" + "\n".join(offenders)
