"""Visual layer: brand, CSS and small presentational helpers shared by the views.

Colours and radii come from `.streamlit/config.toml`; this file only adds what the theme cannot
express (content width, card and badge styling, a calmer chat layout). No user or model text is
ever passed through here as HTML: everything user-provided is rendered by Streamlit's escaped
markdown.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import streamlit as st

ASSETS = Path(__file__).resolve().parent / "assets"
LOGO = ASSETS / "logo.svg"
ICON = ASSETS / "icon.svg"

NAV_ICONS = {
    "Chat": ":material/forum:",
    "Documents": ":material/description:",
    "Memory": ":material/psychology:",
    "Account": ":material/manage_accounts:",
}

FILE_ICONS = {
    "pdf": ":material/picture_as_pdf:",
    "docx": ":material/article:",
    "csv": ":material/table_chart:",
    "md": ":material/notes:",
    "markdown": ":material/notes:",
    "txt": ":material/text_snippet:",
}

BadgeColor = Literal["green", "red", "gray", "blue"]
STATUS_COLORS: dict[str, BadgeColor] = {"completed": "green", "failed": "red", "deleting": "gray"}

_CSS = """
<style>
/* readable line length on wide screens */
.block-container { max-width: 1120px; padding-top: 2.2rem; padding-bottom: 6rem; }
/* headings: a little tighter and darker */
h1 { font-weight: 700; letter-spacing: -0.02em; }
h2, h3 { letter-spacing: -0.01em; }
/* bordered containers as soft cards */
div[data-testid="stVerticalBlockBorderWrapper"] {
  background: #FFFFFF;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04), 0 1px 1px rgba(15, 23, 42, 0.02);
}
/* chat: assistant answers on a light panel, the user's question plain */
div[data-testid="stChatMessage"] { padding: 0.9rem 1rem; border-radius: 0.8rem; }
div[data-testid="stChatMessage"]:has(div[data-testid="stChatMessageAvatarAssistant"]) {
  background: #F8FAFF; border: 1px solid #E6EAF5;
}
/* the sources expander inside an answer */
div[data-testid="stChatMessage"] details { background: #FFFFFF; }
/* sidebar: brand spacing and full-width buttons look like a list */
section[data-testid="stSidebar"] .stButton button { justify-content: flex-start; text-align: left; }
section[data-testid="stSidebar"] hr { margin: 0.6rem 0; }
/* metric tiles */
div[data-testid="stMetric"] {
  background: #F4F6FB; border: 1px solid #E2E8F0; border-radius: 0.65rem; padding: 0.6rem 0.9rem;
}
/* hide the "made with Streamlit" footer */
footer { visibility: hidden; }
</style>
"""


def apply() -> None:
    """Brand and styles; call once per run, before anything else is drawn."""
    st.logo(str(LOGO), icon_image=str(ICON), size="large")
    st.html(_CSS)


def nav_label(section: str) -> str:
    return f"{NAV_ICONS.get(section, '')} {section}".strip()


def file_icon(filename: str) -> str:
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return FILE_ICONS.get(extension, ":material/draft:")


def status_color(status: str) -> BadgeColor:
    return STATUS_COLORS.get(status, "blue")


def initials(name: str) -> str:
    """Two letters for the avatar: "dana.smith@x.com" -> "DS", "Dana" -> "DA"."""
    base = name.split("@", 1)[0]
    parts = [p for p in base.replace(".", " ").replace("_", " ").replace("-", " ").split() if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return (base[:2] or "?").upper()
