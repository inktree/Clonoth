"""Regression coverage for restart-time entry branch checkpointing."""

from __future__ import annotations

from types import SimpleNamespace

from supervisor.task_router import TaskRouterMixin


class _CheckpointHarness(TaskRouterMixin):
    def __init__(self, workspace_root, merge_counts: list[int]) -> None:
        self.workspace_root = workspace_root
        self.merge_counts = list(merge_counts)
        self.calls: list[tuple[str, str, int, str, str]] = []

    def _merge_branch_locked(
        self,
        parent_session_id: str,
        branch_session_id: str,
        base_count: int,
        *,
        base_last_id: str = "",
        event_type: str = "branch_merged",
    ) -> int:
        self.calls.append((parent_session_id, branch_session_id, base_count, base_last_id, event_type))
        return self.merge_counts.pop(0)


def test_restart_checkpoint_advances_base_for_racing_normal_completion(tmp_path) -> None:
    from engine.conversation_store import ConversationStore, Message

    store = ConversationStore(tmp_path / "data" / "conversations")
    store.append("branch_42", Message(id="msg-15", role="assistant", content="step 1"))
    harness = _CheckpointHarness(tmp_path, [4, 2])
    task = SimpleNamespace(input={
        "parent_session_id": "parent-session",
        "branch_session_id": "branch_42",
        "base_count": 11,
        "base_last_id": "msg-11",
    })

    assert harness._checkpoint_branch_task_locked(task) == 4
    assert task.input["base_count"] == 15
    assert task.input["base_last_id"] == "msg-15"
    assert task.input["_restart_checkpointed_count"] == 4
    assert harness.calls[0] == (
        "parent-session", "branch_42", 11, "msg-11", "branch_checkpointed",
    )

    store.append("branch_42", Message(id="msg-17", role="assistant", content="step 2"))
    # If task completion wins before shutdown, only the tail written after the
    # checkpoint is merged; the first four messages cannot be duplicated.
    assert harness._checkpoint_branch_task_locked(task) == 2
    assert task.input["base_count"] == 17
    assert task.input["base_last_id"] == "msg-17"
    assert task.input["_restart_checkpointed_count"] == 6
    assert harness.calls[1] == (
        "parent-session", "branch_42", 15, "msg-15", "branch_checkpointed",
    )


def test_restart_checkpoint_ignores_non_entry_tasks(tmp_path) -> None:
    harness = _CheckpointHarness(tmp_path, [])
    task = SimpleNamespace(input={"base_count": 5})

    assert harness._checkpoint_branch_task_locked(task) == 0
    assert harness.calls == []
