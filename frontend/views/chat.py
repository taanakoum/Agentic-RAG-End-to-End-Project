"""Conversations, streamed answers with citations, and feedback."""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend import state
from frontend.api import ApiError, SessionExpired
from frontend.errors import Presentation, describe, stream_error
from frontend.format import (
    STAGE_LABEL,
    answer_notices,
    citation_line,
    preview_title,
    safe_markdown,
)

SUGGESTIONS = [
    "Summarise my documents in five bullet points",
    "What deadlines or dates are mentioned?",
    "What are the main obligations or rules?",
]


def _active() -> str | None:
    return st.session_state.get("active_session")


def _select(session_id: str | None) -> None:
    st.session_state["active_session"] = session_id
    st.session_state.pop("inflight", None)
    st.session_state.pop("feedback_open", None)


# ------------------------------------------------------------------ sidebar: conversations


def _sidebar() -> list[dict[str, Any]]:
    client = state.api()
    with st.sidebar:
        if st.button(
            "New conversation",
            key="new_chat",
            icon=":material/add_comment:",
            type="primary",
            use_container_width=True,
        ):
            _select(None)
            st.rerun()
        st.caption("Conversations")
        try:
            sessions = client.list_sessions()["items"]
        except ApiError as exc:
            state.fail(exc)
            return []
        if not sessions:
            st.caption("No conversations yet. Ask a question to start one.")
        for item in sessions:
            selected = item["id"] == _active()
            if st.button(
                preview_title(item["title"]),
                key=f"open_{item['id']}",
                icon=":material/chat_bubble:" if selected else ":material/chat_bubble_outline:",
                type="secondary" if selected else "tertiary",
                use_container_width=True,
            ):
                _select(item["id"])
                st.rerun()
        return sessions


def _settings(session: dict[str, Any]) -> None:
    """Rename, delete and the few agent options a user may change (no model or prompt)."""
    client = state.api()
    config = session.get("agent_config", {})
    with st.expander("Conversation settings", icon=":material/tune:"):
        title = st.text_input("Title", value=session["title"], key=f"title_{session['id']}")
        left, right = st.columns(2)
        top_k = left.number_input(
            "Passages to retrieve (top-k, 0 = default)",
            min_value=0,
            max_value=30,
            value=int(config.get("top_k") or 0),
            key=f"topk_{session['id']}",
        )
        right.space("small")
        rerank = right.checkbox(
            "Re-rank passages with the cross-encoder",
            value=True if config.get("rerank") is None else bool(config["rerank"]),
            key=f"rerank_{session['id']}",
        )
        if st.button("Save settings", key=f"save_{session['id']}", icon=":material/save:"):
            try:
                client.update_session(
                    session["id"],
                    title=title,
                    agent_config={"mode": "single", "top_k": int(top_k) or None, "rerank": rerank},
                )
            except ApiError as exc:
                state.fail(exc)
            else:
                st.rerun()
        st.divider()
        confirm = st.checkbox("I want to delete this conversation", key=f"del_ok_{session['id']}")
        if st.button(
            "Delete conversation",
            key=f"del_{session['id']}",
            disabled=not confirm,
            icon=":material/delete:",
        ):
            try:
                client.delete_session(session["id"])
            except ApiError as exc:
                state.fail(exc)
            else:
                _select(None)
                st.rerun()


# ------------------------------------------------------------------ rendering an answer


def _feedback(message_id: str, stored: dict[str, Any] | None = None) -> None:
    ratings: dict[str, str] = st.session_state.setdefault("ratings", {})
    if stored and message_id not in ratings:
        ratings[message_id] = str(stored["rating"])  # given earlier (possibly in another session)
    current = ratings.get(message_id)
    up, down, note = st.columns([1, 1, 10], vertical_alignment="center")
    chosen: str | None = None
    if up.button(
        "👍",
        key=f"up_{message_id}",
        type="primary" if current == "up" else "tertiary",
        help="Helpful",
    ):
        chosen = "up"
    if down.button(
        "👎",
        key=f"down_{message_id}",
        type="primary" if current == "down" else "tertiary",
        help="Not helpful",
    ):
        chosen = "down"
    if current and st.session_state.get("feedback_open") != message_id:
        note.caption("Thanks for rating this answer.")
    if chosen:
        try:
            state.api().send_feedback(message_id, chosen)
        except ApiError as exc:
            state.fail(exc)
            return
        ratings[message_id] = chosen
        st.session_state["feedback_open"] = message_id
        st.rerun()
    if current and st.session_state.get("feedback_open") == message_id:
        comment = st.text_input("Add a comment (optional)", key=f"comment_{message_id}")
        if st.button("Send comment", key=f"send_comment_{message_id}") and comment.strip():
            try:
                state.api().send_feedback(message_id, current, comment.strip())
            except ApiError as exc:
                state.fail(exc)
            else:
                st.session_state.pop("feedback_open", None)
                st.success("Thanks for the feedback.", icon=":material/check_circle:")


def _sources(citations: list[dict[str, Any]]) -> None:
    with st.expander(f"Sources ({len(citations)})", icon=":material/menu_book:"):
        for index, citation in enumerate(citations, start=1):
            st.markdown(
                f":material/description: **{safe_markdown(citation_line(index, citation))}**"
            )
            st.caption(safe_markdown(citation["excerpt"]))


def _answer(
    response: dict[str, Any], message_id: str | None, stored: dict[str, Any] | None = None
) -> None:
    for level, text in answer_notices(response):
        if level == "warning":
            st.warning(text, icon=":material/gpp_maybe:")
        else:
            st.info(text, icon=":material/info:")
    # an unverifiable answer was already replaced server-side; images are never loaded
    st.markdown(safe_markdown(response["answer"]))
    citations = response.get("citations", [])
    if citations:
        _sources(citations)
    follow_ups = response.get("follow_up_questions", [])
    if follow_ups and message_id:
        st.caption("Ask next")
        for number, follow_up in enumerate(follow_ups):
            if st.button(
                follow_up,
                key=f"fu_{message_id}_{number}",
                type="tertiary",
                icon=":material/subdirectory_arrow_right:",
            ):
                st.session_state["queued_message"] = follow_up
                st.rerun()
    if message_id:
        _feedback(message_id, stored)


def _history(session_id: str) -> int:
    try:
        messages = state.api().messages(session_id)
    except ApiError as exc:
        state.fail(exc)
        return 0
    for message in messages:
        with st.chat_message(message["role"]):
            if message["role"] == "assistant" and message.get("response"):
                _answer(message["response"], message["id"], message.get("feedback"))
            else:
                st.markdown(safe_markdown(message["content"]))
    return len(messages)


# ------------------------------------------------------------------ sending


def _stream(session_id: str, text: str, key: str) -> Presentation | None:
    """Run one streamed turn. Returns the problem to show (with retry state kept), or None."""
    status_box = st.empty()
    answer_box = st.empty()
    status_box.caption(":material/hourglass_top: Sending your question...")
    shown = ""
    try:
        for event in state.api().chat_stream(session_id, text, key):
            kind = event.get("type")
            if kind == "status":
                label = STAGE_LABEL.get(str(event["stage"]), "")
                status_box.caption(f":material/progress_activity: {label}")
            elif kind == "token":
                shown += str(event["text"])
                answer_box.markdown(safe_markdown(shown) + "▌")
            elif kind == "error":
                status_box.empty()
                answer_box.empty()
                return stream_error(
                    str(event["code"]),
                    str(event.get("message", "")),
                    bool(event.get("retryable", False)),
                    event.get("retry_after_s"),
                )
            elif kind == "complete":
                # only now has the backend validated AND saved the answer
                status_box.empty()
                answer_box.empty()
                return None
    except SessionExpired as exc:
        state.fail(exc)  # signs out and returns to the sign-in page
    except ApiError as exc:
        status_box.empty()
        answer_box.empty()
        return describe(exc)
    return stream_error("STREAM_INTERRUPTED", "", True, None)


def _send(session_id: str | None, text: str) -> None:
    client = state.api()
    if session_id is None:
        try:
            created = client.create_session(title=preview_title(text))
        except ApiError as exc:
            state.fail(exc)
            return
        session_id = created["id"]
        _select(session_id)
    st.session_state["inflight"] = {
        "session": session_id,
        "text": text,
        "key": client.new_idempotency_key(),
    }


def _run_inflight() -> None:
    inflight = st.session_state.get("inflight")
    if not inflight:
        return
    problem: Presentation | None = inflight.get("problem")
    if problem is None:
        with st.chat_message("user"):
            st.markdown(safe_markdown(inflight["text"]))
        with st.chat_message("assistant"):
            outcome = _stream(inflight["session"], inflight["text"], inflight["key"])
        if outcome is None:
            st.session_state.pop("inflight", None)
            st.rerun()  # reload the saved history from the server
        inflight["problem"] = outcome
        st.rerun()
    # a failed attempt waits for an explicit decision; other interactions never re-ask
    state.show(problem)
    st.caption(f"Your message: {inflight['text']}")
    if problem.retryable and st.button("Try again", key="retry_send", icon=":material/refresh:"):
        if not problem.same_key_retry:
            # a failed turn cannot be replayed under its key: this is a new request
            inflight["key"] = state.api().new_idempotency_key()
        inflight["problem"] = None
        st.rerun()
    elif not problem.retryable:
        st.session_state.pop("inflight", None)


def _resolve_active(sessions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The open conversation, or None. An id that is not the person's own (forged client state, a
    deleted conversation) is dropped; the backend is the judge, not the client."""
    active = _active()
    if not active:
        return None
    listed = next((s for s in sessions if s["id"] == active), None)
    if listed is not None:
        return listed
    try:
        return state.api().get_session(active)
    except ApiError as exc:
        if exc.status not in {403, 404, 422}:
            state.fail(exc)
        _select(None)
        return None


def _welcome() -> None:
    with st.container(border=True):
        st.markdown("#### :material/waving_hand: What would you like to know?")
        st.caption("Ask a question about your uploaded documents.")
        for column, suggestion in zip(st.columns(len(SUGGESTIONS)), SUGGESTIONS, strict=True):
            if column.button(
                suggestion, key=f"suggest_{SUGGESTIONS.index(suggestion)}", use_container_width=True
            ):
                st.session_state["queued_message"] = suggestion
                st.rerun()


# ------------------------------------------------------------------ page


def render() -> None:
    sessions = _sidebar()
    current = _resolve_active(sessions)
    active = _active()
    if current:
        st.subheader(f":material/forum: {current['title']}")
    else:
        st.title("New conversation")
    if current:
        _settings(current)
        _history(current["id"])
    elif not st.session_state.get("inflight"):
        _welcome()
    queued = st.session_state.pop("queued_message", None)
    prompt = st.chat_input("Ask about your documents") or queued
    if prompt and not st.session_state.get("inflight"):
        _send(active, str(prompt))
        st.rerun()
    _run_inflight()
