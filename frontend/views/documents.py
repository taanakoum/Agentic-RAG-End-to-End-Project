"""Upload, processing status (with a progress bar per stage), failure details, retry, re-index
and delete."""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend import state, theme
from frontend.api import ApiError
from frontend.config import FrontendConfig
from frontend.errors import describe
from frontend.format import (
    ACCEPTED_TYPES,
    IN_FLIGHT,
    has_work_in_flight,
    human_size,
    plural,
    stage_progress,
    status_label,
)


def _upload_box() -> None:
    with st.container(border=True):
        st.markdown("##### :material/cloud_upload: Add a document")
        uploaded = st.file_uploader(
            "Upload a document",
            type=ACCEPTED_TYPES,
            key="uploader",
            help="PDF, DOCX, TXT, Markdown or CSV. The server checks the real file type itself.",
            label_visibility="collapsed",
        )
        if uploaded is None:
            st.caption("PDF, Word, Markdown, CSV or text. Scanned PDF pages are read with OCR.")
            return
        info, action = st.columns([4, 1], vertical_alignment="center")
        info.caption(f"{uploaded.name}: {human_size(uploaded.size)}")
        if not action.button(
            "Upload", key="upload_button", type="primary", use_container_width=True
        ):
            return
        with st.spinner("Uploading and checking the file..."):
            try:
                result = state.api().upload(uploaded.name, uploaded.getvalue(), uploaded.type)
            except ApiError as exc:
                state.fail(exc)
                return
        # the backend has accepted it: now (and only now) it is a document in the list
        if result["duplicate"]:
            st.info(
                "You already uploaded this exact file; nothing was processed again.",
                icon=":material/content_copy:",
            )
        else:
            st.success(
                "Uploaded. It is being processed; this page shows the progress.",
                icon=":material/check_circle:",
            )
            # work is in flight now: switch polling on in THIS run (a later full re-run would wipe
            # the message above)
            st.session_state["docs_in_flight"] = True
        st.session_state.pop("uploader", None)


def _failure(document: dict[str, Any]) -> None:
    error = document.get("error")
    if not error:
        return
    st.error(describe_document_error(error), icon=":material/error:")
    if error.get("recoverable") and document.get("latest_job_id"):
        if st.button("Retry", key=f"retry_{document['id']}", icon=":material/refresh:"):
            try:
                state.api().retry_ingestion(document["latest_job_id"])
            except ApiError as exc:
                state.fail(exc)
            else:
                st.session_state["docs_in_flight"] = True
                st.rerun()
    else:
        st.caption("This failure is permanent; fix the file and upload it again.")


def describe_document_error(error: dict[str, Any]) -> str:
    """A failed document's stored error, in the same wording as an upload rejection."""
    shown = describe(
        ApiError(status=422, code=str(error.get("code", "")), message=str(error.get("message", "")))
    )
    return shown.message


def _meta(document: dict[str, Any]) -> str:
    meta = [status_label(document["status"]), human_size(document["size_bytes"])]
    if document.get("page_count"):
        meta.append(plural(document["page_count"], "page"))
    if document.get("chunk_count"):
        meta.append(plural(document["chunk_count"], "passage"))
    if document.get("ocr_status") == "used":
        meta.append(f"OCR on {plural(document['ocr_pages'], 'page')}")
    return " · ".join(meta)


def _actions(document: dict[str, Any], column: Any) -> None:
    status = document["status"]
    if status not in {"completed", "failed"}:
        return
    confirm = column.checkbox("Confirm", key=f"ok_{document['id']}", help="Tick to enable Delete")
    if column.button(
        "Delete",
        key=f"del_{document['id']}",
        disabled=not confirm,
        icon=":material/delete:",
        use_container_width=True,
    ):
        try:
            state.api().delete_document(document["id"])
        except ApiError as exc:
            state.fail(exc)
        else:
            st.rerun()
    if status == "completed" and column.button(
        "Re-index",
        key=f"reindex_{document['id']}",
        icon=":material/autorenew:",
        use_container_width=True,
    ):
        try:
            state.api().reindex(document["id"])
        except ApiError as exc:
            state.fail(exc)
        else:
            st.session_state["docs_in_flight"] = True
            st.rerun()


def _row(document: dict[str, Any]) -> None:
    status = document["status"]
    with st.container(border=True):
        head, action = st.columns([5, 2], gap="medium")
        with head:
            title, badge = st.columns([5, 2], vertical_alignment="center")
            title.markdown(f"{theme.file_icon(document['filename'])} **{document['filename']}**")
            badge.badge(status_label(status), color=theme.status_color(status))
            st.caption(_meta(document))
            if status in IN_FLIGHT:
                st.progress(stage_progress(status), text=f"{status_label(status)}...")
            if status == "failed":
                _failure(document)
        _actions(document, action)


def _summary(documents: list[dict[str, Any]]) -> None:
    ready = sum(1 for d in documents if d["status"] == "completed")
    busy = sum(1 for d in documents if d["status"] in IN_FLIGHT)
    failed = sum(1 for d in documents if d["status"] == "failed")
    passages = sum(int(d.get("chunk_count") or 0) for d in documents if d["status"] == "completed")
    for column, (label, value) in zip(
        st.columns(4),
        (("Ready", ready), ("Processing", busy), ("Failed", failed), ("Passages", passages)),
        strict=True,
    ):
        column.metric(label, value)


def _listing() -> None:
    try:
        documents = state.api().list_documents()["items"]
    except ApiError as exc:
        state.fail(exc)
        return
    if not documents:
        st.caption("No documents yet. Upload one above to start asking questions about it.")
    else:
        _summary(documents)
    for document in documents:
        _row(document)
    busy = has_work_in_flight(documents)
    before = st.session_state.get("docs_in_flight", True)
    st.session_state["docs_in_flight"] = busy
    if busy != before:
        # the fragment's polling interval is fixed when the page is drawn, so a change (the first
        # upload, or the last document finishing) needs one full re-run to switch polling on/off
        st.rerun()


def render() -> None:
    st.title("Documents")
    st.caption("Everything you upload stays private to your account.")
    _upload_box()
    interval = FrontendConfig.from_env().poll_interval_s
    # live progress: this fragment re-runs by itself, but only while something is processing
    run_every = interval if st.session_state.get("docs_in_flight", True) else None

    @st.fragment(run_every=run_every)
    def listing() -> None:
        _listing()
        if st.button("Refresh", key="refresh_docs", icon=":material/refresh:", type="tertiary"):
            st.rerun(scope="fragment")

    listing()
