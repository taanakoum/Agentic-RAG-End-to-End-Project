"""Walk through the running app in a REAL headless browser and take screenshots.

Needs the stack running (python scripts/stack.py start) and Playwright, which is not a project
dependency; run it in a throw-away environment:

    uvx --python 3.12 --from playwright playwright install chromium     # once, ~200 MB
    uvx --python 3.12 --from playwright python scripts/browser_tour.py --out docs/images

It registers a new account, uploads the sample documents of examples/acme, asks a question and
checks the visible result, saving PNG screenshots. Exit code 0 means every step worked.
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

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
    page.locator("[data-testid=stSidebar]").get_by_text(name, exact=True).first.click()
    page.wait_for_timeout(1200)


def tour(url: str, out: Path, question: str, headed: bool, wait_s: int) -> int:
    email = f"tour-{uuid.uuid4().hex[:8]}@example.com"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        page = browser.new_page(viewport={"width": 1320, "height": 900})
        page.set_default_timeout(wait_s * 1000)

        print("1. sign-in page")
        page.goto(url)
        expect(page.get_by_text("Agentic RAG").first).to_be_visible()
        shoot(page, out, "01-sign-in")

        print("2. register")
        page.get_by_role("tab", name="Create account").click()
        panel = page.locator("[role=tabpanel]:visible")
        panel.get_by_label("Email", exact=True).fill(email)
        panel.get_by_label("Password", exact=True).fill(PASSWORD)
        panel.get_by_label("Repeat password").fill(PASSWORD)
        panel.get_by_role("button", name="Create account").click()
        expect(page.locator("[data-testid=stSidebar]").get_by_text("Signed in as")).to_be_visible()
        step(f"signed in as {email}")

        print("3. upload the sample documents")
        open_section(page, "Documents")
        files = sorted(SAMPLES.iterdir())
        for path in files:
            page.locator("input[type=file]").set_input_files(str(path))
            # Streamlit receives the file asynchronously: wait until the app echoes it
            expect(page.get_by_text(f"{path.name}: ")).to_be_visible()
            page.get_by_role("button", name="Upload", exact=True).click()
            expect(
                page.get_by_text("Uploaded.").or_(page.get_by_text("already uploaded"))
            ).to_be_visible(timeout=wait_s * 1000)
            step(f"uploaded {path.name}")
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if page.locator("text=/Ready/").count() >= len(files):
                break
            page.wait_for_timeout(2000)
        else:
            shoot(page, out, "03-documents-timeout")
            print("FAIL: documents did not become Ready in time")
            return 1
        shoot(page, out, "02-documents-ready")

        print("4. ask a question")
        open_section(page, "Chat")
        box = page.locator("[data-testid=stChatInput] textarea")
        box.fill(question)
        box.press("Enter")
        try:
            expect(page.get_by_text("Sources (").first).to_be_visible(timeout=wait_s * 1000)
        except AssertionError:
            shoot(page, out, "04-chat-failure")
            print("FAIL: no answer with sources appeared. The page says:")
            print("   ", " ".join(page.locator("[data-testid=stMain]").inner_text().split())[:600])
            return 1
        page.get_by_text("Sources (").first.click()
        page.wait_for_timeout(800)
        shoot(page, out, "03-answer-with-sources")
        text = page.locator("[data-testid=stChatMessage]").last.inner_text()
        print("   assistant said:", " ".join(text.split())[:240])

        print("5. memory and account pages")
        open_section(page, "Memory")
        shoot(page, out, "04-memory")
        open_section(page, "Account")
        shoot(page, out, "05-account")
        browser.close()
    print("OK: the whole tour worked in a real browser")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--url", default="http://127.0.0.1:8501")
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "images")
    parser.add_argument(
        "--question", default="How many days of annual leave do full-time employees get?"
    )
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--wait", type=int, default=240, help="seconds to wait for slow steps")
    args = parser.parse_args()
    return tour(args.url, args.out, args.question, args.headed, args.wait)


if __name__ == "__main__":
    sys.exit(main())
