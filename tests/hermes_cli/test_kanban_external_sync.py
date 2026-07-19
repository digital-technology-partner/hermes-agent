"""External task adapter and run-reconciliation contracts."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _insert_running_task(conn, task_id: str, *, claim_lock=None, worker_pid=None):
    conn.execute(
        """
        INSERT INTO tasks (
            id, title, status, priority, created_at, workspace_kind,
            claim_lock, worker_pid
        ) VALUES (?, ?, 'running', 0, 1, 'scratch', ?, ?)
        """,
        (task_id, task_id, claim_lock, worker_pid),
    )
    conn.commit()


def test_init_does_not_backfill_naked_running_task(kanban_home):
    with kb.connect() as conn:
        _insert_running_task(conn, "naked")

    kb.init_db()

    with kb.connect() as conn:
        task = kb.get_task(conn, "naked")
        runs = kb.list_runs(conn, task_id="naked")

    assert task is not None
    assert task.current_run_id is None
    assert runs == []


def test_init_backfills_running_task_with_claim_evidence(kanban_home):
    with kb.connect() as conn:
        _insert_running_task(conn, "claimed", claim_lock="host:claimed")

    kb.init_db()

    with kb.connect() as conn:
        task = kb.get_task(conn, "claimed")
        runs = kb.list_runs(conn, task_id="claimed")

    assert task is not None
    assert task.current_run_id == runs[0].id
    assert runs[0].claim_lock == "host:claimed"
    assert runs[0].status == "running"


def test_external_in_progress_is_presentation_state_not_execution(kanban_home):
    with kb.connect() as conn:
        result = kb.sync_external_task(
            conn,
            task_id="external",
            title="External work",
            body="Source-owned body",
            presentation_status="in-progress",
            source="dtp_task_management",
            idempotency_key="dtp-task:external",
        )
        task = kb.get_task(conn, "external")
        runs = kb.list_runs(conn, task_id="external")

    assert result.created is True
    assert result.operational_status == "ready"
    assert task is not None
    assert task.external_status == "in-progress"
    assert task.status == "ready"
    assert task.current_run_id is None
    assert runs == []


def test_external_in_progress_preserves_genuinely_owned_execution(kanban_home):
    with kb.connect() as conn:
        kb.sync_external_task(
            conn,
            task_id="owned",
            title="Owned work",
            body=None,
            presentation_status="ready",
            source="test",
        )
        claimed = kb.claim_task(conn, "owned", claimer="host:owned")
        assert claimed is not None

        result = kb.sync_external_task(
            conn,
            task_id="owned",
            title="Owned work",
            body="updated",
            presentation_status="in-progress",
            source="test",
        )
        task = kb.get_task(conn, "owned")
        runs = kb.list_runs(conn, task_id="owned")

    assert result.operational_status == "running"
    assert task is not None
    assert task.current_run_id == runs[0].id
    assert runs[0].ended_at is None


def test_owned_run_outranks_newer_naked_pointer(kanban_home):
    with kb.connect() as conn:
        kb.sync_external_task(
            conn,
            task_id="conflict",
            title="Conflict",
            body=None,
            presentation_status="ready",
            source="test",
        )
        owned = conn.execute(
            """
            INSERT INTO task_runs (
                task_id, status, claim_lock, claim_expires, worker_pid, started_at
            ) VALUES ('conflict', 'running', 'host:owned', 4102444800, 999, 1)
            """
        ).lastrowid
        assert owned is not None
        naked = conn.execute(
            "INSERT INTO task_runs (task_id, status, started_at) "
            "VALUES ('conflict', 'running', 2)"
        ).lastrowid
        assert naked is not None
        conn.execute(
            "UPDATE tasks SET status='running', current_run_id=? WHERE id='conflict'",
            (naked,),
        )
        conn.commit()

        kb.sync_external_task(
            conn,
            task_id="conflict",
            title="Conflict",
            body=None,
            presentation_status="in-progress",
            source="test",
        )
        task = kb.get_task(conn, "conflict")
        runs = {run.id: run for run in kb.list_runs(conn, task_id="conflict")}

    assert task is not None
    assert task.current_run_id == owned
    assert runs[owned].ended_at is None
    assert runs[naked].outcome == "superseded_active"
    assert runs[naked].ended_at is not None


def test_external_in_progress_reconciles_naked_pointer_as_presentation_orphan(
    kanban_home,
):
    with kb.connect() as conn:
        kb.sync_external_task(
            conn,
            task_id="presentation",
            title="Presentation",
            body=None,
            presentation_status="ready",
            source="test",
        )
        naked = conn.execute(
            "INSERT INTO task_runs (task_id, status, started_at) "
            "VALUES ('presentation', 'running', 1)"
        ).lastrowid
        assert naked is not None
        conn.execute(
            "UPDATE tasks SET status='running', current_run_id=? "
            "WHERE id='presentation'",
            (naked,),
        )
        conn.commit()

        result = kb.sync_external_task(
            conn,
            task_id="presentation",
            title="Presentation",
            body=None,
            presentation_status="in-progress",
            source="test",
        )
        task = kb.get_task(conn, "presentation")
        runs = kb.list_runs(conn, task_id="presentation")

    assert result.operational_status == "ready"
    assert task is not None
    assert task.current_run_id is None
    assert runs[0].outcome == "presentation_orphan"
    assert runs[0].ended_at is not None


def test_external_sync_fails_closed_with_multiple_owned_runs(kanban_home):
    with kb.connect() as conn:
        kb.sync_external_task(
            conn,
            task_id="ambiguous",
            title="Ambiguous",
            body=None,
            presentation_status="ready",
            source="test",
        )
        for index in (1, 2):
            conn.execute(
                """
                INSERT INTO task_runs (
                    task_id, status, claim_lock, claim_expires, worker_pid,
                    started_at
                ) VALUES ('ambiguous', 'running', ?, 4102444800, ?, ?)
                """,
                (f"host:{index}", 900 + index, index),
            )
        conn.execute("UPDATE tasks SET status='running' WHERE id='ambiguous'")
        conn.commit()

        with pytest.raises(kb.ExternalTaskSyncConflict, match="multiple owned"):
            kb.sync_external_task(
                conn,
                task_id="ambiguous",
                title="Ambiguous",
                body=None,
                presentation_status="in-progress",
                source="test",
            )
        runs = kb.list_runs(conn, task_id="ambiguous")

    assert len(runs) == 2
    assert all(run.ended_at is None for run in runs)


def test_external_sync_reconciles_expired_claim_and_dead_pid_as_fossil(
    kanban_home,
):
    with kb.connect() as conn:
        kb.sync_external_task(
            conn,
            task_id="fossil",
            title="Fossil",
            body=None,
            presentation_status="ready",
            source="test",
        )
        fossil = conn.execute(
            """
            INSERT INTO task_runs (
                task_id, status, claim_lock, claim_expires, worker_pid,
                started_at
            ) VALUES ('fossil', 'running', 'old-host:dead', 1, 99999999, 1)
            """
        ).lastrowid
        assert fossil is not None
        conn.execute(
            """
            UPDATE tasks
               SET status='running', current_run_id=?,
                   claim_lock='old-host:dead', claim_expires=1,
                   worker_pid=99999999
             WHERE id='fossil'
            """,
            (fossil,),
        )
        conn.commit()

        result = kb.sync_external_task(
            conn,
            task_id="fossil",
            title="Fossil",
            body=None,
            presentation_status="in-progress",
            source="test",
        )
        task = kb.get_task(conn, "fossil")
        runs = kb.list_runs(conn, task_id="fossil")

    assert result.operational_status == "ready"
    assert task is not None
    assert task.current_run_id is None
    assert task.claim_lock is None
    assert task.worker_pid is None
    assert runs[0].outcome == "presentation_orphan"
    assert runs[0].ended_at is not None


def test_external_sync_does_not_treat_cross_host_pid_collision_as_live(
    kanban_home,
):
    with kb.connect() as conn:
        kb.sync_external_task(
            conn,
            task_id="cross-host",
            title="Cross-host",
            body=None,
            presentation_status="ready",
            source="test",
        )
        claim_lock = f"different-host:{os.getpid()}"
        run_id = conn.execute(
            """
            INSERT INTO task_runs (
                task_id, status, claim_lock, claim_expires, worker_pid,
                started_at
            ) VALUES ('cross-host', 'running', ?, 1, ?, 1)
            """,
            (claim_lock, os.getpid()),
        ).lastrowid
        assert run_id is not None
        conn.execute(
            """
            UPDATE tasks
               SET status='running', current_run_id=?, claim_lock=?,
                   claim_expires=1, worker_pid=?
             WHERE id='cross-host'
            """,
            (run_id, claim_lock, os.getpid()),
        )
        conn.commit()

        result = kb.sync_external_task(
            conn,
            task_id="cross-host",
            title="Cross-host",
            body=None,
            presentation_status="in-progress",
            source="test",
        )
        task = kb.get_task(conn, "cross-host")
        runs = kb.list_runs(conn, task_id="cross-host")

    assert result.operational_status == "ready"
    assert task is not None
    assert task.current_run_id is None
    assert runs[0].outcome == "presentation_orphan"
    assert runs[0].ended_at is not None


def test_external_sync_does_not_treat_pid_without_host_provenance_as_live(
    kanban_home,
):
    with kb.connect() as conn:
        kb.sync_external_task(
            conn,
            task_id="pid-only",
            title="PID only",
            body=None,
            presentation_status="ready",
            source="test",
        )
        run_id = conn.execute(
            """
            INSERT INTO task_runs (task_id, status, worker_pid, started_at)
            VALUES ('pid-only', 'running', ?, 1)
            """,
            (os.getpid(),),
        ).lastrowid
        assert run_id is not None
        conn.execute(
            """
            UPDATE tasks
               SET status='running', current_run_id=?, worker_pid=?
             WHERE id='pid-only'
            """,
            (run_id, os.getpid()),
        )
        conn.commit()

        result = kb.sync_external_task(
            conn,
            task_id="pid-only",
            title="PID only",
            body=None,
            presentation_status="in-progress",
            source="test",
        )
        runs = kb.list_runs(conn, task_id="pid-only")

    assert result.operational_status == "ready"
    assert runs[0].outcome == "presentation_orphan"
    assert runs[0].ended_at is not None


def test_task_row_live_ownership_repairs_and_preserves_pointed_naked_run(
    kanban_home,
):
    with kb.connect() as conn:
        kb.sync_external_task(
            conn,
            task_id="task-owned",
            title="Task-owned",
            body=None,
            presentation_status="ready",
            source="test",
        )
        run_id = conn.execute(
            "INSERT INTO task_runs (task_id, status, started_at) "
            "VALUES ('task-owned', 'running', 1)"
        ).lastrowid
        assert run_id is not None
        claim_lock = kb._claimer_id()
        conn.execute(
            """
            UPDATE tasks
               SET status='running', current_run_id=?, claim_lock=?,
                   claim_expires=4102444800, worker_pid=?
             WHERE id='task-owned'
            """,
            (run_id, claim_lock, os.getpid()),
        )
        conn.commit()

        result = kb.sync_external_task(
            conn,
            task_id="task-owned",
            title="Task-owned",
            body=None,
            presentation_status="in-progress",
            source="test",
        )
        task = kb.get_task(conn, "task-owned")
        runs = kb.list_runs(conn, task_id="task-owned")

    assert result.operational_status == "running"
    assert task is not None
    assert task.current_run_id == run_id
    assert runs[0].ended_at is None
    assert runs[0].claim_lock == claim_lock
    assert runs[0].worker_pid == os.getpid()


def test_task_row_live_ownership_without_open_pointed_run_fails_closed(
    kanban_home,
):
    with kb.connect() as conn:
        kb.sync_external_task(
            conn,
            task_id="task-owned-missing",
            title="Task-owned missing",
            body=None,
            presentation_status="ready",
            source="test",
        )
        conn.execute(
            """
            UPDATE tasks
               SET status='running', current_run_id=999999, claim_lock=?,
                   claim_expires=4102444800, worker_pid=?
             WHERE id='task-owned-missing'
            """,
            (kb._claimer_id(), os.getpid()),
        )
        conn.commit()

        with pytest.raises(kb.ExternalTaskSyncConflict, match="no open pointed run"):
            kb.sync_external_task(
                conn,
                task_id="task-owned-missing",
                title="Task-owned missing",
                body=None,
                presentation_status="in-progress",
                source="test",
            )
        task = kb.get_task(conn, "task-owned-missing")

    assert task is not None
    assert task.status == "running"
    assert task.current_run_id == 999999
    assert task.claim_lock is not None


def test_external_sync_is_idempotent(kanban_home):
    with kb.connect() as conn:
        first = kb.sync_external_task(
            conn,
            task_id="same",
            title="Same",
            body="same body",
            presentation_status="ready",
            source="test",
            idempotency_key="external:same",
        )
        before_events = len(kb.list_events(conn, "same"))
        second = kb.sync_external_task(
            conn,
            task_id="same",
            title="Same",
            body="same body",
            presentation_status="ready",
            source="test",
            idempotency_key="external:same",
        )
        after_events = len(kb.list_events(conn, "same"))

    assert first.changed is True
    assert second.changed is False
    assert after_events == before_events


def test_completion_reconciles_every_open_run(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="reconcile")
        conn.execute(
            """
            INSERT INTO task_runs (task_id, status, started_at)
            VALUES (?, 'running', 1)
            """,
            (task_id,),
        )
        conn.commit()
        claimed = kb.claim_task(conn, task_id, claimer="host:current")
        assert claimed is not None
        authoritative_run_id = claimed.current_run_id
        assert authoritative_run_id is not None

        assert kb.complete_task(conn, task_id, result="done")
        runs = kb.list_runs(conn, task_id=task_id)

    by_id = {run.id: run for run in runs}
    assert by_id[authoritative_run_id].outcome == "completed"
    assert by_id[authoritative_run_id].ended_at is not None
    stale = [run for run in runs if run.id != authoritative_run_id]
    assert len(stale) == 1
    assert stale[0].outcome == "superseded_terminal"
    assert stale[0].ended_at is not None


def test_external_done_reconciles_naked_presentation_run(kanban_home):
    with kb.connect() as conn:
        kb.sync_external_task(
            conn,
            task_id="ghost",
            title="Ghost",
            body=None,
            presentation_status="ready",
            source="test",
        )
        run_cur = conn.execute(
            "INSERT INTO task_runs (task_id, status, started_at) "
            "VALUES ('ghost', 'running', 1)"
        )
        conn.execute(
            "UPDATE tasks SET status='running', current_run_id=? WHERE id='ghost'",
            (run_cur.lastrowid,),
        )
        conn.commit()

        result = kb.sync_external_task(
            conn,
            task_id="ghost",
            title="Ghost",
            body="finished externally",
            presentation_status="done",
            source="test",
        )
        task = kb.get_task(conn, "ghost")
        runs = kb.list_runs(conn, task_id="ghost")

    assert result.operational_status == "done"
    assert task is not None
    assert task.status == "done"
    assert task.external_status == "done"
    assert task.current_run_id is None
    assert all(run.ended_at is not None for run in runs)
    assert any(run.outcome == "superseded_terminal" for run in runs)
    assert any(run.outcome == "completed" for run in runs)


def test_external_done_fails_closed_while_owned_worker_is_active(kanban_home):
    with kb.connect() as conn:
        kb.sync_external_task(
            conn,
            task_id="owned-terminal",
            title="Owned terminal",
            body=None,
            presentation_status="ready",
            source="test",
        )
        claimed = kb.claim_task(conn, "owned-terminal", claimer="host:active")
        assert claimed is not None

        with pytest.raises(kb.ExternalTaskSyncConflict):
            kb.sync_external_task(
                conn,
                task_id="owned-terminal",
                title="Owned terminal",
                body=None,
                presentation_status="done",
                source="test",
            )
        task = kb.get_task(conn, "owned-terminal")
        runs = kb.list_runs(conn, task_id="owned-terminal")

    assert task is not None
    assert task.status == "running"
    assert task.external_status == "ready"
    assert len(runs) == 1
    assert runs[0].ended_at is None


def test_external_block_and_archive_use_lifecycle_transitions(kanban_home):
    with kb.connect() as conn:
        kb.sync_external_task(
            conn,
            task_id="lifecycle",
            title="Lifecycle",
            body=None,
            presentation_status="ready",
            source="test",
        )
        blocked = kb.sync_external_task(
            conn,
            task_id="lifecycle",
            title="Lifecycle",
            body="waiting",
            presentation_status="blocked",
            source="test",
        )
        assert blocked.operational_status == "blocked"
        blocked_task = kb.get_task(conn, "lifecycle")
        assert blocked_task is not None
        assert blocked_task.status == "blocked"

        archived = kb.sync_external_task(
            conn,
            task_id="lifecycle",
            title="Lifecycle",
            body="closed",
            presentation_status="archived",
            source="test",
        )
        task = kb.get_task(conn, "lifecycle")

    assert archived.operational_status == "archived"
    assert task is not None
    assert task.status == "archived"
    assert task.external_status == "archived"


@pytest.mark.parametrize("external_status", ["blocked", "archived"])
def test_external_stop_states_close_naked_runs(kanban_home, external_status):
    with kb.connect() as conn:
        task_id = f"naked-{external_status}"
        kb.sync_external_task(
            conn,
            task_id=task_id,
            title="Naked stop",
            body=None,
            presentation_status="ready",
            source="test",
        )
        run_id = conn.execute(
            "INSERT INTO task_runs (task_id, status, started_at) "
            "VALUES (?, 'running', 1)",
            (task_id,),
        ).lastrowid
        assert run_id is not None
        conn.execute(
            "UPDATE tasks SET status='running', current_run_id=? WHERE id=?",
            (run_id, task_id),
        )
        conn.commit()

        kb.sync_external_task(
            conn,
            task_id=task_id,
            title="Naked stop",
            body=None,
            presentation_status=external_status,
            source="test",
        )
        runs = kb.list_runs(conn, task_id=task_id)

    assert runs
    assert all(run.ended_at is not None for run in runs)
    assert any(run.outcome == "superseded_terminal" for run in runs)


@pytest.mark.parametrize("external_status", ["blocked", "done", "archived"])
def test_unchanged_external_terminal_state_closes_fossil_open_run(
    kanban_home,
    external_status,
):
    task_id = f"unchanged-{external_status}"
    with kb.connect() as conn:
        kb.sync_external_task(
            conn,
            task_id=task_id,
            title="Unchanged terminal",
            body=None,
            presentation_status=external_status,
            source="test",
        )
        fossil_id = conn.execute(
            """
            INSERT INTO task_runs (
                task_id, status, claim_lock, claim_expires, worker_pid,
                started_at
            ) VALUES (?, 'running', 'old-host:dead', 1, 99999999, 1)
            """,
            (task_id,),
        ).lastrowid
        assert fossil_id is not None
        conn.execute(
            """
            UPDATE tasks
               SET current_run_id=?, claim_lock='old-host:dead',
                   claim_expires=1, worker_pid=99999999
             WHERE id=?
            """,
            (fossil_id, task_id),
        )
        conn.commit()

        kb.sync_external_task(
            conn,
            task_id=task_id,
            title="Unchanged terminal",
            body=None,
            presentation_status=external_status,
            source="test",
        )
        task = kb.get_task(conn, task_id)
        runs = kb.list_runs(conn, task_id=task_id)

    assert task is not None
    assert task.current_run_id is None
    fossil = next(run for run in runs if run.id == fossil_id)
    assert fossil.ended_at is not None
    assert fossil.outcome == "superseded_terminal"
