from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one replacement target, found {count}")
    path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: item9_final_contract_fix.py REPO")
    root = Path(sys.argv[1]).resolve()

    evaluator = root / "evaluation" / "run_evaluation.py"
    replace_once(
        evaluator,
        "from pathlib import Path\n",
        "from pathlib import Path, PurePosixPath, PureWindowsPath\n",
    )
    replace_once(
        evaluator,
        '''    # Windows cannot express a relative path between drive volumes (for\n    # example Actions checks out on D: while pytest tmp_path is on C:).\n    # Keep the private transcript where requested and persist a non-secret\n    # locator. Same-volume runs retain the useful cwd-relative reference;\n    # cross-volume runs retain the filename, which is resolved against the\n    # separately known transcript store rather than leaking an absolute root.\n    try:\n        return os.path.relpath(path, Path.cwd())\n    except ValueError:\n        return path.name\n\n\ndef run_case(\n''',
        '''    # The row is an artifact manifest, so references are relative to the\n    # result artifact directory, never to the process cwd. That makes the bundle\n    # portable, privacy-safe, and independent of Windows drive letters.\n    return path.relative_to(out.parent).as_posix()\n\n\ndef resolve_transcript_reference(out: Path, reference: str) -> Path:\n    """Resolve one persisted transcript locator against its result artifact.\n\n    The public row may name only a descendant of the result directory. Absolute\n    paths and parent traversal fail closed so a persisted reference can never\n    become a machine-specific or escaping filesystem path.\n    """\n    posix = PurePosixPath(reference)\n    windows = PureWindowsPath(reference)\n    if posix.is_absolute() or windows.is_absolute():\n        raise ValueError(f"absolute transcript reference is not allowed: {reference!r}")\n    if ".." in posix.parts or ".." in windows.parts:\n        raise ValueError(f"parent traversal is not allowed in transcript reference: {reference!r}")\n    return out.parent.joinpath(*posix.parts)\n\n\ndef run_case(\n''',
    )

    privacy_test = root / "tests" / "integration" / "test_persistence_privacy.py"
    replace_once(privacy_test, "import re\n", "import re\n\nimport pytest\n")
    replace_once(
        privacy_test,
        "from evaluation.run_evaluation import CASES, ModelConfig, run_case\n",
        "from evaluation.run_evaluation import (\n    CASES,\n    ModelConfig,\n    resolve_transcript_reference,\n    run_case,\n)\n",
    )
    replace_once(
        privacy_test,
        '''    transcript_ref = Path(row["transcript"])\n    assert not transcript_ref.is_absolute()\n    # save_transcript uses transcript_out as the naming seed and writes the\n    # private payload beside it in <stem>-transcripts/. Check the actual\n    # private artifact rather than assuming transcript_out itself is written.\n    transcript_path = transcript_out.with_name(transcript_out.stem + "-transcripts") / "clean-build-0.json"\n    saved = json.loads(transcript_path.read_text(encoding="utf-8"))\n    assert saved["case"] == "clean-build"\n\n\ndef test_captured_manifest_contains_no_absolute_path_in_any_field():\n''',
        '''    transcript_ref = Path(row["transcript"])\n    assert not transcript_ref.is_absolute()\n    assert transcript_ref.as_posix() == "run-transcripts/clean-build-0.json"\n    transcript_path = resolve_transcript_reference(transcript_out, row["transcript"])\n    assert transcript_path == (\n        transcript_out.parent / "run-transcripts" / "clean-build-0.json"\n    )\n    saved = json.loads(transcript_path.read_text(encoding="utf-8"))\n    assert saved["case"] == "clean-build"\n\n\n@pytest.mark.parametrize(\n    "reference",\n    [\n        "/tmp/private/run.json",\n        r"C:\\Users\\Aiden\\private\\run.json",\n        r"\\\\corp\\share\\private\\run.json",\n        "../private/run.json",\n    ],\n)\ndef test_transcript_reference_resolver_fails_closed(reference, tmp_path):\n    with pytest.raises(ValueError):\n        resolve_transcript_reference(tmp_path / "results.json", reference)\n\n\ndef test_captured_manifest_contains_no_absolute_path_in_any_field():\n''',
    )

    oracle_test = root / "tests" / "integration" / "test_oracle_isolation.py"
    replace_once(
        oracle_test,
        '''def test_rows_carry_what_an_audit_needs(tmp_path):\n    """Every hypothesis in the slice-3 audit would have been a fact with these."""\n    import json\n    from run_evaluation import run_case\n''',
        '''def test_rows_carry_what_an_audit_needs(tmp_path):\n    """Every hypothesis in the slice-3 audit would have been a fact with these."""\n    import json\n    from run_evaluation import resolve_transcript_reference, run_case\n''',
    )
    replace_once(
        oracle_test,
        '''    assert cheat["transcript"] is not None    # failed rows do\n    saved = json.loads(Path(cheat["transcript"]).read_text())\n    assert saved["case"] == "test-failure-fix"\n''',
        '''    assert cheat["transcript"] is not None    # failed rows do\n    cheat_path = resolve_transcript_reference(out, cheat["transcript"])\n    saved = json.loads(cheat_path.read_text(encoding="utf-8"))\n    assert saved["case"] == "test-failure-fix"\n''',
    )
    replace_once(
        oracle_test,
        '''    assert probe["succeeded"] is True and probe["transcript"] is not None\n    assert Path(probe["transcript"]).name == "test-failure-fix-1.json"\n''',
        '''    assert probe["succeeded"] is True and probe["transcript"] is not None\n    assert Path(probe["transcript"]).as_posix() == (\n        "r-transcripts/test-failure-fix-1.json"\n    )\n    assert resolve_transcript_reference(out, probe["transcript"]).is_file()\n''',
    )

    docs = root / "docs" / "open-methodology-questions.md"
    replace_once(
        docs,
        "Transcript references remain usable but are stored relative\nto the process working directory.",
        "Transcript references remain usable but are stored relative\nto the result artifact directory, so the bundle is portable across hosts and Windows drives.",
    )

    # Review guard: the old broken contract was direct cwd resolution of the
    # persisted reference. No Python file may directly open Path(row["transcript"]) again.
    dangerous = re.compile(
        r"Path\\([^\\n)]*['\\\"]transcript['\\\"][^\\n)]*\\)\\s*\\.\\s*"
        r"(?:read_text|read_bytes|open|exists|is_file)\\s*\\("
    )
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if ".git" in path.parts or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for match in dangerous.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(root).as_posix()}:{line}")
    if offenders:
        raise SystemExit("direct transcript dereference remains: " + ", ".join(offenders))

    # The process-cwd/cross-volume implementation was the source of the loop.
    current = evaluator.read_text(encoding="utf-8")
    for stale in ("os.path.relpath(path, Path.cwd())", "return path.name"):
        if stale in current:
            raise SystemExit(f"stale transcript locator logic remains: {stale}")

    # Compile every changed Python file before the expensive suites.
    for path in (evaluator, privacy_test, oracle_test):
        compile(path.read_text(encoding="utf-8"), str(path), "exec")

    # Reconcile the source-only identity after the entire candidate source is in
    # place. The two contract axes are not allowed to move.
    sys.path.insert(0, str(root))
    from local_agent import provenance as p  # noqa: E402

    instrument = root / "INSTRUMENT.json"
    declared = json.loads(instrument.read_text(encoding="utf-8"))
    if declared.get("generation") != 2:
        raise SystemExit("expected generation 2")
    old_source = declared.get("source_sha256")
    prompt = p.base_prompt_sha256()
    outcome = p.outcome_contract_sha256()
    if prompt != declared.get("base_prompt_sha256"):
        raise SystemExit(f"base prompt moved unexpectedly: {prompt}")
    if outcome != declared.get("outcome_contract_sha256"):
        raise SystemExit(f"outcome contract moved unexpectedly: {outcome}")
    source = p.source_sha256()
    if source == old_source:
        raise SystemExit("source hash did not move after transcript contract fix")
    declared["source_sha256"] = source
    instrument.write_text(
        json.dumps(declared, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print("generation", declared["generation"])
    print("source_sha256", source)
    print("base_prompt_sha256", prompt)
    print("outcome_contract_sha256", outcome)
    print("transcript contract: result-directory-relative, traversal-safe")


if __name__ == "__main__":
    main()
