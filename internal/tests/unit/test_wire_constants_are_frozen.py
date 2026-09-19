"""Freeze the durable Session Hub v1 wire vocabulary against mechanical renames.

Python identifiers and files may be renamed. Values written to durable rows or
published to clients may not move as a side effect of that refactor. Protocol
changes require an explicit version/migration decision rather than a synchronized
search-and-replace across producer, schema, tests and prose.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest


INTERNAL = Path(__file__).resolve().parents[2]
if str(INTERNAL) not in sys.path:
    sys.path.insert(0, str(INTERNAL))

SCHEMA_PATH = INTERNAL / "docs" / "session-contract" / "v1" / "events.schema.json"

# Hard-coded on purpose. Never derive these from the code under test.
SCHEMA_ID = "lca.session.events"
SCHEMA_VERSION = 1
SCHEMA_URN = "urn:lca:session:events:1"
STREAM_KIND = "durable"

EVENT_KINDS = frozenset({
    "artifact.recorded", "conversation.delta", "endpoint.state_changed",
    "fault.reported", "route.proposed", "route.resolved", "session.opened",
    "task.admitted", "task.cancel_requested", "task.closed", "task.state_changed",
    "task.verdict", "telemetry.policy", "telemetry.sample", "tool.finished",
    "tool.started", "turn.recorded", "watch.run_recorded", "watch.state_changed",
})
TERMINAL_STATES = frozenset({
    "completed", "failed", "blocked", "cancelled", "timed_out", "interrupted", "unknown",
})
VERDICTS = frozenset({"VERIFIED", "FAILED", "REFUSED", "NO_VERDICT", "NOT_REQUIRED"})
ROUTE_SOURCES = frozenset({"rule", "model_proposal", "user_direct"})


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _payload_for(kind: str) -> dict:
    found: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            props = node.get("properties")
            if (
                isinstance(props, dict)
                and isinstance(props.get("kind"), dict)
                and props["kind"].get("const") == kind
            ):
                found.append(props.get("payload", {}))
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(_schema())
    assert len(found) == 1, f"{kind} is declared {len(found)} times"
    return found[0]


def _example(spec: dict):
    if "const" in spec:
        return spec["const"]
    if "enum" in spec:
        return spec["enum"][0]
    kind = spec.get("type")
    if kind == "string":
        if spec.get("format") == "uuid":
            from uuid import uuid4
            return str(uuid4())
        if spec.get("format") == "date-time":
            return "2030-01-01T00:00:00Z"
        pattern = spec.get("pattern", "")
        if "64" in pattern:
            return "a" * 64
        return "x"
    if kind == "integer":
        return spec.get("minimum", 0)
    if kind == "boolean":
        return False
    if kind == "array":
        return []
    if kind == "object":
        return {
            name: _example(sub)
            for name, sub in spec.get("properties", {}).items()
            if name in spec.get("required", [])
        }
    for option in spec.get("anyOf", []):
        if option.get("type") != "null":
            return _example(option)
    return None


def _minimal_session_opened_payload() -> dict:
    payload = _payload_for("session.opened")
    return {
        name: _example(payload.get("properties", {}).get(name, {}))
        for name in payload.get("required", [])
    }


def test_producer_stamps_the_frozen_v1_envelope_header():
    from uuid import uuid4
    from local_agent.session.event_contract import build_event

    event = build_event(
        stream_id=str(uuid4()),
        sequence=1,
        producer_epoch=str(uuid4()),
        kind="session.opened",
        payload=_minimal_session_opened_payload(),
    )
    assert event["schema_id"] == SCHEMA_ID
    assert event["schema_version"] == SCHEMA_VERSION
    assert event["stream_kind"] == STREAM_KIND


def test_schema_declares_the_same_id_and_version_for_every_event_kind():
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    ids = set(re.findall(r'"schema_id"\s*:\s*\{\s*"const"\s*:\s*"([^"]+)"', text))
    versions = set(
        int(value)
        for value in re.findall(r'"schema_version"\s*:\s*\{\s*"const"\s*:\s*([0-9]+)', text)
    )
    assert ids == {SCHEMA_ID}, f"schema declares schema_id values {ids}"
    assert versions == {SCHEMA_VERSION}, f"schema declares schema_version values {versions}"
    assert text.count(f'"{SCHEMA_ID}"') == len(EVENT_KINDS)
    assert len(re.findall(r'"schema_version"\s*:\s*\{\s*"const"\s*:', text)) == len(EVENT_KINDS)


def test_schema_urn_and_event_kinds_are_frozen():
    schema = _schema()
    assert schema["$id"] == SCHEMA_URN
    text = json.dumps(schema)
    found = set(re.findall(r'"const": "([a-z_]+\.[a-z_]+)"', text))
    assert found == EVENT_KINDS, (
        f"added: {sorted(found - EVENT_KINDS)}, removed: {sorted(EVENT_KINDS - found)}"
    )


def test_terminal_verdict_and_route_vocabularies_are_frozen_structurally():
    schema = _schema()
    completion = schema["$defs"]["TaskCompletion"]["properties"]
    assert set(completion["status"]["enum"]) == TERMINAL_STATES
    assert set(schema["$defs"]["VerdictBlock"]["properties"]["verdict"]["enum"]) == VERDICTS
    route = _payload_for("route.proposed")["properties"]["source"]
    assert set(route["enum"]) == ROUTE_SOURCES


# Exact textual forms that committed source/prose may use. Anything else shaped
# like this namespace is a typo or an undeclared protocol version.
ALLOWED_WIRE_TOKENS = frozenset(
    {SCHEMA_ID, f"{SCHEMA_ID}/{SCHEMA_VERSION}", SCHEMA_URN} | set(EVENT_KINDS)
)

_UNTRACKED_DIRS = frozenset({
    "__pycache__", ".git", "build", "dist", ".venv", ".venv-workstation",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".local-agent", "node_modules",
    ".eggs", "htmlcov", "site-packages",
})
_SCANNED_SUFFIXES = frozenset({
    ".md", ".py", ".json", ".yml", ".yaml", ".toml", ".ps1", ".sh", ".txt", ".tcss",
})
_WIRE_TOKEN = re.compile(
    r"lca\.session\.[A-Za-z0-9_.]+(?:/[0-9]+)?|urn:lca:session:[A-Za-z0-9_:]+"
)


def _repository_root() -> Path:
    return INTERNAL.parent


def _committed_files(root: Path | None = None) -> tuple[list[Path], str]:
    root = root or _repository_root()
    git = shutil.which("git")
    if git and (root / ".git").exists():
        try:
            proc = subprocess.run(
                [git, "ls-files", "-z", "--cached", "--exclude-standard"],
                cwd=root,
                capture_output=True,
                timeout=60,
            )
            if proc.returncode == 0:
                names = [
                    name
                    for name in proc.stdout.decode("utf-8", "replace").split("\0")
                    if name
                ]
                paths = [path for path in (root / name for name in names) if path.is_file()]
                if paths:
                    return sorted(paths), "git ls-files"
        except (OSError, subprocess.SubprocessError):
            pass

    out: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative_parts = path.relative_to(root).parts
        if _UNTRACKED_DIRS.intersection(relative_parts):
            continue
        out.append(path)
    return out, "filesystem walk (no usable git index)"


def _offenders(root: Path, files: list[Path]) -> list[str]:
    offenders: list[str] = []
    for path in files:
        if path.suffix not in _SCANNED_SUFFIXES:
            continue
        relative = path.relative_to(root)
        if "experiments" in relative.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for found in sorted(set(_WIRE_TOKEN.findall(text))):
            token = found.rstrip(".,)`\"';")
            if token not in ALLOWED_WIRE_TOKENS:
                offenders.append(f"{relative.as_posix()}: {token!r}")
    return offenders


def test_no_committed_text_names_an_undeclared_wire_constant():
    root = _repository_root()
    files, source = _committed_files(root)
    assert files, f"no files to scan via {source}"
    offenders = _offenders(root, files)
    assert not offenders, (
        f"committed files scanned via {source} name undeclared session wire constants:\n  "
        + "\n  ".join(offenders)
    )


def test_undeclared_schema_versions_are_rejected_by_the_token_rules():
    def accepted(text: str) -> bool:
        return all(
            found.rstrip(".,)`\"';") in ALLOWED_WIRE_TOKENS
            for found in _WIRE_TOKEN.findall(text)
        )

    # Build negative fixtures from fragments: this committed test file is scanned
    # by the guard itself, so undeclared wire spellings must not appear literally.
    ns = "lca" + "." + "session" + "."
    urn = "urn" + ":lca:session:events:"

    assert accepted(f"the `{ns}events/1` contract")
    assert accepted(f'schema_id="{ns}events"')
    assert accepted(f"{urn}1")
    assert accepted("task.admitted")

    assert not accepted(f"{ns}events/2")
    assert not accepted(f"{ns}events/0")
    assert not accepted(f"{ns}live_event_buffer")
    assert not accepted(f"{ns}typo")
    assert not accepted(f"{urn}2")


def test_git_scope_ignores_untracked_files_but_scans_the_same_file_when_tracked(tmp_path):
    git = shutil.which("git")
    if not git:
        pytest.skip("git is required to exercise index-backed scan scope")

    subprocess.run([git, "init", "-q"], cwd=tmp_path, check=True)
    tracked = tmp_path / "tracked.md"
    tracked.write_text(f"uses {SCHEMA_ID}/{SCHEMA_VERSION}\n", encoding="utf-8")
    subprocess.run([git, "add", "tracked.md"], cwd=tmp_path, check=True)

    ns = "lca" + "." + "session" + "."
    untracked = tmp_path / "generated.txt"
    untracked.write_text(f"bad {ns}events/2\n", encoding="utf-8")
    build = tmp_path / "build"
    build.mkdir()
    (build / "generated.txt").write_text(f"bad {ns}events/2\n", encoding="utf-8")

    files, source = _committed_files(tmp_path)
    assert source == "git ls-files"
    assert [path.relative_to(tmp_path).as_posix() for path in files] == ["tracked.md"]
    assert _offenders(tmp_path, files) == []

    subprocess.run([git, "add", "generated.txt"], cwd=tmp_path, check=True)
    files, source = _committed_files(tmp_path)
    assert source == "git ls-files"
    offenders = _offenders(tmp_path, files)
    assert len(offenders) == 1
    assert offenders[0].startswith("generated.txt:")


def test_packaged_tree_fallback_skips_generated_directories(tmp_path):
    (tmp_path / "src.md").write_text(f"uses {SCHEMA_ID}\n", encoding="utf-8")
    generated = tmp_path / "build"
    generated.mkdir()
    ns = "lca" + "." + "session" + "."
    (generated / "generated.txt").write_text(f"bad {ns}events/2\n", encoding="utf-8")

    files, source = _committed_files(tmp_path)
    assert source == "filesystem walk (no usable git index)"
    assert [path.relative_to(tmp_path).as_posix() for path in files] == ["src.md"]
    assert _offenders(tmp_path, files) == []
