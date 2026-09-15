from __future__ import annotations

import json
import re

import pytest
from pathlib import Path

from evaluation.run_evaluation import (
    CASES,
    ModelConfig,
    resolve_transcript_reference,
    run_case,
)
from measurement.capture_run_manifest import capture_manifest
from local_agent.persistence import sanitize_for_persistence


# Independent from the production redactor on purpose. If the implementation
# forgets one path family, this test still has a chance to catch it.
_POSIX = re.compile(r"(?<![.:/A-Za-z0-9])/(?!/)[^\s\"'<>|]+")
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


def test_sanitizer_preserves_parent_relative_references():
    value = "../../tmp/private/run.json"
    assert sanitize_for_persistence(value) == value
    assert _absolute_paths(value) == []


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


def test_run_case_emits_no_absolute_path_anywhere_and_keeps_transcript_usable(tmp_path):
    case = next(case for case in CASES if case.name == "clean-build")
    transcript_out = tmp_path / "private" / "run.json"
    row = run_case(
        case,
        ModelConfig(),
        tmp_path / "work",
        auto_approve=True,
        rehearse=True,
        transcript_out=transcript_out,
        keep_all_transcripts=True,
        attempt=0,
        condition="skill",
    )
    assert row["transcript"] is not None
    assert _absolute_paths(row) == []
    transcript_ref = Path(row["transcript"])
    assert not transcript_ref.is_absolute()
    assert transcript_ref.as_posix() == "run-transcripts/clean-build-0.json"
    transcript_path = resolve_transcript_reference(transcript_out, row["transcript"])
    assert transcript_path == (
        transcript_out.parent / "run-transcripts" / "clean-build-0.json"
    )
    saved = json.loads(transcript_path.read_text(encoding="utf-8"))
    assert saved["case"] == "clean-build"


@pytest.mark.parametrize(
    "reference",
    [
        "/tmp/private/run.json",
        r"C:\Users\Aiden\private\run.json",
        r"\\corp\share\private\run.json",
        "../private/run.json",
    ],
)
def test_transcript_reference_resolver_fails_closed(reference, tmp_path):
    with pytest.raises(ValueError):
        resolve_transcript_reference(tmp_path / "results.json", reference)


def test_captured_manifest_contains_no_absolute_path_in_any_field():
    manifest = capture_manifest(
        "ptl-npu-8b",
        ["skill"],
        {"clean-build"},
    )
    assert _absolute_paths(manifest) == []
