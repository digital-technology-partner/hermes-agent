"""Morning-brief Telegram action cards.

Focused MVP support for Hudson's morning brief interaction layer.  This is
intentionally narrower than a generic card framework: approval, choice and
permission cards, compact Telegram callbacks, durable state, and append-only
write-back/audit logging.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

try:
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover - import fallback for isolated tests
    def get_hermes_home() -> Path:  # type: ignore
        return Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))


MVP_CARD_TYPES = {"approval", "choice", "permission"}
CARD_STATES = {
    "pending",
    "opened",
    "accepted",
    "pushed_back",
    "deferred",
    "approved",
    "held",
    "discussion_requested",
    "selected",
    "failed",
}
TERMINAL_STATES = {
    "accepted",
    "pushed_back",
    "deferred",
    "approved",
    "held",
    "discussion_requested",
    "selected",
    "failed",
}
NON_TERMINAL_STATES = {"pending", "opened"}
MVP_ACTIONS = {
    "open",
    "accept",
    "pushback",
    "later",
    "approve_cleanup",
    "hold",
    "discuss",
}
MAX_CARD_TEXT_CHARS = 1300
MAX_BUTTON_LABEL_CHARS = 36
CALLBACK_PREFIX = "mb"
CALLBACK_RE = re.compile(r"^mb:([A-Za-z0-9_-]{6,24}):([A-Za-z0-9_-]+)(?::([A-Za-z0-9_-]+))?$")


@dataclasses.dataclass
class OpenTarget:
    label: str = "Open source"
    url: Optional[str] = None
    path: Optional[str] = None
    kind: str = "unknown"
    verified: bool = False

    @classmethod
    def from_dict(cls, value: Optional[Dict[str, Any]]) -> "OpenTarget":
        if not value:
            return cls()
        return cls(
            label=str(value.get("label") or "Open source"),
            url=value.get("url") or None,
            path=value.get("path") or None,
            kind=str(value.get("kind") or "unknown"),
            verified=bool(value.get("verified")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class CardAction:
    label: str
    verb: str
    option_key: Optional[str] = None
    option_label: Optional[str] = None
    url: Optional[str] = None

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "CardAction":
        return cls(
            label=str(value.get("label") or value.get("verb") or "Action"),
            verb=str(value.get("verb") or ""),
            option_key=value.get("option_key") or None,
            option_label=value.get("option_label") or None,
            url=value.get("url") or None,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in dataclasses.asdict(self).items() if v is not None}


@dataclasses.dataclass
class WriteBackTarget:
    type: str = "audit_log"  # audit_log | markdown_file | task_note | decision_log
    path: Optional[str] = None
    section: str = "Morning brief decisions"

    @classmethod
    def from_dict(cls, value: Optional[Dict[str, Any]]) -> "WriteBackTarget":
        if not value:
            return cls()
        return cls(
            type=str(value.get("type") or "audit_log"),
            path=value.get("path") or None,
            section=str(value.get("section") or "Morning brief decisions"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class MorningBriefCard:
    interaction_id: str
    brief_date: str
    item_key: str
    source_item_type: str
    title: str
    summary: str
    decision_prompt: str
    card_type: str
    open_target: OpenTarget
    actions: List[CardAction]
    write_back_target: WriteBackTarget
    created_at: str
    state: str = "pending"
    source_object: Optional[str] = None
    selected_option: Optional[str] = None
    selected_option_label: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    telegram_message_id: Optional[str] = None
    last_action_at: Optional[str] = None
    last_action_by: Optional[str] = None
    last_error: Optional[str] = None
    resurfacing_policy: Optional[str] = None
    action_jobs: Optional[Dict[str, Any]] = None

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "MorningBriefCard":
        return cls(
            interaction_id=str(value["interaction_id"]),
            brief_date=str(value.get("brief_date") or ""),
            item_key=str(value.get("item_key") or value["interaction_id"]),
            source_item_type=str(value.get("source_item_type") or "brief_item"),
            title=str(value.get("title") or "Untitled card"),
            summary=str(value.get("summary") or ""),
            decision_prompt=str(value.get("decision_prompt") or "Decision needed."),
            card_type=str(value.get("card_type") or "approval"),
            open_target=OpenTarget.from_dict(value.get("open_target")),
            actions=[CardAction.from_dict(a) for a in value.get("actions", [])],
            write_back_target=WriteBackTarget.from_dict(value.get("write_back_target")),
            created_at=str(value.get("created_at") or utc_now_iso()),
            state=str(value.get("state") or "pending"),
            source_object=value.get("source_object") or None,
            selected_option=value.get("selected_option") or None,
            selected_option_label=value.get("selected_option_label") or None,
            telegram_chat_id=value.get("telegram_chat_id") or None,
            telegram_message_id=value.get("telegram_message_id") or None,
            last_action_at=value.get("last_action_at") or None,
            last_action_by=value.get("last_action_by") or None,
            last_error=value.get("last_error") or None,
            resurfacing_policy=value.get("resurfacing_policy") or None,
            action_jobs=value.get("action_jobs") if isinstance(value.get("action_jobs"), dict) else None,
        )

    def to_dict(self) -> Dict[str, Any]:
        data = dataclasses.asdict(self)
        data["open_target"] = self.open_target.to_dict()
        data["actions"] = [a.to_dict() for a in self.actions]
        data["write_back_target"] = self.write_back_target.to_dict()
        return data


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def state_dir() -> Path:
    path = get_hermes_home() / "state" / "morning_brief_cards"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_store_path() -> Path:
    return state_dir() / "cards.json"


def default_audit_path() -> Path:
    return state_dir() / "audit.jsonl"


def default_workflow_path() -> Path:
    return state_dir() / "workflows.json"


def make_interaction_id(brief_date: str, item_key: str) -> str:
    raw = f"{brief_date}:{item_key}".encode("utf-8", errors="replace")
    return hashlib.sha1(raw).hexdigest()[:12]


def normalise_item_key(text: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return key[:48] or "item"


def _truncate(value: str, limit: int) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def build_default_actions(card_type: str, *, options: Optional[List[Dict[str, str]]] = None, open_target: Optional[OpenTarget] = None) -> List[CardAction]:
    target = open_target or OpenTarget()
    open_label = "Open task" if card_type == "permission" else ("Open shortlist" if card_type == "choice" else "Open note")
    open_action = CardAction(open_label, "open", url=target.url)
    if card_type == "approval":
        return [open_action, CardAction("Accept", "accept"), CardAction("Push back", "pushback"), CardAction("Later", "later")]
    if card_type == "permission":
        return [open_action, CardAction("Approve cleanup", "approve_cleanup"), CardAction("Hold", "hold"), CardAction("Discuss", "discuss")]
    if card_type == "choice":
        actions = [open_action]
        for idx, option in enumerate((options or [])[:3], 1):
            key = option.get("key") or normalise_item_key(option.get("label") or f"option-{idx}")
            label = option.get("label") or f"Option {idx}"
            actions.append(CardAction(_truncate(label, MAX_BUTTON_LABEL_CHARS), "choose", option_key=key, option_label=label))
        if len(actions) == 1:
            actions.append(CardAction("Choose candidate", "choose", option_key="candidate", option_label="Candidate to confirm"))
        return actions
    raise ValueError(f"Unsupported card_type for MVP: {card_type}")


def create_card(
    *,
    title: str,
    summary: str,
    decision_prompt: str,
    card_type: str,
    brief_date: str,
    item_key: Optional[str] = None,
    source_item_type: str = "brief_item",
    source_object: Optional[str] = None,
    open_target: Optional[Dict[str, Any]] = None,
    write_back_target: Optional[Dict[str, Any]] = None,
    actions: Optional[List[Dict[str, Any]]] = None,
    options: Optional[List[Dict[str, str]]] = None,
    action_jobs: Optional[Dict[str, Any]] = None,
) -> MorningBriefCard:
    if card_type not in MVP_CARD_TYPES:
        raise ValueError(f"Unsupported MVP card type: {card_type}")
    key = item_key or normalise_item_key(title)
    interaction_id = make_interaction_id(brief_date, key)
    target = OpenTarget.from_dict(open_target)
    card_actions = [CardAction.from_dict(a) for a in actions] if actions else build_default_actions(card_type, options=options, open_target=target)
    return MorningBriefCard(
        interaction_id=interaction_id,
        brief_date=brief_date,
        item_key=key,
        source_item_type=source_item_type,
        title=title,
        summary=summary,
        decision_prompt=decision_prompt,
        card_type=card_type,
        open_target=target,
        actions=card_actions,
        write_back_target=WriteBackTarget.from_dict(write_back_target),
        created_at=utc_now_iso(),
        source_object=source_object,
        resurfacing_policy=None,
        action_jobs=action_jobs if isinstance(action_jobs, dict) else None,
    )


def callback_for(card_id: str, action: CardAction | str, option_key: Optional[str] = None) -> str:
    if isinstance(action, CardAction):
        verb = action.verb
        option = action.option_key
    else:
        verb = action
        option = option_key
    if verb == "choose" and option:
        data = f"{CALLBACK_PREFIX}:{card_id}:choose:{option}"
    else:
        data = f"{CALLBACK_PREFIX}:{card_id}:{verb}"
    if len(data.encode("utf-8")) > 64:
        raise ValueError(f"Telegram callback_data too long: {data}")
    return data


def parse_callback(data: str) -> Tuple[str, str, Optional[str]]:
    match = CALLBACK_RE.fullmatch(data or "")
    if not match:
        raise ValueError("Malformed morning-brief callback")
    card_id, verb, option = match.groups()
    if verb == "choose":
        if not option:
            raise ValueError("Choice callback missing option")
        return card_id, verb, option
    if verb not in MVP_ACTIONS:
        raise ValueError(f"Unknown action verb: {verb}")
    return card_id, verb, option


def render_card_text(card: MorningBriefCard, *, include_state: bool = True) -> str:
    parts = [f"<b>{_html_escape(card.title)}</b>"]
    if card.summary:
        parts.append(_html_escape(_truncate(card.summary, 420)))
    if card.decision_prompt:
        parts.append(f"Decision needed: {_html_escape(_truncate(card.decision_prompt, 420))}")
    if card.open_target.path and not card.open_target.url:
        parts.append(f"Open route: local fallback only — <code>{_html_escape(card.open_target.path)}</code>")
    if include_state and card.state != "pending":
        parts.append(_html_escape(render_state_line(card)))
    text = "\n\n".join(parts)
    return _truncate(text, MAX_CARD_TEXT_CHARS)


def render_state_line(card: MorningBriefCard) -> str:
    when = f" at {card.last_action_at}" if card.last_action_at else ""
    if card.state == "opened":
        return f"Status: opened{when}; awaiting decision."
    if card.state == "accepted":
        return f"Status: accepted{when}."
    if card.state == "pushed_back":
        return f"Status: pushed back{when}; item remains open for rework."
    if card.state == "deferred":
        return f"Status: deferred{when}; will resurface in the pending morning-brief review list."
    if card.state == "approved":
        return f"Status: approved{when}; Hudson may proceed within the stated boundary."
    if card.state == "held":
        return f"Status: held{when}; no action will be taken until this is revisited."
    if card.state == "discussion_requested":
        return f"Status: discussion requested{when}."
    if card.state == "selected":
        choice = card.selected_option_label or card.selected_option or "selected option"
        return f"Status: selected {choice}{when}."
    if card.state == "failed":
        return f"Status: failed{when}; {card.last_error or 'write-back failed'}"
    return f"Status: {card.state}{when}."


def _html_escape(text: str) -> str:
    import html

    return html.escape(text or "", quote=False)


def keyboard_spec(card: MorningBriefCard) -> List[List[Dict[str, str]]]:
    if card.state in TERMINAL_STATES:
        return []
    rows: List[List[Dict[str, str]]] = []
    pending_row: List[Dict[str, str]] = []
    for action in card.actions:
        label = _truncate(action.label, MAX_BUTTON_LABEL_CHARS)
        if action.verb == "open" and action.url:
            rows.append([{"text": label, "url": action.url}])
            continue
        button = {"text": label, "callback_data": callback_for(card.interaction_id, action)}
        pending_row.append(button)
        if len(pending_row) == 2:
            rows.append(pending_row)
            pending_row = []
    if pending_row:
        rows.append(pending_row)
    return rows


class CardStore:
    def __init__(self, path: Optional[Path] = None, audit_path: Optional[Path] = None):
        self.path = Path(path) if path else default_store_path()
        self.audit_path = Path(audit_path) if audit_path else default_audit_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)

    def load_all(self) -> Dict[str, MorningBriefCard]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            corrupt = self.path.with_suffix(f".corrupt-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.json")
            self.path.replace(corrupt)
            self.audit({"event": "store_corrupt", "result": "failed", "path": str(corrupt), "timestamp": utc_now_iso()})
            return {}
        return {cid: MorningBriefCard.from_dict(value) for cid, value in data.get("cards", {}).items()}

    def save_all(self, cards: Dict[str, MorningBriefCard]) -> None:
        payload = {"cards": {cid: card.to_dict() for cid, card in sorted(cards.items())}}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=self.path.name, suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp_name, self.path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

    def upsert(self, card: MorningBriefCard) -> MorningBriefCard:
        cards = self.load_all()
        cards[card.interaction_id] = card
        self.save_all(cards)
        self.audit({"event": "card_upsert", "card_id": card.interaction_id, "state": card.state, "timestamp": utc_now_iso()})
        return card

    def get(self, card_id: str) -> Optional[MorningBriefCard]:
        return self.load_all().get(card_id)

    def update(self, card: MorningBriefCard) -> None:
        cards = self.load_all()
        cards[card.interaction_id] = card
        self.save_all(cards)

    def audit(self, event: Dict[str, Any]) -> None:
        event = dict(event)
        event.setdefault("timestamp", utc_now_iso())
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n")


@dataclasses.dataclass
class ActionJob:
    job_id: str
    created_at: str
    updated_at: str
    status: str
    source_card_id: str
    source_workflow_id: Optional[str]
    brief_date: str
    trigger_action: str
    trigger_state: str
    job_type: str
    title: str
    summary: str
    source_item_type: str
    source_object: Optional[str]
    input: Dict[str, Any]
    write_back_target: Dict[str, Any]
    telegram_chat_id: Optional[str] = None
    telegram_thread_id: Optional[str] = None
    telegram_card_message_id: Optional[str] = None
    attempt_count: int = 0
    locked_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    result_summary: Optional[str] = None
    result_paths: List[str] = dataclasses.field(default_factory=list)
    error: Optional[str] = None
    subagent_session_id: Optional[str] = None
    idempotency_key: Optional[str] = None

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "ActionJob":
        return cls(
            job_id=str(value["job_id"]),
            created_at=str(value.get("created_at") or utc_now_iso()),
            updated_at=str(value.get("updated_at") or utc_now_iso()),
            status=str(value.get("status") or "queued"),
            source_card_id=str(value.get("source_card_id") or ""),
            source_workflow_id=value.get("source_workflow_id") or None,
            brief_date=str(value.get("brief_date") or ""),
            trigger_action=str(value.get("trigger_action") or ""),
            trigger_state=str(value.get("trigger_state") or ""),
            job_type=str(value.get("job_type") or ""),
            title=str(value.get("title") or "Morning-brief action job"),
            summary=str(value.get("summary") or ""),
            source_item_type=str(value.get("source_item_type") or "brief_item"),
            source_object=value.get("source_object") or None,
            input=value.get("input") if isinstance(value.get("input"), dict) else {},
            write_back_target=value.get("write_back_target") if isinstance(value.get("write_back_target"), dict) else {},
            telegram_chat_id=value.get("telegram_chat_id") or None,
            telegram_thread_id=value.get("telegram_thread_id") or None,
            telegram_card_message_id=value.get("telegram_card_message_id") or None,
            attempt_count=int(value.get("attempt_count") or 0),
            locked_at=value.get("locked_at") or None,
            started_at=value.get("started_at") or None,
            completed_at=value.get("completed_at") or None,
            result_summary=value.get("result_summary") or None,
            result_paths=list(value.get("result_paths") or []),
            error=value.get("error") or None,
            subagent_session_id=value.get("subagent_session_id") or None,
            idempotency_key=value.get("idempotency_key") or None,
        )

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


ACTION_JOB_STATES = {
    "queued",
    "claimed",
    "running",
    "succeeded",
    "failed",
    "blocked_needs_approval",
    "cancelled",
    "duplicate_ignored",
}
ACTION_JOB_TERMINAL_STATES = {"succeeded", "failed", "blocked_needs_approval", "cancelled", "duplicate_ignored"}


def default_action_job_store_path() -> Path:
    return state_dir() / "action_jobs.json"


def default_action_job_audit_path() -> Path:
    return state_dir() / "action_jobs.audit.jsonl"


def _job_id_for(idempotency_key: str) -> str:
    return hashlib.sha1(idempotency_key.encode("utf-8", errors="replace")).hexdigest()[:12]


class ActionJobStore:
    def __init__(self, path: Optional[Path] = None, audit_path: Optional[Path] = None):
        self.path = Path(path) if path else default_action_job_store_path()
        self.audit_path = Path(audit_path) if audit_path else default_action_job_audit_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)

    def load_all(self) -> Dict[str, ActionJob]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            corrupt = self.path.with_suffix(f".corrupt-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.json")
            self.path.replace(corrupt)
            self.audit({"event": "job_store_corrupt", "result": "failed", "path": str(corrupt), "timestamp": utc_now_iso()})
            return {}
        jobs = data.get("jobs", {}) if isinstance(data, dict) else {}
        return {jid: ActionJob.from_dict(value) for jid, value in jobs.items()}

    def save_all(self, jobs: Dict[str, ActionJob]) -> None:
        payload = {"jobs": {jid: job.to_dict() for jid, job in sorted(jobs.items())}}
        fd, tmp_name = tempfile.mkstemp(prefix=self.path.name, suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp_name, self.path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

    def audit(self, event: Dict[str, Any]) -> None:
        event = dict(event)
        event.setdefault("timestamp", utc_now_iso())
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n")

    def create_or_get(self, job: ActionJob) -> Tuple[ActionJob, bool]:
        jobs = self.load_all()
        for existing in jobs.values():
            if existing.idempotency_key and existing.idempotency_key == job.idempotency_key:
                self.audit({
                    "event": "duplicate_job_suppressed",
                    "job_id": existing.job_id,
                    "source_card_id": existing.source_card_id,
                    "result": "duplicate",
                })
                return existing, False
        jobs[job.job_id] = job
        self.save_all(jobs)
        self.audit({"event": "job_created", "job_id": job.job_id, "source_card_id": job.source_card_id, "result": "queued"})
        return job, True

    def get(self, job_id: str) -> Optional[ActionJob]:
        return self.load_all().get(job_id)

    def list(self, status: Optional[str] = None) -> List[ActionJob]:
        jobs = list(self.load_all().values())
        if status:
            jobs = [job for job in jobs if job.status == status]
        return sorted(jobs, key=lambda j: (j.created_at, j.job_id))

    def update(self, job: ActionJob) -> None:
        jobs = self.load_all()
        job.updated_at = utc_now_iso()
        jobs[job.job_id] = job
        self.save_all(jobs)

    def claim_next(self, *, now: Optional[str] = None) -> Optional[ActionJob]:
        now = now or utc_now_iso()
        jobs = self.load_all()
        queued = sorted([j for j in jobs.values() if j.status == "queued"], key=lambda j: (j.created_at, j.job_id))
        if not queued:
            return None
        job = queued[0]
        job.status = "claimed"
        job.locked_at = now
        job.updated_at = now
        jobs[job.job_id] = job
        self.save_all(jobs)
        self.audit({"event": "job_claimed", "job_id": job.job_id, "source_card_id": job.source_card_id, "result": "claimed", "timestamp": now})
        return job




class CardWorkflowStore:
    """Sequential queue for morning-brief card workflows.

    This stays deliberately narrower than a generic cross-channel framework: it
    tracks ordered post-brief Telegram card queues and lets the callback handler
    send the next card automatically after a terminal answer.
    """

    def __init__(self, path: Optional[Path] = None, audit_path: Optional[Path] = None):
        self.path = Path(path) if path else default_workflow_path()
        self.audit_path = Path(audit_path) if audit_path else default_audit_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)

    def load_all(self) -> Dict[str, Dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            corrupt = self.path.with_suffix(f".corrupt-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.json")
            self.path.replace(corrupt)
            CardStore(audit_path=self.audit_path).audit({"event": "workflow_store_corrupt", "result": "failed", "path": str(corrupt), "timestamp": utc_now_iso()})
            return {}
        workflows = data.get("workflows", {}) if isinstance(data, dict) else {}
        return workflows if isinstance(workflows, dict) else {}

    def save_all(self, workflows: Dict[str, Dict[str, Any]]) -> None:
        payload = {"workflows": {wid: workflows[wid] for wid in sorted(workflows)}}
        fd, tmp_name = tempfile.mkstemp(prefix=self.path.name, suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp_name, self.path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

    def create(self, *, cards: List[MorningBriefCard], chat_id: str, thread_id: Optional[str] = None, brief_date: Optional[str] = None) -> Dict[str, Any]:
        if not cards:
            raise ValueError("Cannot create a morning-brief card workflow with no cards")
        brief_date = brief_date or cards[0].brief_date
        raw = f"{brief_date}:{chat_id}:{','.join(card.interaction_id for card in cards)}".encode("utf-8", errors="replace")
        workflow_id = hashlib.sha1(raw).hexdigest()[:12]
        now = utc_now_iso()
        workflow = {
            "workflow_id": workflow_id,
            "brief_date": brief_date,
            "chat_id": str(chat_id),
            "thread_id": str(thread_id) if thread_id else None,
            "card_ids": [card.interaction_id for card in cards],
            "current_index": 0,
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }
        workflows = self.load_all()
        workflows[workflow_id] = workflow
        self.save_all(workflows)
        CardStore(audit_path=self.audit_path).audit({"event": "workflow_created", "workflow_id": workflow_id, "card_ids": workflow["card_ids"], "timestamp": now})
        return workflow

    def find_workflow_for_card(self, card_id: str) -> Optional[Dict[str, Any]]:
        for workflow in self.load_all().values():
            if card_id in list(workflow.get("card_ids") or []):
                return workflow
        return None

    def retire_stale_active_workflows(self, brief_date: str, *, reason: str = "superseded") -> List[str]:
        """Retire earlier active workflows before sending a fresh scheduled brief."""
        workflows = self.load_all()
        retired: List[str] = []
        now = utc_now_iso()
        for workflow_id, workflow in workflows.items():
            if workflow.get("status") != "active":
                continue
            if str(workflow.get("brief_date") or "") == str(brief_date):
                continue
            workflow["status"] = "retired"
            workflow["retired_at"] = now
            workflow["retired_reason"] = reason
            workflow["updated_at"] = now
            retired.append(workflow_id)
        if retired:
            self.save_all(workflows)
            CardStore(audit_path=self.audit_path).audit({"event": "workflows_retired", "workflow_ids": retired, "brief_date": brief_date, "reason": reason, "timestamp": now})
        return retired

    def advance_after_terminal_card(self, card_id: str, *, card_store: Optional[CardStore] = None) -> Tuple[Optional[Dict[str, Any]], Optional[MorningBriefCard]]:
        card_store = card_store or CardStore(audit_path=self.audit_path)
        workflows = self.load_all()
        for workflow_id, workflow in workflows.items():
            if workflow.get("status") != "active":
                continue
            card_ids = list(workflow.get("card_ids") or [])
            idx = int(workflow.get("current_index") or 0)
            if idx >= len(card_ids) or card_ids[idx] != card_id:
                continue
            card = card_store.get(card_id)
            if not card or card.state not in TERMINAL_STATES:
                return workflow, None
            next_index = idx + 1
            workflow["current_index"] = next_index
            workflow["updated_at"] = utc_now_iso()
            if next_index >= len(card_ids):
                workflow["status"] = "completed"
                self.save_all(workflows)
                card_store.audit({"event": "workflow_completed", "workflow_id": workflow_id, "card_id": card_id, "timestamp": workflow["updated_at"]})
                return workflow, None
            next_card = card_store.get(card_ids[next_index])
            self.save_all(workflows)
            card_store.audit({"event": "workflow_advanced", "workflow_id": workflow_id, "from_card_id": card_id, "next_card_id": card_ids[next_index], "timestamp": workflow["updated_at"]})
            return workflow, next_card
        return None, None


def _matching_action_job_config(card: MorningBriefCard, verb: str, option_key: Optional[str]) -> Optional[Dict[str, Any]]:
    configs = card.action_jobs if isinstance(card.action_jobs, dict) else None
    if not configs:
        return None
    keys = []
    if verb == "choose" and option_key:
        keys.extend([f"choose:{option_key}", "choose:<option_key>", "choose"])
    else:
        keys.append(verb)
    for key in keys:
        value = configs.get(key)
        if isinstance(value, dict):
            return value
    return None


def create_action_job_from_card(
    card: MorningBriefCard,
    verb: str,
    option_key: Optional[str],
    *,
    workflow: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    now: Optional[str] = None,
) -> Optional[ActionJob]:
    config = config or _matching_action_job_config(card, verb, option_key)
    if not config:
        return None
    job_type = str(config.get("job_type") or "").strip()
    if not job_type:
        return None
    now = now or utc_now_iso()
    action_key = f"choose:{option_key}" if verb == "choose" and option_key else verb
    selected_label = card.selected_option_label or option_key
    job_input: Dict[str, Any] = dict(config.get("input") or {})
    if option_key:
        job_input.setdefault("selected_option", option_key)
        job_input.setdefault("selected_option_label", selected_label)
    if card.open_target.path:
        job_input.setdefault("open_target_path", card.open_target.path)
    if card.open_target.url:
        job_input.setdefault("open_target_url", card.open_target.url)
    idempotency_key = f"{card.interaction_id}:{action_key}:{job_type}"
    job_id = _job_id_for(idempotency_key)
    return ActionJob(
        job_id=job_id,
        created_at=now,
        updated_at=now,
        status="queued",
        source_card_id=card.interaction_id,
        source_workflow_id=(workflow or {}).get("workflow_id") if workflow else None,
        brief_date=card.brief_date,
        trigger_action=action_key,
        trigger_state=card.state,
        job_type=job_type,
        title=str(config.get("title") or card.title),
        summary=str(config.get("summary") or card.summary),
        source_item_type=card.source_item_type,
        source_object=card.source_object,
        input=job_input,
        write_back_target=card.write_back_target.to_dict(),
        telegram_chat_id=card.telegram_chat_id or ((workflow or {}).get("chat_id") if workflow else None),
        telegram_thread_id=((workflow or {}).get("thread_id") if workflow else None),
        telegram_card_message_id=card.telegram_message_id,
        idempotency_key=idempotency_key,
    )


def enqueue_action_job_for_card(
    card: MorningBriefCard,
    verb: str,
    option_key: Optional[str] = None,
    *,
    store: Optional[ActionJobStore] = None,
    workflow_store: Optional[CardWorkflowStore] = None,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    if card.state not in TERMINAL_STATES:
        return {"ok": True, "queued": False, "reason": "non_terminal"}
    config = _matching_action_job_config(card, verb, option_key)
    if not config:
        return {"ok": True, "queued": False, "reason": "no_config"}
    store = store or ActionJobStore()
    workflow_store = workflow_store or CardWorkflowStore()
    workflow = workflow_store.find_workflow_for_card(card.interaction_id)
    job = create_action_job_from_card(card, verb, option_key, workflow=workflow, config=config, now=now)
    if not job:
        return {"ok": True, "queued": False, "reason": "no_job_type"}
    try:
        saved, created = store.create_or_get(job)
        return {"ok": True, "queued": created, "duplicate": not created, "job": saved, "job_id": saved.job_id}
    except Exception as exc:
        store.audit({"event": "job_create_failed", "job_id": job.job_id, "source_card_id": card.interaction_id, "result": "failed", "error": str(exc)})
        return {"ok": False, "queued": False, "error": str(exc), "job_id": job.job_id}


def _append_markdown_section(path: Path, section: str, lines: List[str]) -> None:
    if not path.exists() or path.is_dir():
        raise FileNotFoundError(f"write-back target does not exist or is not a file: {path}")
    content = path.read_text(encoding="utf-8")
    if f"## {section}" not in content:
        if not content.endswith("\n"):
            content += "\n"
        content += f"\n## {section}\n"
    if not content.endswith("\n"):
        content += "\n"
    content += "\n".join(lines) + "\n"
    path.write_text(content, encoding="utf-8")


def _validate_worker_path(path: Path, job: ActionJob) -> Optional[str]:
    text = str(path.resolve())
    if "/DTP Working Files/" in text:
        return "target is inside DTP Working Files and needs explicit approval"
    allowed = [str(Path(p).expanduser().resolve()) for p in job.input.get("allowed_paths", []) if p]
    if allowed and not any(text == a or text.startswith(a.rstrip(os.sep) + os.sep) for a in allowed):
        return "target is outside the job allowed_paths boundary"
    return None


def _job_write_back_path(job: ActionJob) -> Optional[Path]:
    target = job.write_back_target or {}
    path = target.get("path")
    return Path(os.path.expanduser(str(path))).resolve() if path else None


def _record_action_job_result(job: ActionJob, summary: str, *, status: str, result_paths: Optional[List[str]] = None) -> None:
    path = _job_write_back_path(job)
    if not path:
        return
    section = str((job.write_back_target or {}).get("section") or "Morning brief action executor")
    lines = [
        f"- {job.completed_at or utc_now_iso()} — Action job `{job.job_id}` `{job.job_type}` {status}.",
        f"  - Source card: `{job.source_card_id}`",
        f"  - Trigger: `{job.trigger_action}`",
        f"  - Result: {summary}",
    ]
    for rp in result_paths or []:
        lines.append(f"  - Output: `{rp}`")
    _append_markdown_section(path, section, lines)


def _handler_cleanup_task_sync_conflict(job: ActionJob) -> Dict[str, Any]:
    path = _job_write_back_path(job)
    if not path:
        return {"status": "blocked_needs_approval", "summary": "No write-back target was configured."}
    reason = _validate_worker_path(path, job)
    if reason:
        return {"status": "blocked_needs_approval", "summary": reason}
    summary = "Safe bounded cleanup action recorded. Real destructive cleanup remains outside this MVP handler unless explicitly scoped."
    return {"status": "succeeded", "summary": summary, "result_paths": [str(path)]}


def _handler_prepare_selected_candidate_followup(job: ActionJob) -> Dict[str, Any]:
    selected = job.input.get("selected_option_label") or job.input.get("selected_option") or "selected option"
    source = _job_write_back_path(job)
    if not source:
        return {"status": "blocked_needs_approval", "summary": "No source write-back target was configured."}
    reason = _validate_worker_path(source, job)
    if reason:
        return {"status": "blocked_needs_approval", "summary": reason}
    output_path = job.input.get("followup_path")
    if output_path:
        out = Path(os.path.expanduser(str(output_path))).resolve()
    else:
        out = source.with_name(f"{source.stem}-followup-{job.job_id}.md")
    reason = _validate_worker_path(out, job)
    if reason:
        return {"status": "blocked_needs_approval", "summary": reason}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "# Morning brief selected-candidate follow-up\n\n"
        f"- Action job: `{job.job_id}`\n"
        f"- Source card: `{job.source_card_id}`\n"
        f"- Selected option: {selected}\n"
        f"- Created: {utc_now_iso()}\n\n"
        "## Next safe step\n\n"
        "Prepare the warm-validation follow-up from this selected candidate. Do not send external messages without Steve's explicit approval.\n",
        encoding="utf-8",
    )
    return {"status": "succeeded", "summary": f"Prepared follow-up artefact for {selected}.", "result_paths": [str(out)]}


def _handler_record_acceptance_followup(job: ActionJob) -> Dict[str, Any]:
    path = _job_write_back_path(job)
    if not path:
        return {"status": "succeeded", "summary": "Acceptance recorded; no further action target configured.", "result_paths": []}
    reason = _validate_worker_path(path, job)
    if reason:
        return {"status": "blocked_needs_approval", "summary": reason}
    return {"status": "succeeded", "summary": "Acceptance follow-up recorded.", "result_paths": [str(path)]}


def _frontmatter_value(text: str, key: str) -> Optional[str]:
    m = re.search(rf"^{re.escape(key)}:\s*(.+?)\s*$", text, flags=re.MULTILINE)
    if not m:
        return None
    return m.group(1).strip().strip('"').strip("'")


def _section_text(text: str, heading: str) -> str:
    pattern = rf"^##\s+{re.escape(heading)}\s*$"
    m = re.search(pattern, text, flags=re.MULTILINE)
    if not m:
        return ""
    start = m.end()
    nxt = re.search(r"^##\s+", text[start:], flags=re.MULTILINE)
    end = start + nxt.start() if nxt else len(text)
    return text[start:end].strip()


def _handler_prepare_duplicate_task_node_cleanup_plan(job: ActionJob) -> Dict[str, Any]:
    canonical_raw = job.input.get("canonical_path")
    duplicate_raw = job.input.get("duplicate_path")
    output_dir_raw = job.input.get("plan_output_dir")
    if not canonical_raw or not duplicate_raw or not output_dir_raw:
        return {"status": "blocked_needs_approval", "summary": "Missing canonical_path, duplicate_path or plan_output_dir."}
    canonical = Path(os.path.expanduser(str(canonical_raw))).resolve()
    duplicate = Path(os.path.expanduser(str(duplicate_raw))).resolve()
    output_dir = Path(os.path.expanduser(str(output_dir_raw))).resolve()
    for target in (canonical, duplicate, output_dir):
        reason = _validate_worker_path(target, job)
        if reason:
            return {"status": "blocked_needs_approval", "summary": f"{target}: {reason}"}
    if not canonical.exists() or not canonical.is_file():
        return {"status": "blocked_needs_approval", "summary": f"Canonical task file missing: {canonical}"}
    if not duplicate.exists() or not duplicate.is_file():
        return {"status": "blocked_needs_approval", "summary": f"Duplicate task file missing: {duplicate}"}
    canonical_text = canonical.read_text(encoding="utf-8")
    duplicate_text = duplicate.read_text(encoding="utf-8")
    c_card = _frontmatter_value(canonical_text, "kanban_card_id")
    d_card = _frontmatter_value(duplicate_text, "kanban_card_id")
    c_status = _frontmatter_value(canonical_text, "status")
    d_status = _frontmatter_value(duplicate_text, "status")
    c_kanban = _frontmatter_value(canonical_text, "kanban_status")
    d_kanban = _frontmatter_value(duplicate_text, "kanban_status")
    if not c_card or c_card != d_card:
        return {"status": "blocked_needs_approval", "summary": "Canonical and duplicate do not share the same kanban_card_id; refusing cleanup plan."}
    c_notes = _section_text(canonical_text, "Notes and progress")
    d_notes = _section_text(duplicate_text, "Notes and progress")
    unique_duplicate_lines = [line for line in d_notes.splitlines() if line.strip() and line.strip() not in c_notes]
    recommendation = "Quarantine the duplicate copy outside wiki/tasks, then rerun task sync/preflight."
    if unique_duplicate_lines:
        recommendation = "Review the duplicate-only notes before quarantine; they may need copying into the canonical task first."
    output_dir.mkdir(parents=True, exist_ok=True)
    plan = output_dir / f"duplicate-task-node-cleanup-plan-{job.job_id}.md"
    plan.write_text(
        "# Duplicate task-node cleanup plan\n\n"
        f"- Action job: `{job.job_id}`\n"
        f"- Created: {utc_now_iso()}\n"
        f"- Canonical: `{canonical}`\n"
        f"- Duplicate: `{duplicate}`\n"
        f"- Shared Kanban card ID: `{c_card}`\n\n"
        "## Observed state\n\n"
        f"| File | status | kanban_status | bytes | notes lines |\n"
        f"|---|---:|---:|---:|---:|\n"
        f"| Canonical | `{c_status}` | `{c_kanban}` | {len(canonical_text.encode('utf-8'))} | {len([l for l in c_notes.splitlines() if l.strip()])} |\n"
        f"| Duplicate | `{d_status}` | `{d_kanban}` | {len(duplicate_text.encode('utf-8'))} | {len([l for l in d_notes.splitlines() if l.strip()])} |\n\n"
        "## Duplicate-only notes detected\n\n"
        + ("\n".join(f"- {line.strip()}" for line in unique_duplicate_lines) if unique_duplicate_lines else "None detected. The duplicate appears to be an older stale copy of the canonical task.")
        + "\n\n## Recommendation\n\n"
        f"{recommendation}\n\n"
        "## Proposed apply step, pending Steve approval\n\n"
        "1. Create a quarantine folder under Hudson Outputs for task-sync cleanup artefacts.\n"
        "2. Move the duplicate file out of `wiki/tasks/` into that quarantine folder, preserving the original filename.\n"
        "3. Rerun `dtp_task_management.py sync` and `preflight`.\n"
        "4. If conflicts clear, append closeout to the root-fix task and mark it done for Steve review.\n\n"
        "No real task file was moved or edited by this prepare step.\n",
        encoding="utf-8",
    )
    summary = "Prepared duplicate task-node cleanup plan. No task files were moved or edited."
    return {"status": "succeeded", "summary": summary, "result_paths": [str(plan)]}


def _handler_apply_duplicate_task_node_cleanup(job: ActionJob) -> Dict[str, Any]:
    canonical_raw = job.input.get("canonical_path")
    duplicate_raw = job.input.get("duplicate_path")
    quarantine_dir_raw = job.input.get("quarantine_dir") or job.input.get("plan_output_dir")
    if not canonical_raw or not duplicate_raw or not quarantine_dir_raw:
        return {"status": "blocked_needs_approval", "summary": "Missing canonical_path, duplicate_path or quarantine_dir."}
    canonical = Path(os.path.expanduser(str(canonical_raw))).resolve()
    duplicate = Path(os.path.expanduser(str(duplicate_raw))).resolve()
    quarantine_dir = Path(os.path.expanduser(str(quarantine_dir_raw))).resolve()
    for target in (canonical, duplicate, quarantine_dir):
        reason = _validate_worker_path(target, job)
        if reason:
            return {"status": "blocked_needs_approval", "summary": f"{target}: {reason}"}
    if not canonical.exists() or not canonical.is_file():
        return {"status": "blocked_needs_approval", "summary": f"Canonical task file missing: {canonical}"}
    if not duplicate.exists() or not duplicate.is_file():
        return {"status": "succeeded", "summary": "Duplicate task file was already absent; no move needed.", "result_paths": []}
    canonical_text = canonical.read_text(encoding="utf-8")
    duplicate_text = duplicate.read_text(encoding="utf-8")
    c_card = _frontmatter_value(canonical_text, "kanban_card_id")
    d_card = _frontmatter_value(duplicate_text, "kanban_card_id")
    if not c_card or c_card != d_card:
        return {"status": "blocked_needs_approval", "summary": "Canonical and duplicate do not share the same kanban_card_id; refusing apply cleanup."}
    c_notes = _section_text(canonical_text, "Notes and progress")
    d_notes = _section_text(duplicate_text, "Notes and progress")
    unique_duplicate_lines = [line for line in d_notes.splitlines() if line.strip() and line.strip() not in c_notes]
    if unique_duplicate_lines and not bool(job.input.get("allow_unique_duplicate_notes")):
        return {"status": "blocked_needs_approval", "summary": "Duplicate contains notes not present in canonical task; review plan before applying."}
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    destination = quarantine_dir / duplicate.name
    if destination.exists():
        destination = quarantine_dir / f"{duplicate.stem}-{job.job_id}{duplicate.suffix}"
    duplicate.replace(destination)
    sync_summary = "sync/preflight not run"
    if bool(job.input.get("run_sync", True)):
        import subprocess
        py = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
        script = Path.home() / ".hermes" / "scripts" / "dtp_task_management.py"
        proc = subprocess.run([str(py), str(script), "preflight"], text=True, capture_output=True, timeout=120)
        if proc.returncode != 0:
            return {
                "status": "failed",
                "summary": f"Moved duplicate to quarantine but preflight failed with exit {proc.returncode}.",
                "error": (proc.stderr or proc.stdout)[-1000:],
                "result_paths": [str(destination)],
            }
        try:
            parsed = json.loads(proc.stdout)
            sync = parsed.get("sync", {}) if isinstance(parsed, dict) else {}
            sync_summary = f"preflight ok={sync.get('ok')} conflicts={sync.get('conflicts')} updated_kanban={sync.get('updated_kanban')} updated_wiki={sync.get('updated_wiki')}"
            if sync.get("conflicts") not in (0, None):
                return {
                    "status": "blocked_needs_approval",
                    "summary": f"Moved duplicate to quarantine, but preflight still reports conflicts: {sync_summary}",
                    "result_paths": [str(destination)],
                }
        except Exception:
            sync_summary = "preflight completed but JSON could not be parsed"
    return {
        "status": "succeeded",
        "summary": f"Moved duplicate task file to quarantine and verified {sync_summary}.",
        "result_paths": [str(destination)],
    }


ACTION_JOB_HANDLERS = {
    "apply_duplicate_task_node_cleanup": _handler_apply_duplicate_task_node_cleanup,
    "prepare_duplicate_task_node_cleanup_plan": _handler_prepare_duplicate_task_node_cleanup_plan,
    "cleanup_task_sync_conflict": _handler_cleanup_task_sync_conflict,
    "prepare_selected_candidate_followup": _handler_prepare_selected_candidate_followup,
    "record_acceptance_followup": _handler_record_acceptance_followup,
}


def run_action_job(job: ActionJob, *, store: Optional[ActionJobStore] = None, now: Optional[str] = None) -> ActionJob:
    store = store or ActionJobStore()
    now = now or utc_now_iso()
    if job.status in ACTION_JOB_TERMINAL_STATES:
        return job
    job.status = "running"
    job.started_at = job.started_at or now
    job.attempt_count += 1
    store.update(job)
    store.audit({"event": "job_started", "job_id": job.job_id, "source_card_id": job.source_card_id, "result": "running", "timestamp": now})
    try:
        handler = ACTION_JOB_HANDLERS.get(job.job_type)
        if not handler:
            result = {"status": "blocked_needs_approval", "summary": f"No handler is registered for job type `{job.job_type}`."}
        else:
            result = handler(job)
        status = str(result.get("status") or "succeeded")
        if status not in {"succeeded", "blocked_needs_approval", "failed"}:
            status = "failed"
        summary = str(result.get("summary") or status)
        paths = [str(p) for p in result.get("result_paths") or []]
        job.status = status
        job.completed_at = utc_now_iso()
        job.result_summary = summary
        job.result_paths = paths
        job.error = str(result.get("error") or "") or None
        try:
            _record_action_job_result(job, summary, status=status, result_paths=paths)
            store.audit({"event": "write_back_succeeded", "job_id": job.job_id, "source_card_id": job.source_card_id, "result": "ok"})
        except Exception as exc:
            job.status = "failed"
            job.error = f"result write-back failed: {exc}"
            store.audit({"event": "write_back_failed", "job_id": job.job_id, "source_card_id": job.source_card_id, "result": "failed", "error": str(exc)})
        store.update(job)
        event = "job_succeeded" if job.status == "succeeded" else ("job_blocked" if job.status == "blocked_needs_approval" else "job_failed")
        store.audit({"event": event, "job_id": job.job_id, "source_card_id": job.source_card_id, "result": job.status, "error": job.error, "summary": job.result_summary})
        return job
    except Exception as exc:
        job.status = "failed"
        job.completed_at = utc_now_iso()
        job.error = str(exc)
        job.result_summary = "Action job failed."
        store.update(job)
        store.audit({"event": "job_failed", "job_id": job.job_id, "source_card_id": job.source_card_id, "result": "failed", "error": str(exc)})
        return job


def run_next_action_job(*, store: Optional[ActionJobStore] = None) -> Optional[ActionJob]:
    store = store or ActionJobStore()
    job = store.claim_next()
    if not job:
        return None
    return run_action_job(job, store=store)


def render_action_job_result_message(job: ActionJob) -> Optional[str]:
    if job.status == "succeeded":
        if not job.result_summary and not job.result_paths:
            return None
        suffix = f" Recorded in {job.result_paths[0]}." if job.result_paths else ""
        return f"Done: {job.result_summary or job.title}.{suffix}"
    if job.status == "blocked_needs_approval":
        return f"Blocked: {job.result_summary or job.error or job.title}. I need approval before continuing."
    if job.status == "failed":
        return f"Failed: {job.error or job.result_summary or job.title}. I logged the error and left the source unchanged where possible."
    return None



def state_for_action(card: MorningBriefCard, verb: str, option_key: Optional[str]) -> str:
    if verb == "open":
        return "opened"
    if verb == "accept":
        return "accepted"
    if verb == "pushback":
        return "pushed_back"
    if verb == "later":
        return "deferred"
    if verb == "approve_cleanup":
        return "approved"
    if verb == "hold":
        return "held"
    if verb == "discuss":
        return "discussion_requested"
    if verb == "choose" and option_key:
        return "selected"
    raise ValueError(f"Unsupported action: {verb}")


def apply_action(
    callback_data: str,
    *,
    user_display: str = "User",
    store: Optional[CardStore] = None,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    store = store or CardStore()
    now = now or utc_now_iso()
    try:
        card_id, verb, option_key = parse_callback(callback_data)
    except ValueError as exc:
        store.audit({"event": "callback_failure", "callback_data": callback_data, "result": "malformed", "error": str(exc), "timestamp": now})
        return {"ok": False, "error": str(exc), "status": "malformed"}

    card = store.get(card_id)
    if not card:
        store.audit({"event": "callback_failure", "card_id": card_id, "action": verb, "result": "stale", "timestamp": now})
        return {"ok": False, "error": "This morning-brief card is no longer available.", "status": "stale"}

    if card.state in TERMINAL_STATES:
        store.audit({"event": "duplicate_tap", "card_id": card_id, "action": verb, "state": card.state, "timestamp": now})
        return {"ok": True, "duplicate": True, "card": card, "label": render_state_line(card)}

    old_state = card.state
    new_state = state_for_action(card, verb, option_key)
    if verb == "open" and old_state == "opened":
        store.audit({"event": "duplicate_tap", "card_id": card_id, "action": verb, "state": card.state, "timestamp": now})
        return {"ok": True, "duplicate": True, "card": card, "label": render_state_line(card)}
    card.state = new_state
    card.last_action_at = now
    card.last_action_by = user_display
    if verb == "choose":
        card.selected_option = option_key
        for action in card.actions:
            if action.verb == "choose" and action.option_key == option_key:
                card.selected_option_label = action.option_label or action.label
                break
    if new_state == "deferred":
        card.resurfacing_policy = "resurface_next_morning_pending_review"
    elif new_state == "held":
        card.resurfacing_policy = "remain_held_until_source_changes_or_manual_review"

    write_result = write_back(card, verb, old_state=old_state)
    if not write_result.get("ok"):
        card.state = "failed"
        card.last_error = write_result.get("error", "write-back failed")
        store.update(card)
        store.audit({"event": "write_back_failure", "card_id": card_id, "action": verb, "error": card.last_error, "timestamp": now})
        return {"ok": False, "status": "failed", "card": card, "error": card.last_error}

    store.update(card)
    store.audit({"event": "action", "card_id": card_id, "action": verb, "state": card.state, "result": "ok", "timestamp": now, "user": user_display})
    action_job = None
    if card.state in TERMINAL_STATES:
        action_job = enqueue_action_job_for_card(card, verb, option_key, now=now)
        if not action_job.get("ok"):
            store.audit({"event": "action_job_enqueue_failure", "card_id": card_id, "action": verb, "result": "failed", "error": action_job.get("error"), "timestamp": now})
    return {"ok": True, "status": card.state, "card": card, "label": render_state_line(card), "action_job": action_job}


def write_back(card: MorningBriefCard, action: str, *, old_state: str) -> Dict[str, Any]:
    target = card.write_back_target
    record = decision_record_markdown(card, action, old_state=old_state)
    if target.type == "audit_log" or not target.path:
        return {"ok": True, "destination": "audit_log"}
    path = Path(os.path.expanduser(target.path))
    if not path.exists():
        return {"ok": False, "error": f"write-back target does not exist: {path}"}
    if path.is_dir():
        return {"ok": False, "error": f"write-back target is a directory: {path}"}
    try:
        content = path.read_text(encoding="utf-8")
        if f"## {target.section}" not in content:
            if not content.endswith("\n"):
                content += "\n"
            content += f"\n## {target.section}\n"
        if not content.endswith("\n"):
            content += "\n"
        content += record
        path.write_text(content, encoding="utf-8")
        return {"ok": True, "destination": str(path)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def decision_record_markdown(card: MorningBriefCard, action: str, *, old_state: str) -> str:
    lines = [
        f"- {card.last_action_at or utc_now_iso()} — Telegram morning-brief card `{card.interaction_id}` action `{action}` by {card.last_action_by or 'User'}.",
        f"  - State: `{old_state}` → `{card.state}`",
    ]
    if card.selected_option_label or card.selected_option:
        lines.append(f"  - Selected option: {card.selected_option_label or card.selected_option}")
    if card.resurfacing_policy:
        lines.append(f"  - Resurfacing policy: `{card.resurfacing_policy}`")
    return "\n".join(lines) + "\n"


def classify_brief_item(item: Dict[str, Any]) -> Optional[str]:
    """Conservative MVP classifier for post-brief card emission.

    Manual overrides win: card_type='no-card' suppresses, force_card=True with a
    supported card_type emits.  Otherwise only strongly bounded decision shapes
    become cards.
    """
    explicit = str(item.get("card_type") or "").strip().lower()
    if explicit == "no-card" or item.get("suppress_card"):
        return None
    if explicit in MVP_CARD_TYPES:
        return explicit
    text = " ".join(str(item.get(k) or "") for k in ("title", "summary", "decision_prompt", "body")).lower()
    if any(term in text for term in ("accept", "push back", "awaiting your review", "archive it")):
        return "approval"
    if any(term in text for term in ("choose", "shortlist", "candidate", "which option")):
        return "choice"
    if any(term in text for term in ("approve cleanup", "permission", "approve this", "hold", "can proceed")):
        return "permission"
    if item.get("force_card") and explicit in MVP_CARD_TYPES:
        return explicit
    return None


def resolve_open_target(source: Dict[str, Any]) -> OpenTarget:
    label = str(source.get("label") or "Open source")
    url = source.get("url")
    path = source.get("path")
    kind = str(source.get("kind") or source.get("type") or "unknown")
    verified = False
    if url and str(url).startswith(("https://", "http://", "tg://", "obsidian://")):
        verified = True
        return OpenTarget(label=label, url=str(url), path=path, kind=kind, verified=verified)
    if path:
        expanded = Path(os.path.expanduser(str(path)))
        verified = expanded.exists()
        return OpenTarget(label=label, path=str(expanded), kind=kind, verified=verified)
    return OpenTarget(label=label, kind=kind, verified=False)


def get_deferred_items(store: Optional[CardStore] = None) -> List[Dict[str, Any]]:
    """Return card-shaped brief items that should resurface in the next brief.

    Deferred morning-brief cards are intentionally conservative: only cards
    explicitly marked with the next-morning resurfacing policy are converted
    back into brief-item dictionaries, and the original card id is carried so a
    successful send can mark it as resurfaced.
    """
    store = store or CardStore()
    items: List[Dict[str, Any]] = []
    for card in store.load_all().values():
        if card.state != "deferred":
            continue
        if card.resurfacing_policy != "resurface_next_morning_pending_review":
            continue
        item: Dict[str, Any] = {
            "title": card.title,
            "item_key": f"resurfaced-{card.item_key or card.interaction_id}",
            "source_item_type": card.source_item_type,
            "source_object": card.source_object,
            "summary": card.summary,
            "decision_prompt": card.decision_prompt,
            "card_type": card.card_type,
            "open_target": card.open_target.to_dict(),
            "write_back_target": card.write_back_target.to_dict(),
            "actions": [action.to_dict() for action in card.actions],
            "action_jobs": card.action_jobs,
            "force_card": True,
            "_resurfaced_from_card_id": card.interaction_id,
        }
        if card.selected_option:
            item["previous_selection"] = card.selected_option
        items.append(item)
    return items


def mark_deferred_items_resurfaced(card_ids: Iterable[str], *, store: Optional[CardStore] = None) -> List[str]:
    """Mark deferred cards as resurfaced after their replacement card is sent."""
    store = store or CardStore()
    marked: List[str] = []
    for card_id in card_ids:
        card = store.get(str(card_id))
        if not card or card.state != "deferred":
            continue
        card.resurfacing_policy = "resurfaced_in_later_morning_brief"
        card.last_action_at = utc_now_iso()
        store.update(card)
        store.audit({"event": "deferred_card_resurfaced", "card_id": card.interaction_id, "result": "ok", "timestamp": card.last_action_at})
        marked.append(card.interaction_id)
    return marked


def build_cards_from_brief_items(
    items: Iterable[Dict[str, Any]],
    *,
    brief_date: str,
    max_cards: int = 5,
    store: Optional[CardStore] = None,
) -> List[MorningBriefCard]:
    cards: List[MorningBriefCard] = []
    for item in items:
        card_type = classify_brief_item(item)
        if not card_type:
            continue
        open_target = resolve_open_target(item.get("open_target") or {})
        options = item.get("options") if isinstance(item.get("options"), list) else None
        action_jobs = item.get("action_jobs") if isinstance(item.get("action_jobs"), dict) else None
        if not action_jobs and item.get("choice_job_type") and card_type == "choice":
            action_jobs = {"choose": {"job_type": str(item.get("choice_job_type")), "title": str(item.get("title") or "Choice follow-up")}}
        card = create_card(
            title=str(item.get("title") or "Untitled item"),
            summary=str(item.get("summary") or ""),
            decision_prompt=str(item.get("decision_prompt") or "Decision needed."),
            card_type=card_type,
            brief_date=brief_date,
            item_key=item.get("item_key") or None,
            source_item_type=str(item.get("source_item_type") or "brief_item"),
            source_object=item.get("source_object") or None,
            open_target=open_target.to_dict(),
            write_back_target=item.get("write_back_target") or None,
            actions=item.get("actions") if isinstance(item.get("actions"), list) else None,
            options=options,
            action_jobs=action_jobs,
        )
        cards.append(card)
        if len(cards) >= max_cards:
            break
    return cards


def reference_case_items(base_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """The three real DTP MVP reference cases used for dry tests."""
    base = Path(base_dir) if base_dir else state_dir() / "fixtures"
    return [
        {
            "title": "Audit email monitoring gap",
            "item_key": "audit-email-monitoring-gap",
            "card_type": "approval",
            "source_item_type": "task_note",
            "source_object": "20260611-audit-email-monitoring-gap",
            "summary": "Done. The finding is ready for Steve to accept or push back.",
            "decision_prompt": "Accept this for archive, push back if the conclusion is wrong, or defer it.",
            "open_target": {"label": "Open note", "path": str(base / "audit-email-monitoring-gap.md"), "kind": "task_note"},
            "write_back_target": {"type": "task_note", "path": str(base / "audit-email-monitoring-gap.md")},
            "action_jobs": {
                "accept": {
                    "job_type": "record_acceptance_followup",
                    "title": "Record accepted audit email monitoring gap",
                    "input": {"allowed_paths": [str(base)]},
                }
            },
        },
        {
            "title": "Duplicate task-node sync conflict cleanup",
            "item_key": "duplicate-task-node-sync-conflict-cleanup",
            "card_type": "permission",
            "source_item_type": "task_note",
            "source_object": "duplicate-task-node-sync-conflict-cleanup",
            "summary": "A bounded cleanup is available, but it touches task-state hygiene.",
            "decision_prompt": "Approve cleanup, hold it, or ask to discuss before I touch the records.",
            "open_target": {"label": "Open task", "path": str(base / "duplicate-task-node-sync-conflict-cleanup.md"), "kind": "task_note"},
            "write_back_target": {"type": "task_note", "path": str(base / "duplicate-task-node-sync-conflict-cleanup.md")},
            "action_jobs": {
                "approve_cleanup": {
                    "job_type": "cleanup_task_sync_conflict",
                    "title": "Clean up duplicate task-node sync conflict",
                    "input": {"allowed_paths": [str(base)]},
                }
            },
        },
        {
            "title": "Choose AI Readiness warm-validation candidate",
            "item_key": "choose-ai-readiness-warm-validation-candidate",
            "card_type": "choice",
            "source_item_type": "decision_note",
            "source_object": "ai-readiness-warm-validation-shortlist",
            "summary": "Pick one warm candidate for the first AI Readiness validation route.",
            "decision_prompt": "Choose the first candidate to validate, or open the shortlist first.",
            "open_target": {"label": "Open shortlist", "path": str(base / "ai-readiness-warm-validation-shortlist.md"), "kind": "decision_note"},
            "options": [
                {"key": "asco", "label": "ASCO follow-on"},
                {"key": "mdl", "label": "MDL adjacent route"},
                {"key": "warm-local", "label": "Warm local operator"},
            ],
            "write_back_target": {"type": "decision_log", "path": str(base / "ai-readiness-warm-validation-shortlist.md")},
            "action_jobs": {
                "choose": {
                    "job_type": "prepare_selected_candidate_followup",
                    "title": "Prepare selected AI Readiness warm-validation follow-up",
                    "input": {"allowed_paths": [str(base)]},
                }
            },
        },
    ]


def create_reference_fixture_files(base_dir: str) -> List[Path]:
    base = Path(base_dir)
    base.mkdir(parents=True, exist_ok=True)
    files = []
    for item in reference_case_items(str(base)):
        path = Path(item["write_back_target"]["path"])
        if not path.exists():
            path.write_text(f"# {item['title']}\n\nFixture source for Telegram morning-brief card tests.\n", encoding="utf-8")
        files.append(path)
    return files
