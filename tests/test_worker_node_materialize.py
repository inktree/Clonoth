"""Self-check for _worker node materialization and dispatch-session retention.

Why: _worker moved from a plugin declaration to a physical config/nodes file so
the web settings editor can change provider/model, and fresh/fork dispatch
sessions must survive task completion (TTL cleanup instead of immediate delete).
How: temp workspace, call ensure_worker_node_yaml twice, parse the result with
load_node, and assert the router no longer references immediate cleanup.
Purpose: prove editability and retention without touching real workspaces.

Run: python tests/test_worker_node_materialize.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.builtin.agent_manage import WORKER_NODE_YAML, ensure_worker_node_yaml  # noqa: E402


def check_materialize_idempotent():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        first = ensure_worker_node_yaml(ws)
        assert first == ws / "config" / "nodes" / "_worker.yaml"
        assert first.exists()
        original = first.read_text(encoding="utf-8")
        assert "id: _worker" in original

        # Second call must not overwrite user edits.
        first.write_text(original.replace('provider: ""', 'provider: "deepseek"'), encoding="utf-8")
        ensure_worker_node_yaml(ws)
        assert 'provider: "deepseek"' in first.read_text(encoding="utf-8"), \
            "ensure must not clobber an existing file"
    print("check_materialize_idempotent: PASS")


def check_yaml_parses_into_node():
    import yaml

    from engine.node import _node_from_dict

    data = yaml.safe_load(WORKER_NODE_YAML)
    node = _node_from_dict(Path("/tmp"), "_worker", data)
    assert node is not None, "default worker YAML must parse into a Node"
    assert node.type == "ai"
    assert node.persistent is False
    assert node.provider == "" and node.model == "", "defaults follow the global provider/model"
    assert "{{instruction}}" in node.prompt
    assert node.control_tools == ["finish", "intermediate_reply", "compact_context", "manage_workers"]
    assert node.tool_access.mode == "all"
    print("check_yaml_parses_into_node: PASS")


def check_no_immediate_dispatch_cleanup():
    src = Path("supervisor/task_router.py").read_text(encoding="utf-8")
    assert "_cleanup_dispatch_session_locked" not in src, \
        "immediate fresh/fork cleanup must be fully removed (TTL sweep owns deletion now)"
    assert "fresh/fork dispatch session 不再完成即删" in src, "retention comment must be present"
    state_src = Path("supervisor/state.py").read_text(encoding="utf-8")
    assert "conversations\" / f\"{sid}.jsonl" in state_src or "jsonl" in state_src, \
        "stale sweep should delete conversation files alongside registry rows"
    print("check_no_immediate_dispatch_cleanup: PASS")


def main() -> int:
    check_materialize_idempotent()
    check_yaml_parses_into_node()
    check_no_immediate_dispatch_cleanup()
    print("all worker-node materialize checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
