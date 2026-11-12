"""Long-term memory: consent switch, view, add, edit, delete and reset."""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend import state
from frontend.api import ApiError

KINDS = ["semantic", "episodic", "summary"]


def _preferences() -> None:
    try:
        prefs = state.api().preferences()
    except ApiError as exc:
        state.fail(exc)
        return
    enabled = st.toggle(
        "Let the assistant remember facts about me across conversations",
        value=bool(prefs["memory_enabled"]),
        key="memory_enabled",
        help="Off by default. Memories are only used in answers while this is on.",
    )
    days = st.number_input(
        "Keep memories for (days)",
        min_value=1,
        max_value=3650,
        value=int(prefs["memory_retention_days"]),
        key="memory_days",
    )
    changed = enabled != prefs["memory_enabled"] or int(days) != prefs["memory_retention_days"]
    if changed and st.button("Save memory settings", key="save_prefs"):
        try:
            state.api().update_preferences(memory_enabled=enabled, memory_retention_days=int(days))
        except ApiError as exc:
            state.fail(exc)
        else:
            st.rerun()


def _add_form() -> None:
    with st.form("add_memory", clear_on_submit=True):
        content = st.text_area("Something to remember", key="new_memory", max_chars=2000)
        kind = st.selectbox("Kind", KINDS, key="new_memory_kind")
        submitted = st.form_submit_button("Add memory")
    if not submitted:
        return
    if not content.strip():
        st.error("Please write what should be remembered.")
        return
    try:
        state.api().create_memory(content.strip(), kind=kind)
    except ApiError as exc:
        presentation = state.fail(exc)
        state.field_error(presentation, "content")
    else:
        st.success("Saved.")
        st.rerun()


def _item(memory: dict[str, Any]) -> None:
    with st.container(border=True):
        st.caption(f"{memory['kind']} · importance {memory['importance']} · {memory['source']}")
        edited = st.text_area(
            "Text",
            value=memory["content"],
            key=f"mem_text_{memory['id']}",
            label_visibility="collapsed",
        )
        left, right = st.columns(2)
        text = (edited or "").strip()
        unchanged = text == memory["content"]
        if left.button("Save changes", key=f"mem_save_{memory['id']}", disabled=unchanged):
            try:
                state.api().update_memory(memory["id"], content=text)
            except ApiError as exc:
                state.fail(exc)
            else:
                st.rerun()
        if right.button("Delete", key=f"mem_del_{memory['id']}"):
            try:
                state.api().delete_memory(memory["id"])
            except ApiError as exc:
                state.fail(exc)
            else:
                st.rerun()


def render() -> None:
    st.title("Memory")
    _preferences()
    st.subheader("What is remembered")
    try:
        memories = state.api().list_memories()["items"]
    except ApiError as exc:
        state.fail(exc)
        return
    if not memories:
        st.caption("Nothing is remembered yet.")
    for memory in memories:
        _item(memory)
    _add_form()
    with st.expander("Danger zone"):
        sure = st.checkbox("I want to delete ALL my memories", key="reset_ok")
        if st.button("Reset memory", key="reset_memory", disabled=not sure):
            try:
                state.api().reset_memory()
            except ApiError as exc:
                state.fail(exc)
            else:
                st.success("All memories are being deleted.")
                st.rerun()
