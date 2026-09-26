from pathlib import Path

from local_agent import provenance

ROOT = Path(__file__).resolve().parents[3]


def test_product_source_hash_is_stable_sha256():
    first = provenance.source_sha256()
    second = provenance.source_sha256()
    assert first == second
    assert len(first) == 64
    int(first, 16)


def test_product_source_identity_covers_live_product_surfaces():
    keys = {provenance._key(path) for path in provenance._files()}
    assert any(key.startswith("internal/local_agent/") for key in keys)
    assert any(key.startswith("internal/serving/") for key in keys)
    assert any(key.startswith("internal/scripts/") for key in keys)
    assert any(key.startswith("internal/skills/") for key in keys)
    assert "internal/docs/session-contract/v1/events.schema.json" in keys
    assert "local-code-agent.ps1" in keys
    assert "install.ps1" in keys


def test_archived_research_trees_are_not_part_of_product_identity():
    keys = {provenance._key(path) for path in provenance._files()}
    old_measurement = "/".join(("internal", "measurement")) + "/"
    old_experiments = "/".join(("internal", "experiments")) + "/"
    assert not any(key.startswith(old_measurement) for key in keys)
    assert not any(key.startswith(old_experiments) for key in keys)
