"""Walk through the running app in a REAL headless browser, take screenshots, write a report.

Needs the stack running (python scripts/stack.py start) and Playwright, which is not a project
dependency; run it in a throw-away environment:

    uvx --python 3.12 --from playwright playwright install chromium     # once, ~200 MB
    uvx --python 3.12 --from playwright python scripts/browser_tour.py --out docs/images

    # your own documents and questions, plus a JSON report of timings and answers:
    uvx --python 3.12 --from playwright python scripts/browser_tour.py \\
        --docs data/real_docs/*.pdf --question "What is ...?" --question "How many ...?" \\
        --report evaluation/reports/browser_tour.json

Steps: register a throw-away account; sign out and back in with "keep me signed in" and prove a
page reload keeps the session; upload the documents (screenshot while they are processed); wait
until every document is Ready; ask each question in a new conversation and record the answer, its
citations and the time it took; visit the memory and account pages. Exit code 0 = all steps worked.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "examples" / "acme"
PASSWORD = "A-long-Unusual-passphrase-42!"  # noqa: S105  (a throw-away demo password)


def step(message: str) -> None:
    print(f"  {message}", flush=True)


def shoot(page: Page, out: Path, name: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):  # headless Chromium occasionally fails a capture: try again
        try:
            page.screenshot(path=str(out / f"{name}.png"), full_page=False)
            break
        except PlaywrightError:
            if attempt == 2:
                raise
            page.wait_for_timeout(1000)
    step(f"screenshot {name}.png")


def open_section(page: Page, name: str) -> None:
    """Navigation labels carry an icon, so match the radio option by its text, not exactly."""
    group = page.locator("[data-testid=stSidebar]").get_by_role("radiogroup")
    group.get_by_text(name).first.click(timeout=30_000)
    page.wait_for_timeout(1500)


def api_documents(api: str, email: str) -> list[dict[str, Any]]:
    """The same account's documents straight from the API (for exact processing times)."""
    body = json.dumps({"email": email, "password": PASSWORD}).encode()
    login = urllib.request.Request(  # noqa: S310  (the local API URL given on the command line)
        f"{api}/auth/login", data=body, headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(login, timeout=30) as response:  # noqa: S310  (local URL)
        token = json.loads(response.read())["access_token"]
    listing = urllib.request.Request(  # noqa: S310
        f"{api}/documents?limit=100", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(listing, timeout=30) as response:  # noqa: S310
        return list(json.loads(response.read())["items"])


def sign_in(page: Page, email: str, *, keep: bool) -> None:
    panel = page.locator("[role=tabpanel]:visible")
    panel.get_by_label("Email", exact=True).fill(email)
    panel.get_by_label("Password", exact=True).fill(PASSWORD)
    if keep:
        panel.get_by_text("Keep me signed in on this device").click()
    panel.get_by_role("button", name="Sign in").click()
    expect(page.locator("[data-testid=stSidebar]").get_by_text("Signed in as")).to_be_visible()


def processing_seconds(document: dict[str, Any]) -> float | None:
    if not document.get("completed_at"):
        return None
    took = datetime.fromisoformat(document["completed_at"]) - datetime.fromisoformat(
        document["created_at"]
    )
    return round(took.total_seconds(), 1)


def tour(args: argparse.Namespace) -> int:
    email = f"tour-{uuid.uuid4().hex[:8]}@example.com"
    out: Path = args.out
    wait_s: int = args.wait
    files = sorted(Path(p) for p in args.docs) if args.docs else sorted(SAMPLES.iterdir())
    report: dict[str, Any] = {"account": email, "documents": [], "questions": []}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page(viewport={"width": 1360, "height": 900})
        page.set_default_timeout(wait_s * 1000)
        sidebar = page.locator("[data-testid=stSidebar]")

        print("1. sign-in page")
        page.goto(args.url)
        expect(page.get_by_role("tab", name="Create account")).to_be_visible()
        page.wait_for_timeout(1000)
        shoot(page, out, "01-sign-in")

        print("2. register, sign in again with 'keep me signed in', reload the page")
        page.get_by_role("tab", name="Create account").click()
        panel = page.locator("[role=tabpanel]:visible")
        panel.get_by_label("Email").fill(email)
        panel.get_by_label("Password", exact=True).fill(PASSWORD)
        panel.get_by_label("Repeat password").fill(PASSWORD)
        panel.get_by_role("button", name="Create account").click()
        expect(sidebar.get_by_text("Signed in as")).to_be_visible()
        sidebar.get_by_role("button", name="Sign out").click()
        expect(page.get_by_role("tab", name="Sign in")).to_be_visible()
        sign_in(page, email, keep=True)
        page.wait_for_timeout(1500)  # let the cookie script run
        page.reload()
        expect(sidebar.get_by_text("Signed in as")).to_be_visible()
        report["kept_signed_in_after_reload"] = True
        step(f"signed in as {email}; still signed in after a reload")
        expect(page.get_by_text("What would you like to know?")).to_be_visible()
        page.wait_for_timeout(800)
        shoot(page, out, "02-chat-welcome")

        print("3. upload the documents")
        open_section(page, "Documents")
        started = time.monotonic()
        for path in files:
            page.locator("input[type=file]").set_input_files(str(path))
            # Streamlit receives the file asynchronously: wait until the app echoes it
            expect(page.get_by_text(f"{path.name}: ")).to_be_visible()
            page.get_by_role("button", name="Upload", exact=True).click()
            expect(
                page.get_by_text("Uploaded.").or_(page.get_by_text("already uploaded"))
            ).to_be_visible(timeout=wait_s * 1000)
            step(f"uploaded {path.name} ({path.stat().st_size / 1e6:.1f} MB)")
        page.wait_for_timeout(2500)
        if page.locator("[data-testid=stProgress]").count():
            shoot(page, out, "03-documents-processing")
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            items = api_documents(args.api, email)
            if items and all(d["status"] in {"completed", "failed"} for d in items):
                break
            page.wait_for_timeout(3000)
        else:
            shoot(page, out, "03-documents-timeout")
            print("FAIL: documents did not finish processing in time")
            return 1
        page.wait_for_timeout(3500)  # the page polls every 2 s
        report["all_ready_after_s"] = round(time.monotonic() - started, 1)
        for document in api_documents(args.api, email):
            took = processing_seconds(document)
            report["documents"].append(
                {
                    "file": document["filename"],
                    "status": document["status"],
                    "pages": document.get("page_count"),
                    "passages": document.get("chunk_count"),
                    "parser": document.get("parser"),
                    "ocr_pages": document.get("ocr_pages"),
                    "processing_s": took,
                    "error": document.get("error"),
                }
            )
            step(
                f"{document['filename']}: {document['status']}, "
                f"{document.get('page_count')} pages, "
                f"{document.get('chunk_count')} passages, {took} s"
            )
        shoot(page, out, "04-documents-ready")

        print("4. ask the questions")
        open_section(page, "Chat")
        for number, question in enumerate(args.question, start=1):
            sidebar.get_by_role("button", name="New conversation").click()
            page.wait_for_timeout(1200)
            box = page.locator("[data-testid=stChatInput] textarea")
            box.fill(question)
            asked = time.monotonic()
            box.press("Enter")
            answer = page.locator("[data-testid=stChatMessage]").nth(1)
            try:
                expect(answer.get_by_role("button", name="👍")).to_be_visible(timeout=wait_s * 1000)
            except AssertionError:
                shoot(page, out, f"q{number}-failure")
                print(f"FAIL: no answer to question {number}")
                return 1
            took = round(time.monotonic() - asked, 1)
            sources = answer.get_by_text("Sources (")
            citations: list[str] = []
            if sources.count():
                sources.first.click()
                page.wait_for_timeout(700)
                panel_text = answer.locator("[data-testid=stExpander]").first.inner_text()
                citations = [
                    " ".join(line.split()).replace("description ", "", 1)
                    for line in panel_text.splitlines()
                    if line.strip().startswith(("[", "description ["))
                ]
            markdown = answer.locator("[data-testid=stMarkdownContainer]")
            text = " ".join(markdown.first.inner_text().split()) if markdown.count() else ""
            report["questions"].append(
                {"question": question, "answer": text, "citations": citations, "seconds": took}
            )
            step(f"Q{number} ({took} s): {text[:160]}")
            for citation in citations:
                step(f"     source: {citation}")
            if number == 1:
                shoot(page, out, "05-answer-with-sources")
            elif number == len(args.question):
                shoot(page, out, "06-another-answer")

        print("5. memory and account pages")
        open_section(page, "Memory")
        shoot(page, out, "07-memory")
        open_section(page, "Account")
        shoot(page, out, "08-account")
        browser.close()
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
        step(f"report written to {args.report}")
    print("OK: the whole tour worked in a real browser")
    return 0


def main() -> int:
    # answers can contain any Unicode (maths symbols from PDFs); Windows consoles are not UTF-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--url", default="http://127.0.0.1:8501")
    parser.add_argument("--api", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "images")
    parser.add_argument("--docs", nargs="*", help="files to upload (default: examples/acme/*)")
    parser.add_argument("--question", action="append", help="repeat for several questions")
    parser.add_argument("--report", type=Path, help="write timings, answers and citations as JSON")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--wait", type=int, default=600, help="seconds to wait for slow steps")
    args = parser.parse_args()
    if not args.question:
        args.question = ["How many days of annual leave do full-time employees get?"]
    return tour(args)


if __name__ == "__main__":
    sys.exit(main())
