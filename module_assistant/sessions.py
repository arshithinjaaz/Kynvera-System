"""
Ask Kynvera conversation sessions.

Each chat lives in an AssistantChatSession. A session keeps extending while
the user is active; after SESSION_IDLE_MINUTES of silence the next message
starts a new session and the old one becomes read-only history. Reopening a
past session from the history panel and sending a message reactivates it
explicitly, regardless of age.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.models import AssistantChatMessage, AssistantChatSession, db
from module_assistant.llm import StructuredLLMError, generate_structured, is_llm_enabled

logger = logging.getLogger(__name__)

SESSION_IDLE_MINUTES = 5
TITLE_MAX_LEN = 60
SUMMARY_MAX_LEN = 140

TITLE_SYSTEM_PROMPT = (
    "You write short chat titles and one-line summaries, the same way ChatGPT, Claude, "
    "and Gemini label a conversation in a history list.\n"
    "You will get the first user message and the assistant's reply. Respond with JSON only: "
    '{"title": "...", "summary": "..."}\n'
    "- title: 3 to 6 words, plain text, no quotes, no trailing period — a clean topical "
    "headline (e.g. 'Plumbing issue at Injaaz HQ', 'Annual leave request'), not a copy of the "
    "raw message.\n"
    "- summary: one short plain sentence (under 120 characters) describing what the "
    "conversation is about.\n"
    "Respond with ONLY the JSON object, nothing else."
)

_TITLE_SCHEMA = {'required': ['title', 'summary'], 'properties': {'title': str, 'summary': str}}


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _is_live(session: AssistantChatSession) -> bool:
    if session.status != 'active' or not session.last_active_at:
        return False
    return _utcnow() - session.last_active_at <= timedelta(minutes=SESSION_IDLE_MINUTES)


def get_live_session(user) -> AssistantChatSession | None:
    """Return the user's current session if it's still within the idle window, else None."""
    session = (
        AssistantChatSession.query
        .filter_by(user_id=user.id, status='active')
        .order_by(AssistantChatSession.last_active_at.desc())
        .first()
    )
    if session and _is_live(session):
        return session
    return None


def get_owned_session(session_id, user) -> AssistantChatSession | None:
    try:
        sid = int(session_id)
    except (TypeError, ValueError):
        return None
    session = db.session.get(AssistantChatSession, sid)
    if not session or session.user_id != user.id:
        return None
    return session


def resume_or_create_session(user, requested_session_id=None) -> AssistantChatSession:
    if requested_session_id is not None:
        session = get_owned_session(requested_session_id, user)
        if session:
            touch_session(session)
            return session

    session = get_live_session(user)
    if session:
        return session

    # Idle-expired active session, if any, is now history — close it out.
    stale = (
        AssistantChatSession.query
        .filter_by(user_id=user.id, status='active')
        .order_by(AssistantChatSession.last_active_at.desc())
        .first()
    )
    if stale:
        stale.status = 'closed'
        stale.ended_at = _utcnow()

    session = AssistantChatSession(user_id=user.id, status='active')
    db.session.add(session)
    db.session.flush()
    return session


def touch_session(session: AssistantChatSession) -> None:
    session.last_active_at = _utcnow()
    session.status = 'active'
    session.ended_at = None


def _make_title(text: str) -> str:
    """Fallback title when the LLM is unavailable: a clipped word-boundary excerpt."""
    stripped = (text or '').strip()
    if len(stripped) <= TITLE_MAX_LEN:
        return stripped
    cut = stripped[:TITLE_MAX_LEN]
    last_space = cut.rfind(' ')
    if last_space > 20:
        cut = cut[:last_space]
    return cut.rstrip(' ,.;:') + '…'


def generate_title_and_summary(user_text: str, bot_text: str) -> tuple[str, str] | None:
    """Ask the LLM for a short headline + one-line summary, like ChatGPT/Claude/Gemini
    auto-titling. Returns None if the LLM is off or the call fails — caller should fall
    back to _make_title()."""
    if not is_llm_enabled():
        return None
    user_content = (
        f"User message: {(user_text or '').strip()[:600]}\n\n"
        f"Assistant reply: {(bot_text or '').strip()[:600]}"
    )
    try:
        data = generate_structured(TITLE_SYSTEM_PROMPT, user_content, _TITLE_SCHEMA)
    except StructuredLLMError as exc:
        logger.warning('Session title generation failed, falling back: %s', exc)
        return None
    title = (data.get('title') or '').strip().strip('."\'')
    summary = (data.get('summary') or '').strip()
    if not title:
        return None
    return title[:TITLE_MAX_LEN], summary[:SUMMARY_MAX_LEN]


def ensure_session_title(session: AssistantChatSession, user_text: str, bot_text: str) -> None:
    """Set the session's title (and summary) once, from its first exchange."""
    if session.title:
        return
    generated = generate_title_and_summary(user_text, bot_text)
    if generated:
        session.title, session.summary = generated
    else:
        session.title = _make_title(user_text)


def record_message(session: AssistantChatSession, role: str, text: str, payload: dict | None = None) -> None:
    db.session.add(AssistantChatMessage(
        session_id=session.id,
        role=role,
        text=text,
        payload=payload,
    ))


def close_live_session(user) -> AssistantChatSession | None:
    """Explicitly archive the user's current session (if any) to history now,
    regardless of the idle window — used by the widget's "New chat" action."""
    session = get_live_session(user)
    if session:
        session.status = 'closed'
        session.ended_at = _utcnow()
    return session
