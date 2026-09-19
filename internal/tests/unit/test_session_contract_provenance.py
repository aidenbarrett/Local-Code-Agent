"""Regression coverage for runtime session-contract provenance."""
from __future__ import annotations

from pathlib import Path
import shutil
import sys


INTERNAL = Path(__file__).resolve().parents[2]
REPO = INTERNAL.parent
if str(INTERNAL) not in sys.path:
    sys.path.insert(0, str(INTERNAL))

from local_agent import provenance  # noqa: E402


SCHEMA_KEY = "internal/docs/session-contract/v1/events.schema.json"


def test_session_event_schema_mutation_moves_source_hash(tmp_path, monkeypatch):
    """A validation-contract edit must change the declared source identity."""
    source = REPO / SCHEMA_KEY
    dest = tmp_path / SCHEMA_KEY
    dest.parent.mkdir(parents=True)
    shutil.copy2(source, dest)

    monkeypatch.setattr(provenance, "_ROOT", tmp_path)
    keys = [provenance._key(path) for path in provenance._files()]
    assert SCHEMA_KEY in keys, "runtime event schema is outside source_sha256"

    before = provenance.source_sha256()
    original = dest.read_bytes()
    needle = b'"maxLength":128'
    assert needle in original, "fixture no longer contains the validation bound used by this regression"
    dest.write_bytes(original.replace(needle, b'"maxLength":129', 1))

    assert provenance.source_sha256() != before
