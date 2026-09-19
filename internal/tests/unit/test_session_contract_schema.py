"""Guard the Session Hub v1 wire contract and its runtime boundary.

These checks pin vocabulary, fields and local references. They are not a JSON Schema
metaschema validator and they do not replace producer/consumer lifecycle tests. The
versioned JSON schema is normative; runtime validation must load that exact asset.
Prototype `.emit` vocabulary remains separately inventoried until those producers are
migrated, because matching an event name alone never establishes conformance.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

from local_agent.session import event_contract

ROOT = Path(__file__).resolve().parents[3]
SCHEMA = ROOT / "internal/docs/session-contract/v1/events.schema.json"
SESSION = ROOT / "internal/local_agent/session"

# Reviewed field inventory, independent of the schema being checked. Nullable
# fields are still required. A deliberate contract change updates both in review.
PAYLOAD_FIELDS = {
    "session.opened": "conversation_id repository_id controller_commit capabilities recovered",
    "turn.recorded": "turn_ref role content_ref",
    "route.proposed": "route_id revision turn_ref source mode skill rule_id explanation requires_acceptance",
    "route.resolved": "route_id revision resolution mode source skill",
    "task.admitted": "origin request_ref contract_sha256 repository_id skill execution_epoch deadline_utc",
    "task.state_changed": "previous current reason_code execution_epoch",
    "endpoint.state_changed": "endpoint_id role state queue_position wait_ms lease_id",
    "tool.started": "call_id tool_name arguments_ref execution_epoch deadline_utc",
    "tool.finished": "call_id tool_name execution domain reason_code exit_code duration_ms evidence_ids result_ref execution_epoch",
    "artifact.recorded": "artifact_ref purpose truncated",
    "task.cancel_requested": "request_id source reason_code execution_epoch",
    "task.verdict": "completion",
    "task.closed": "status result_ref cleanup",
    "watch.state_changed": "job_id job_revision schedule_revision state reason_code due_utc next_due_utc",
    "watch.run_recorded": "job_id job_revision schedule_revision execution_contract_sha256 run_id task_id due_utc started_utc finished_utc status verdict previous_attempt_id comparison_run_id comparison counts delta_ref next_due_utc",
    "telemetry.policy": "visible requested_sampling effective_sampling run_mode reason_code sampler_ids poll_interval_ms manifest_ref exclusion_lease_id",
    "fault.reported": "reason_code message recoverable detail_ref",
    "telemetry.sample": "host_id device_id source sampled_utc utilization_percent memory_bytes stale unavailable_reason",
    "conversation.delta": "turn_request_id chunk_index text final",
}
ENVELOPE_FIELDS = set("schema_id schema_version event_id stream_id stream_kind sequence producer_epoch session_id task_id occurred_utc kind payload".split())


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        assert key not in result, f"duplicate JSON key: {key}"
        result[key] = value
    return result


def _schema():
    return json.loads(SCHEMA.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)


def _nodes(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _nodes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _nodes(value)


def _emitted_kinds():
    """Inventory current direct .emit calls, including the worker projection.

    Not general Python dataflow analysis. Unknown dynamic expressions fail rather
    than disappearing from the inventory. Aliased emitters/new producer packages
    require an explicit guard update and runtime conformance coverage at migration.
    """
    kinds = set()
    for path in SESSION.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "emit"):
                continue
            expr = node.args[0] if node.args else next(
                (k.value for k in node.keywords if k.arg == "kind"), None)
            if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
                kinds.add(expr.value)
                continue
            # The only current dynamic emitter is controller's allowlisted table.
            assert (path.name == "controller.py" and isinstance(expr, ast.BinOp)
                    and isinstance(expr.op, ast.Add)
                    and isinstance(expr.left, ast.Constant) and isinstance(expr.left.value, str)
                    and isinstance(expr.right, ast.Name) and expr.right.id == "kind"), (
                f"unaccounted dynamic event kind at {path.name}:{node.lineno}")
            tables = [n.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "fields" for t in n.targets)]
            assert len(tables) == 1, "worker projection table must be explicitly inventoried"
            fields = ast.literal_eval(tables[0])
            assert isinstance(fields, dict) and fields
            assert all(isinstance(k, str) for k in fields)
            kinds.update(expr.left.value + k for k in fields)
    assert kinds, "no prototype emitters found; update the migration guard"
    return kinds


def test_the_contract_is_structurally_a_json_schema():
    schema = _schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == "urn:lca:session:events:1"
    assert isinstance(schema["$defs"], dict) and schema["$defs"]
    assert isinstance(schema["oneOf"], list) and schema["oneOf"]


def test_every_reviewed_kind_has_exactly_one_branch():
    # oneOf requires exactly one matching branch, not "first match wins".
    names = [branch["properties"]["kind"]["const"] for branch in _schema()["oneOf"]]
    assert len(names) == len(set(names)), f"duplicate kinds: {names}"
    assert set(names) == set(PAYLOAD_FIELDS), "missing or unreviewed contract kind"


def test_envelopes_and_required_payload_fields_match_the_reviewed_contract():
    for branch in _schema()["oneOf"]:
        props = branch["properties"]
        kind = props["kind"]["const"]
        assert set(props) == set(branch["required"]) == ENVELOPE_FIELDS, kind
        assert props["schema_id"] == {"const": "lca.session.events"}, kind
        assert props["schema_version"] == {"const": 1}, kind
        expected_stream = "ephemeral" if kind in {"telemetry.sample", "conversation.delta"} else "durable"
        assert props["stream_kind"] == {"const": expected_stream}, kind
        payload = props["payload"]
        assert set(payload["properties"]) == set(payload["required"]) == set(PAYLOAD_FIELDS[kind].split()), kind


def test_all_object_shapes_are_closed_and_require_their_declared_fields():
    for node in _nodes(_schema()):
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, node
            assert set(node["required"]) == set(node["properties"]), node
            assert len(node["required"]) == len(set(node["required"])), node


def test_all_schema_references_resolve_locally():
    schema = _schema()
    refs = [node["$ref"] for node in _nodes(schema) if "$ref" in node]
    assert refs
    for ref in refs:
        assert ref.startswith("#/"), f"unexpected external schema dependency: {ref}"
        target = schema
        for token in ref[2:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            target = target[int(token)] if isinstance(target, list) else target[token]
        assert isinstance(target, (dict, bool)), ref


def test_the_prototype_vocabulary_and_the_v1_contract_stay_disjoint():
    declared = {b["properties"]["kind"]["const"] for b in _schema()["oneOf"]}
    overlap = declared & _emitted_kinds()
    assert not overlap, (
        f"prototype emits reserved v1 kinds: {sorted(overlap)}. Migrate with validated "
        "envelopes/payloads and producer, consumer and lifecycle conformance tests; "
        "name overlap, partial or complete, does not implement the contract.")


def test_runtime_validation_loads_the_normative_versioned_schema():
    """Implementation status is executable state, not a sentence in a README."""
    assert event_contract._SCHEMA_PATH.resolve() == SCHEMA.resolve()
    assert event_contract._SCHEMA == _schema()
    assert event_contract._VALIDATOR.schema == _schema()
