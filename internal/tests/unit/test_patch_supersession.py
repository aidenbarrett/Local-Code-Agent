"""One pending proposal per file: separate edits combine, a re-edit replaces and says so."""
from __future__ import annotations

import pytest

from local_agent.config import load_repo_config
from local_agent.tools import build_registry
from local_agent.tools.tool_primitives import ToolError

RING = "src/ring_buffer.cpp"


def _tools(sandbox):
    registry, _ctx, store = build_registry(load_repo_config(sandbox.root))
    return (registry.get("propose_patch").handler, registry.get("propose_file").handler,
            registry.get("apply_patch").handler, store)


def test_two_edits_to_different_lines_both_land_with_one_apply(sandbox):
    """The case that must not regress: on hardware the model proposes, then proposes again."""
    propose, _new, apply, store = _tools(sandbox)
    target = sandbox.root / RING
    original = target.read_text()
    first = propose(path=RING, find="    ++count_;\n", replace="    ++count_;  // pushed\n")
    second = propose(path=RING, find="    --count_;\n", replace="    --count_;  // popped\n")
    first_id, second_id = first.data["patch_id"], second.data["patch_id"]
    assert second.data["includes"] == first_id and second.data["supersedes"] == first_id
    assert f"includes {first_id}, apply {second_id} only" in second.summary
    assert "+    ++count_;  // pushed" in second.data["diff"]  # one diff, both edits
    assert len(store) == 1 and target.read_text() == original
    with pytest.raises(ToolError, match=f"{first_id} is included in {second_id}; apply {second_id}"):
        apply(patch_id=first_id)
    apply(patch_id=second_id)
    text = target.read_text()
    assert "++count_;  // pushed" in text and "--count_;  // popped" in text


def test_an_edit_to_text_only_the_pending_proposal_contains_builds_on_it(sandbox):
    propose, _new, apply, _store = _tools(sandbox)
    first = propose(path=RING, find="    ++count_;\n", replace="    ++count_;  // draft\n")
    second = propose(path=RING, find="// draft", replace="// final")
    assert second.data["includes"] == first.data["patch_id"]
    apply(patch_id=second.data["patch_id"])
    assert "++count_;  // final" in (sandbox.root / RING).read_text()


def test_a_re_edit_of_the_same_lines_replaces_and_names_the_dropped_edit(sandbox):
    propose, _new, apply, store = _tools(sandbox)
    target = sandbox.root / RING
    original = target.read_text()
    old = propose(path=RING, find="++count_;", replace="++count_; // old")
    new = propose(path=RING, find="++count_;", replace="++count_; // new")
    old_id, new_id = old.data["patch_id"], new.data["patch_id"]
    assert new.data["supersedes"] == old_id and new.data["includes"] is None
    assert f"replaces {old_id}, whose edit is dropped" in new.summary
    assert len(store) == 1 and target.read_text() == original
    with pytest.raises(ToolError, match=f"{old_id} was replaced by {new_id}, which does not contain"):
        apply(patch_id=old_id)
    apply(patch_id=new_id)
    assert target.read_text() == original.replace("++count_;", "++count_; // new")


def test_other_file_pending_patch_survives(sandbox):
    propose, new_file, apply, store = _tools(sandbox)
    other = new_file(path="src/other.cpp", content="// other\n")
    propose(path=RING, find="++count_;", replace="++count_; // old")
    propose(path=RING, find="++count_;", replace="++count_; // new")
    assert len(store) == 2
    apply(patch_id=other.data["patch_id"])
    assert (sandbox.root / "src/other.cpp").read_text() == "// other\n"


def test_invalid_new_proposal_does_not_displace_valid_one(sandbox):
    propose, _new, apply, store = _tools(sandbox)
    old = propose(path=RING, find="++count_;", replace="++count_; // valid")
    with pytest.raises(ToolError, match="does not appear"):
        propose(path=RING, find="not in the file", replace="bad")
    assert len(store) == 1
    apply(patch_id=old.data["patch_id"])
    assert "// valid" in (sandbox.root / RING).read_text()


def test_a_combined_proposal_still_refuses_an_external_file_change(sandbox):
    propose, _new, apply, _store = _tools(sandbox)
    propose(path=RING, find="    ++count_;\n", replace="    ++count_;  // pushed\n")
    combined = propose(path=RING, find="    --count_;\n", replace="    --count_;  // popped\n")
    assert combined.data["includes"] is not None
    target = sandbox.root / RING
    external = target.read_text() + "\n// user edit\n"
    target.write_text(external)
    with pytest.raises(ToolError, match="changed since patch"):
        apply(patch_id=combined.data["patch_id"])
    assert target.read_text() == external


def test_a_pending_proposal_made_stale_by_an_external_change_is_not_built_on(sandbox):
    propose, _new, apply, _store = _tools(sandbox)
    stale = propose(path=RING, find="    ++count_;\n", replace="    ++count_;  // pushed\n")
    target = sandbox.root / RING
    target.write_text(target.read_text() + "// user edit\n")
    fresh = propose(path=RING, find="    --count_;\n", replace="    --count_;  // popped\n")
    assert fresh.data["includes"] is None
    assert f"replaces {stale.data['patch_id']}, whose edit is dropped" in fresh.summary
    apply(patch_id=fresh.data["patch_id"])
    text = target.read_text()
    assert "// popped" in text and "// pushed" not in text and "// user edit" in text


def test_supersession_chain_names_latest_and_never_replays_applied_edit(sandbox):
    _propose, new_file, apply, _store = _tools(sandbox)
    results = [new_file(path="src/new.cpp", content=f"// version {i}\n") for i in range(3)]
    latest = results[-1].data["patch_id"]
    for previous in results[:-1]:
        with pytest.raises(ToolError, match=f"replaced by {latest}"):
            apply(patch_id=previous.data["patch_id"])
    apply(patch_id=latest)
    with pytest.raises(ToolError, match="unknown patch id"):
        apply(patch_id=latest)
    with pytest.raises(ToolError, match="no longer pending"):
        apply(patch_id=results[0].data["patch_id"])
    assert (sandbox.root / "src/new.cpp").read_text() == "// version 2\n"
