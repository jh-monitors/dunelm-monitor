#!/usr/bin/env python3
"""Dunelm single-product stock monitor.

Conservative detection: only reports in stock when a recognised positive
purchase/availability signal is present. Unknown pages cause the workflow to
fail rather than generating a false restock alert.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from html import unescape
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

VERSION = "DUNELM-MONITOR-V1"
CONFIG_PATH = Path("config.json")
STATE_PATH = Path("state.json")


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def fetch(url: str, attempts: int = 3) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/138.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=30) as response:
                html = response.read().decode("utf-8", errors="replace")
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
                if len(html) < 20_000:
                    raise RuntimeError(f"Page unexpectedly short ({len(html)} bytes)")
                return html
        except (HTTPError, URLError, TimeoutError, RuntimeError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(attempt * 3)
    raise RuntimeError(f"Could not download {url}: {last_error}")


def visible_text(html: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip().lower()


def detect_stock(html: str) -> tuple[bool | None, str]:
    raw = html.lower()
    text = visible_text(html)

    # Strong structured-data signals, when supplied by the product page.
    positive_raw = [
        '"availability":"https://schema.org/instock"',
        '"availability": "https://schema.org/instock"',
        'schema.org/instock',
        '"stockstatus":"instock"',
        '"stockstatus": "instock"',
    ]
    negative_raw = [
        '"availability":"https://schema.org/outofstock"',
        '"availability": "https://schema.org/outofstock"',
        'schema.org/outofstock',
        '"stockstatus":"outofstock"',
        '"stockstatus": "outofstock"',
    ]

    # Strong visible purchase signals. Keep these specific to avoid matching
    # generic help/footer wording.
    positive_text = [
        "add to basket",
        "add to bag",
        "available for home delivery",
        "choose a delivery date",
    ]
    negative_text = [
        "out of stock",
        "currently unavailable",
        "this product is unavailable",
        "email me when back in stock",
        "notify me when back in stock",
    ]

    for signal in positive_raw:
        if signal in raw:
            return True, f"structured signal: {signal}"
    for signal in negative_raw:
        if signal in raw:
            return False, f"structured signal: {signal}"

    for signal in positive_text:
        if signal in text:
            return True, f"visible signal: {signal}"
    for signal in negative_text:
        if signal in text:
            return False, f"visible signal: {signal}"

    # Dunelm may render availability with JavaScript. Do not guess from the
    # absence of a button: unknown is safer than a false alert.
    return None, "no recognised stock signal found"


def send_discord(webhook_url: str, content: str, username: str) -> None:
    payload = json.dumps({"username": username, "content": content}).encode("utf-8")
    req = Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "DunelmStockMonitor/1.0"},
        method="POST",
    )
    with urlopen(req, timeout=20) as response:
        if response.status not in (200, 204):
            raise RuntimeError(f"Discord returned HTTP {response.status}")


def main() -> int:
    print(VERSION)
    config = load_json(CONFIG_PATH)
    state = load_json(STATE_PATH)
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    test_mode = os.environ.get("TEST_NOTIFICATION", "false").lower() == "true"

    if not webhook:
        raise RuntimeError("DISCORD_WEBHOOK_URL secret is missing")

    name = config["product_name"]
    url = config["product_url"]
    username = config.get("discord_username", "Dunelm Stock Monitor")

    if test_mode:
        send_discord(webhook, f"✅ **Dunelm monitor test successful**\n{name}\n<{url}>", username)
        print("Test notification sent; website was not checked.")
        return 0

    html = fetch(url)
    in_stock, reason = detect_stock(html)
    print(f"Detection result: {in_stock}; {reason}")

    if in_stock is None:
        raise RuntimeError(
            "Dunelm page loaded, but its stock status could not be identified safely. "
            "The website may have changed or may require browser rendering."
        )

    previous = state.get("in_stock")
    initialised = bool(state.get("initialised"))

    if not initialised:
        state.update({"initialised": True, "in_stock": in_stock, "last_status": "in_stock" if in_stock else "out_of_stock"})
        save_json(STATE_PATH, state)
        print(f"Initial baseline saved: {'IN STOCK' if in_stock else 'OUT OF STOCK'}. No alert sent.")
        return 0

    if previous is False and in_stock is True:
        send_discord(
            webhook,
            f"🚨 **DUNELM RESTOCK DETECTED**\n**{name}**\nPrice shown: **£400**\n🟢 **In stock / purchasable**\n<{url}>",
            username,
        )
        print("Restock alert sent.")
    else:
        print(f"No restock transition. Previous={previous}, current={in_stock}")

    state.update({"initialised": True, "in_stock": in_stock, "last_status": "in_stock" if in_stock else "out_of_stock"})
    save_json(STATE_PATH, state)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
