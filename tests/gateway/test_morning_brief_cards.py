import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.morning_brief_cards import (
    ActionJobStore,
    CardStore,
    CardWorkflowStore,
    apply_action,
    build_cards_from_brief_items,
    callback_for,
    create_reference_fixture_files,
    keyboard_spec,
    parse_callback,
    reference_case_items,
    render_action_job_result_message,
    render_card_text,
    run_next_action_job,
)
from gateway.platforms.telegram import TelegramAdapter


@pytest.fixture()
def fixture_cards(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    source_dir = tmp_path / "sources"
    create_reference_fixture_files(str(source_dir))
    cards = build_cards_from_brief_items(reference_case_items(str(source_dir)), brief_date="2026-06-12")
    store = CardStore()
    for card in cards:
        store.upsert(card)
    return cards, store, source_dir


def test_card_rendering_and_keyboard_for_mvp_reference_cases(fixture_cards):
    cards, _store, _source_dir = fixture_cards
    assert [c.card_type for c in cards] == ["approval", "permission", "choice"]

    approval = cards[0]
    text = render_card_text(approval)
    assert "<b>Audit email monitoring gap</b>" in text
    assert "Decision needed:" in text
    keyboard = keyboard_spec(approval)
    flattened = [button["text"] for row in keyboard for button in row]
    assert flattened == ["Open note", "Accept", "Push back", "Later"]
    assert all(len((button.get("callback_data") or "").encode("utf-8")) <= 64 for row in keyboard for button in row if button.get("callback_data"))


def test_callback_parsing_success_and_malformed_safety(fixture_cards):
    cards, store, _source_dir = fixture_cards
    cb = callback_for(cards[2].interaction_id, "choose", "asco")
    assert parse_callback(cb) == (cards[2].interaction_id, "choose", "asco")

    result = apply_action("mb:not-valid", user_display="Steve", store=store)
    assert result["ok"] is False
    assert result["status"] == "malformed"
    assert "Malformed" in result["error"]


def test_success_actions_mutate_state_and_write_back(fixture_cards):
    cards, store, source_dir = fixture_cards
    approval = cards[0]
    cb = callback_for(approval.interaction_id, "accept")
    result = apply_action(cb, user_display="Steve", store=store, now="2026-06-12T08:14:00+00:00")

    assert result["ok"] is True
    updated = store.get(approval.interaction_id)
    assert updated.state == "accepted"
    assert "Status: accepted" in render_card_text(updated)
    note = (source_dir / "audit-email-monitoring-gap.md").read_text()
    assert "Telegram morning-brief card" in note
    assert "`pending` → `accepted`" in note


def test_duplicate_tap_is_idempotent(fixture_cards):
    cards, store, source_dir = fixture_cards
    permission = cards[1]
    cb = callback_for(permission.interaction_id, "approve_cleanup")

    first = apply_action(cb, user_display="Steve", store=store, now="2026-06-12T08:15:00+00:00")
    second = apply_action(cb, user_display="Steve", store=store, now="2026-06-12T08:16:00+00:00")

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["duplicate"] is True
    note = (source_dir / "duplicate-task-node-sync-conflict-cleanup.md").read_text()
    assert note.count("action `approve_cleanup`") == 1


def test_repeated_open_tap_is_idempotent(fixture_cards):
    cards, store, source_dir = fixture_cards
    choice = cards[2]
    cb = callback_for(choice.interaction_id, "open")

    first = apply_action(cb, user_display="Steve", store=store, now="2026-06-12T08:15:00+00:00")
    second = apply_action(cb, user_display="Steve", store=store, now="2026-06-12T08:16:00+00:00")

    assert first["ok"] is True
    assert first["status"] == "opened"
    assert second["ok"] is True
    assert second["duplicate"] is True
    updated = store.get(choice.interaction_id)
    assert updated.state == "opened"
    note = (source_dir / "ai-readiness-warm-validation-shortlist.md").read_text()
    assert note.count("action `open`") == 1


def test_card_workflow_advances_only_after_terminal_answer(fixture_cards):
    cards, store, _source_dir = fixture_cards
    workflow_store = CardWorkflowStore()
    workflow_store.create(cards=cards, chat_id="123")

    open_result = apply_action(callback_for(cards[0].interaction_id, "open"), user_display="Steve", store=store)
    workflow, next_card = workflow_store.advance_after_terminal_card(cards[0].interaction_id, card_store=store)
    assert workflow is not None
    assert open_result["status"] == "opened"
    assert workflow["current_index"] == 0
    assert next_card is None

    accept_result = apply_action(callback_for(cards[0].interaction_id, "accept"), user_display="Steve", store=store)
    workflow, next_card = workflow_store.advance_after_terminal_card(cards[0].interaction_id, card_store=store)
    assert workflow is not None
    assert next_card is not None
    assert accept_result["status"] == "accepted"
    assert workflow["current_index"] == 1
    assert next_card.interaction_id == cards[1].interaction_id


def test_deferral_policy_is_recorded(fixture_cards):
    cards, store, source_dir = fixture_cards
    approval = cards[0]
    result = apply_action(callback_for(approval.interaction_id, "later"), user_display="Steve", store=store)
    assert result["status"] == "deferred"
    updated = store.get(approval.interaction_id)
    assert updated.resurfacing_policy == "resurface_next_morning_pending_review"
    assert "Resurfacing policy" in (source_dir / "audit-email-monitoring-gap.md").read_text()


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.from_user = SimpleNamespace(id="123", first_name="Steve")
        self.message = SimpleNamespace(text="old", chat_id=123, message_id=55, chat=SimpleNamespace(type="private"), message_thread_id=None)
        self.answers = []
        self.edits = []

    async def answer(self, text=None):
        self.answers.append(text)

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)


@pytest.mark.asyncio
async def test_telegram_callback_handler_edits_message_and_removes_terminal_buttons(fixture_cards):
    cards, store, monkey_source_dir = fixture_cards
    cb = callback_for(cards[0].interaction_id, "accept")

    fake_self = SimpleNamespace(
        name="telegram-test",
        _is_callback_user_authorized=lambda *args, **kwargs: True,
        send_morning_brief_card=lambda *args, **kwargs: None,
    )
    query = FakeQuery(cb)
    await TelegramAdapter._handle_morning_brief_card_callback(
        fake_self,
        query,
        cb,
        query_chat_id=123,
        query_chat_type="private",
        query_thread_id=None,
        query_user_name="Steve",
    )

    assert query.answers
    assert query.edits
    assert "Status: accepted" in query.edits[-1]["text"]
    assert query.edits[-1]["reply_markup"] is None


@pytest.mark.asyncio
async def test_telegram_callback_handler_sends_next_workflow_card(fixture_cards):
    cards, store, _source_dir = fixture_cards
    CardWorkflowStore().create(cards=cards, chat_id="123")
    sent = []

    async def fake_send_morning_brief_card(chat_id, card, metadata=None):
        sent.append((chat_id, card.interaction_id, metadata))
        return SimpleNamespace(success=True, error=None)

    fake_self = SimpleNamespace(
        name="telegram-test",
        _is_callback_user_authorized=lambda *args, **kwargs: True,
        send_morning_brief_card=fake_send_morning_brief_card,
    )
    cb = callback_for(cards[0].interaction_id, "accept")
    query = FakeQuery(cb)

    await TelegramAdapter._handle_morning_brief_card_callback(
        fake_self,
        query,
        cb,
        query_chat_id=123,
        query_chat_type="private",
        query_thread_id=None,
        query_user_name="Steve",
    )

    assert sent == [("123", cards[1].interaction_id, None)]


def test_stale_callback_fails_without_writeback(fixture_cards):
    _cards, store, _source_dir = fixture_cards
    result = apply_action("mb:missing12345:accept", user_display="Steve", store=store)
    assert result["ok"] is False
    assert result["status"] == "stale"
    assert "no longer available" in result["error"]


@pytest.mark.asyncio
async def test_morning_brief_mb_prefix_does_not_route_to_model_picker(fixture_cards):
    cards, _store, _source_dir = fixture_cards
    cb = callback_for(cards[2].interaction_id, "choose", "asco")
    calls = []

    async def morning_handler(query, data, **kwargs):
        calls.append(("morning", data, kwargs))

    async def model_handler(query, data, chat_id):  # pragma: no cover - should not run
        raise AssertionError("morning-brief mb: callback was routed to model picker")

    fake_self = SimpleNamespace(
        _handle_morning_brief_card_callback=morning_handler,
        _handle_model_picker_callback=model_handler,
    )
    query = FakeQuery(cb)
    update = SimpleNamespace(callback_query=query)

    await TelegramAdapter._handle_callback_query(fake_self, update, None)

    assert calls
    assert calls[0][0] == "morning"
    assert calls[0][1] == cb



def test_terminal_action_creates_durable_action_job_once(fixture_cards):
    cards, store, _source_dir = fixture_cards
    permission = cards[1]
    cb = callback_for(permission.interaction_id, "approve_cleanup")

    first = apply_action(cb, user_display="Steve", store=store, now="2026-06-12T09:00:00+00:00")
    second = apply_action(cb, user_display="Steve", store=store, now="2026-06-12T09:01:00+00:00")

    assert first["ok"] is True
    assert first["action_job"]["queued"] is True
    assert second["duplicate"] is True
    jobs = ActionJobStore().list()
    assert len(jobs) == 1
    assert jobs[0].source_card_id == permission.interaction_id
    assert jobs[0].job_type == "cleanup_task_sync_conflict"
    assert jobs[0].status == "queued"


def test_action_worker_processes_job_and_writes_result(fixture_cards):
    cards, store, source_dir = fixture_cards
    permission = cards[1]
    cb = callback_for(permission.interaction_id, "approve_cleanup")
    result = apply_action(cb, user_display="Steve", store=store, now="2026-06-12T09:02:00+00:00")
    assert result["action_job"]["queued"] is True

    job = run_next_action_job(store=ActionJobStore())
    assert job is not None
    assert job.status == "succeeded"
    assert "Safe bounded cleanup action recorded" in (job.result_summary or "")
    note = (source_dir / "duplicate-task-node-sync-conflict-cleanup.md").read_text()
    assert f"Action job `{job.job_id}`" in note
    assert "cleanup_task_sync_conflict" in note
    msg = render_action_job_result_message(job)
    assert msg and msg.startswith("Done:")


def test_choice_action_worker_creates_followup_artifact(fixture_cards):
    cards, store, source_dir = fixture_cards
    choice = cards[2]
    cb = callback_for(choice.interaction_id, "choose", "asco")
    result = apply_action(cb, user_display="Steve", store=store, now="2026-06-12T09:03:00+00:00")
    assert result["action_job"]["queued"] is True

    job = run_next_action_job(store=ActionJobStore())
    assert job is not None
    assert job.status == "succeeded"
    assert job.result_paths
    out = Path(job.result_paths[0])
    assert out.exists()
    text = out.read_text()
    assert "ASCO follow-on" in text
    assert str(out).startswith(str(source_dir))


@pytest.mark.asyncio
async def test_telegram_callback_kicks_worker_after_job_enqueue(fixture_cards):
    cards, _store, _source_dir = fixture_cards
    kicked = []
    fake_self = SimpleNamespace(
        name="telegram-test",
        _is_callback_user_authorized=lambda *args, **kwargs: True,
        _kick_morning_brief_action_worker=lambda: kicked.append(True),
        send_morning_brief_card=lambda *args, **kwargs: None,
    )
    cb = callback_for(cards[1].interaction_id, "approve_cleanup")
    query = FakeQuery(cb)

    await TelegramAdapter._handle_morning_brief_card_callback(
        fake_self,
        query,
        cb,
        query_chat_id=123,
        query_chat_type="private",
        query_thread_id=None,
        query_user_name="Steve",
    )

    assert kicked == [True]
    assert query.edits[-1]["reply_markup"] is None


def test_worker_blocks_unregistered_job_type(fixture_cards):
    cards, store, _source_dir = fixture_cards
    approval = cards[0]
    approval.action_jobs = {"accept": {"job_type": "not_registered", "title": "Unknown job"}}
    store.update(approval)
    result = apply_action(callback_for(approval.interaction_id, "accept"), user_display="Steve", store=store)
    assert result["action_job"]["queued"] is True

    job = run_next_action_job(store=ActionJobStore())
    assert job is not None
    assert job.status == "blocked_needs_approval"
    assert "No handler is registered" in (job.result_summary or "")



def test_prepare_duplicate_task_node_cleanup_plan_handler(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    canonical = tmp_path / "wiki" / "tasks" / "task.md"
    duplicate = tmp_path / "wiki" / "tasks" / "task (1).md"
    outdir = tmp_path / "Hudson Outputs"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("""---
title: Test
status: archived
kanban_status: archived
kanban_card_id: t_same
---
# Test

## Notes and progress

- already captured
""", encoding="utf-8")
    duplicate.write_text("""---
title: Test
status: active
kanban_status: triage
kanban_card_id: t_same
---
# Test

## Notes and progress

- already captured
""", encoding="utf-8")
    card = build_cards_from_brief_items([{
        "title": "Prepare duplicate cleanup plan",
        "item_key": "prepare-duplicate-cleanup-plan",
        "card_type": "permission",
        "summary": "Prepare only.",
        "decision_prompt": "Approve preparing the cleanup plan.",
        "open_target": {"path": str(canonical), "kind": "task_note"},
        "write_back_target": {"type": "task_note", "path": str(canonical)},
        "action_jobs": {"approve_cleanup": {
            "job_type": "prepare_duplicate_task_node_cleanup_plan",
            "input": {
                "canonical_path": str(canonical),
                "duplicate_path": str(duplicate),
                "plan_output_dir": str(outdir),
                "allowed_paths": [str(tmp_path)],
            },
        }},
    }], brief_date="2026-06-12")[0]
    store = CardStore()
    store.upsert(card)
    result = apply_action(callback_for(card.interaction_id, "approve_cleanup"), user_display="Steve", store=store)
    assert result["action_job"]["queued"] is True
    job = run_next_action_job(store=ActionJobStore())
    assert job.status == "succeeded"
    assert job.result_paths
    plan = Path(job.result_paths[0])
    assert plan.exists()
    text = plan.read_text()
    assert "No real task file was moved or edited" in text
    assert "t_same" in text
    assert canonical.exists()
    assert duplicate.exists()



def test_apply_duplicate_task_node_cleanup_moves_duplicate_to_quarantine(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    canonical = tmp_path / "wiki" / "tasks" / "task.md"
    duplicate = tmp_path / "wiki" / "tasks" / "task (1).md"
    quarantine = tmp_path / "Hudson Outputs" / "quarantine"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("""---
title: Test
status: archived
kanban_status: archived
kanban_card_id: t_same
---
# Test

## Notes and progress

- already captured
""", encoding="utf-8")
    duplicate.write_text("""---
title: Test
status: active
kanban_status: triage
kanban_card_id: t_same
---
# Test

## Notes and progress

- already captured
""", encoding="utf-8")
    card = build_cards_from_brief_items([{
        "title": "Apply duplicate cleanup",
        "item_key": "apply-duplicate-cleanup",
        "card_type": "permission",
        "summary": "Apply after approval.",
        "decision_prompt": "Approve applying the cleanup.",
        "open_target": {"path": str(canonical), "kind": "task_note"},
        "write_back_target": {"type": "task_note", "path": str(canonical)},
        "action_jobs": {"approve_cleanup": {
            "job_type": "apply_duplicate_task_node_cleanup",
            "input": {
                "canonical_path": str(canonical),
                "duplicate_path": str(duplicate),
                "quarantine_dir": str(quarantine),
                "run_sync": False,
                "allowed_paths": [str(tmp_path)],
            },
        }},
    }], brief_date="2026-06-12")[0]
    store = CardStore()
    store.upsert(card)
    result = apply_action(callback_for(card.interaction_id, "approve_cleanup"), user_display="Steve", store=store)
    assert result["action_job"]["queued"] is True
    job = run_next_action_job(store=ActionJobStore())
    assert job.status == "succeeded"
    assert not duplicate.exists()
    moved = Path(job.result_paths[0])
    assert moved.exists()
    assert moved.parent == quarantine
    assert canonical.exists()
