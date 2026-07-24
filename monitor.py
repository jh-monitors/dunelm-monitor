#!/usr/bin/env python3
from __future__ import annotations
import json, os, re, sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

VERSION = "DUNELM-MONITOR-PLAYWRIGHT-V2"
CONFIG_PATH = Path("config.json")
STATE_PATH = Path("state.json")
DIAG_DIR = Path("diagnostics")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def send_discord(webhook: str, content: str, username: str) -> None:
    body = json.dumps({"username": username, "content": content}).encode()
    req = Request(webhook, data=body, method="POST", headers={"Content-Type":"application/json", "User-Agent":"DunelmStockMonitor/2.0"})
    with urlopen(req, timeout=20) as r:
        if r.status not in (200, 204):
            raise RuntimeError(f"Discord returned HTTP {r.status}")


def normalise(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def detect_with_browser(url: str) -> tuple[bool | None, str, str]:
    DIAG_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="en-GB",
            timezone_id="Europe/London",
            viewport={"width": 1440, "height": 1200},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
        )
        page = context.new_page()
        response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
        if response and response.status >= 400:
            raise RuntimeError(f"Dunelm returned HTTP {response.status}")
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(5000)

        title = page.title()
        body_text = normalise(page.locator("body").inner_text(timeout=15000))
        html = page.content()

        # Save diagnostics on every run; workflow only uploads them on failure.
        page.screenshot(path=str(DIAG_DIR / "dunelm-page.png"), full_page=True)
        (DIAG_DIR / "dunelm-page.html").write_text(html, encoding="utf-8")
        (DIAG_DIR / "dunelm-body.txt").write_text(body_text, encoding="utf-8")

        if "access denied" in normalise(title) or "access denied" in body_text[:1000]:
            browser.close()
            raise RuntimeError("Dunelm returned an Access Denied page")

        positive_phrases = [
            "add to basket", "add to bag", "available for delivery",
            "available for home delivery", "choose delivery", "in stock"
        ]
        negative_phrases = [
            "out of stock", "currently unavailable", "unavailable online",
            "notify me when back in stock", "email me when back in stock"
        ]

        # Prefer visible, enabled purchase buttons.
        button_details: list[str] = []
        for selector in ["button", "[role=button]", "input[type=submit]"]:
            loc = page.locator(selector)
            count = min(loc.count(), 200)
            for i in range(count):
                el = loc.nth(i)
                try:
                    text = normalise(el.inner_text(timeout=1000) or el.get_attribute("value") or "")
                    if not text:
                        continue
                    if any(p in text for p in positive_phrases):
                        enabled = el.is_enabled() and el.is_visible()
                        button_details.append(f"{text} (enabled={enabled})")
                        if enabled:
                            browser.close()
                            return True, f"enabled purchase control: {text}", body_text
                except Exception:
                    continue

        # Structured availability is useful if present after rendering.
        raw = html.lower()
        if "schema.org/instock" in raw or '"availability":"instock"' in raw:
            browser.close()
            return True, "structured availability: InStock", body_text
        if "schema.org/outofstock" in raw or '"availability":"outofstock"' in raw:
            browser.close()
            return False, "structured availability: OutOfStock", body_text

        # Visible negative status takes precedence over generic page wording.
        for phrase in negative_phrases:
            if phrase in body_text:
                browser.close()
                return False, f"visible status: {phrase}", body_text

        # Fallback positive wording only where the page visibly presents it.
        for phrase in positive_phrases:
            if phrase in body_text:
                browser.close()
                return True, f"visible status: {phrase}", body_text

        details = "; ".join(button_details[:10]) or "no matching purchase controls"
        browser.close()
        return None, f"no reliable stock signal; {details}", body_text


def main() -> int:
    print(VERSION)
    cfg, state = load_json(CONFIG_PATH), load_json(STATE_PATH)
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    test = os.environ.get("TEST_NOTIFICATION", "false").lower() == "true"
    if not webhook:
        raise RuntimeError("DISCORD_WEBHOOK_URL secret is missing")

    name, url = cfg["product_name"], cfg["product_url"]
    username = cfg.get("discord_username", "Dunelm Stock Monitor")
    if test:
        send_discord(webhook, f"✅ **Dunelm V2 monitor test successful**\n**{name}**\n<{url}>", username)
        print("Test notification sent.")
        return 0

    current, reason, _ = detect_with_browser(url)
    print(f"Detection result: {current}; {reason}")
    if current is None:
        raise RuntimeError(f"Could not identify Dunelm stock status: {reason}")

    previous = state.get("in_stock")
    initialised = bool(state.get("initialised"))
    should_alert = (initialised and previous is False and current is True) or (
        not initialised and current is True and cfg.get("notify_if_initially_in_stock", True)
    )

    if should_alert:
        label = "CURRENTLY IN STOCK" if not initialised else "RESTOCK DETECTED"
        send_discord(webhook,
            f"🚨 **DUNELM {label}**\n**{name}**\n🟢 **Available to purchase**\nDetected by: `{reason}`\n<{url}>",
            username)
        print("Stock alert sent.")
    else:
        print(f"No alert required. Previous={previous}, current={current}, initialised={initialised}")

    state.update({
        "initialised": True,
        "in_stock": current,
        "last_status": "in_stock" if current else "out_of_stock",
        "last_reason": reason,
        "last_checked_utc": datetime.now(timezone.utc).isoformat(),
    })
    save_json(STATE_PATH, state)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
