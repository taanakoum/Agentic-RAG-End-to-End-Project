"""Upload, processing status, failure details, retry, re-index and delete."""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend import state
from frontend.api import ApiError
from frontend.config import FrontendConfig
from frontend.errors import describe
from frontend.format import ACCEPTED_TYPES, has_work_in_flight, human_size, plural, status_label


def _upload_box() -> None:
    uploaded = st.file_uploader(
        "Upload a document",
        type=ACCEPTED_TYPES,
        key="uploader",
        help="PDF, DOCX, TXT, Markdown or CSV. The server checks the real file type itself.",
    )
    if uploaded is None:
        return
    st.caption(f"{uploaded.name}: {human_size(uploaded.size)}")
    if not st.button("Upload", key="upload_button"):
        return
    with st.spinner("Uploading..."):
        try:
            result = state.api().upload(uploaded.name, uploaded.getvalue(), uploaded.type)
        except ApiError as exc:
            state.fail(exc)
            return
    # the backend has accepted it: now (and only now) it is a document in the list
    if result["duplicate"]:
        st.info("You already uploaded this exact file; nothing was processed again.")
    else:
        st.success("Uploaded. It is being processed; this page shows the progress.")
        # work is in flight now: switch polling on in THIS run (a later full re-run would wipe
        # the message above)
        st.session_state["docs_in_flight"] = True
    st.session_state.pop("uploader", None)


def _failure(document: dict[str, Any]) -> None:
    error = document.get("error")
    if not error:
        return
    presentation = describe_document_error(error)
    st.error(presentation)
    cols = st.columns(2)
    if error.get("recoverable") and document.get("latest_job_id"):
        if cols[0].button("Retry", key=f"retry_{document['id']}"):
            try:
                state.api().retry_ingestion(document["latest_job_id"])
            except ApiError as exc:
                state.fail(exc)
            else:
                st.session_state["docs_in_flight"] = True
                st.rerun()
    else:
        cols[0].caption("This failure is permanent; fix the file and upload it again.")


def describe_document_error(error: dict[str, Any]) -> str:
    """A failed document's stored error, in the same wording as an upload rejection."""
    from frontend.api import ApiError as _Error

    shown = describe(
        _Error(status=422, code=str(error.get("code", "")), message=str(error.get("message", "")))
    )
    return shown.message


def _row(document: dict[str, Any]) -> None:
    status = document["status"]
    icon = {"completed": "✅", "failed": "❌", "deleting": "🗑️"}.get(status, "⏳")
    with st.container(border=True):
        head, action = st.columns([5, 2])
        head.markdown(f"{icon} **{document['filename']}**")
        meta = [status_label(status), human_size(document["size_bytes"])]
        if document.get("page_count"):
            meta.append(plural(document["page_count"], "page"))
        if document.get("chunk_count"):
            meta.append(plural(document["chunk_count"], "passage"))
        if document.get("ocr_status") == "used":
            meta.append(f"OCR on {plural(document['ocr_pages'], 'page')}")
        head.caption(" · ".join(meta))
        if status == "failed":
            _failure(document)
        if status in {"completed", "failed"}:
            confirm = action.checkbox("Confirm", key=f"ok_{document['id']}")
            if action.button("Delete", key=f"del_{document['id']}", disabled=not confirm):
                try:
                    state.api().delete_document(document["id"])
                except ApiError as exc:
                    state.fail(exc)
                else:
                    st.rerun()
            if status == "completed" and action.button("Re-index", key=f"reindex_{document['id']}"):
                try:
                    state.api().reindex(document["id"])
                except ApiError as exc:
                    state.fail(exc)
                else:
                    st.session_state["docs_in_flight"] = True
                    st.rerun()


def _listing() -> None:
    try:
        documents = state.api().list_documents()["items"]
    except ApiError as exc:
        state.fail(exc)
        return
    if not documents:
        st.caption("No documents yet. Upload one above to start asking questions about it.")
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
    _upload_box()
    interval = FrontendConfig.from_env().poll_interval_s
    # live progress: this fragment re-runs by itself, but only while something is processing
    run_every = interval if st.session_state.get("docs_in_flight", True) else None

    @st.fragment(run_every=run_every)
    def listing() -> None:
        _listing()
        if st.button("Refresh", key="refresh_docs"):
            st.rerun(scope="fragment")

    listing()
