from pathlib import Path


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"{path}: expected exactly one replacement target")
    path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


p = Path("evaluation/run_evaluation.py")
replace_once(
    p,
    "    return os.path.relpath(path, Path.cwd())\n",
    "    # Windows cannot express a relative path between drive volumes (for\n"
    "    # example Actions checks out on D: while pytest tmp_path is on C:).\n"
    "    # Keep the private transcript where requested and persist a non-secret\n"
    "    # locator. Same-volume runs retain the useful cwd-relative reference;\n"
    "    # cross-volume runs retain the filename, which is resolved against the\n"
    "    # separately known transcript store rather than leaking an absolute root.\n"
    "    try:\n"
    "        return os.path.relpath(path, Path.cwd())\n"
    "    except ValueError:\n"
    "        return path.name\n",
)

p = Path("tests/integration/test_persistence_privacy.py")
replace_once(
    p,
    '''    row = run_case(\n        case,\n        ModelConfig(),\n        tmp_path / "work",\n        auto_approve=True,\n        rehearse=True,\n        transcript_out=tmp_path / "private" / "run.json",\n        keep_all_transcripts=True,\n        attempt=0,\n        condition="skill",\n    )\n    assert row["transcript"] is not None\n    assert _absolute_paths(row) == []\n    transcript = Path(row["transcript"])\n    assert not transcript.is_absolute()\n    saved = json.loads(transcript.read_text(encoding="utf-8"))\n    assert saved["case"] == "clean-build"\n''',
    '''    transcript_out = tmp_path / "private" / "run.json"\n    row = run_case(\n        case,\n        ModelConfig(),\n        tmp_path / "work",\n        auto_approve=True,\n        rehearse=True,\n        transcript_out=transcript_out,\n        keep_all_transcripts=True,\n        attempt=0,\n        condition="skill",\n    )\n    assert row["transcript"] is not None\n    assert _absolute_paths(row) == []\n    transcript_ref = Path(row["transcript"])\n    assert not transcript_ref.is_absolute()\n    # The private transcript remains usable at the caller-owned output path.\n    # On Windows the checkout and pytest temp directory can be different drives,\n    # where no filesystem-relative path between them exists. The persisted row\n    # must still be privacy-safe rather than crashing or leaking the drive path.\n    saved = json.loads(transcript_out.read_text(encoding="utf-8"))\n    assert saved["case"] == "clean-build"\n''',
)

compile(Path("evaluation/run_evaluation.py").read_text(encoding="utf-8"), "evaluation/run_evaluation.py", "exec")
compile(Path("tests/integration/test_persistence_privacy.py").read_text(encoding="utf-8"), "tests/integration/test_persistence_privacy.py", "exec")
print("Windows cross-volume transcript fix applied")
