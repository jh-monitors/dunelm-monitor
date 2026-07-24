#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

VERSION = "DUNELM-MONITOR-STRICT-V3"
CONFIG_PATH = Path("config.json")
STATE_PATH = Path("state.json")
DIAG_DIR = Path("diagnostics")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def send_discord(webhook: str, content: str, username: str) -> None:
    payload = json.dumps({"username": username, "content": content}).encode("utf-8")
    req = Request(
        webhook,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "DunelmStockMonitor/3.0"},
    )
    with urlopen(req, timeout=20) as response:
        if response.status not in (200, 204):
            raise RuntimeError(f"Discord returned HTTP {response.status}")


def normalise(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def walk_json(value: Any, path: str = "root"):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            yield child_path, key.lower(), child
            yield from walk_json(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk_json(child, f"{path}[{index}]")


def inspect_inventory_json(data: Any) -> list[tuple[bool, str]]:
    """Return only explicit stock evidence. Generic button/config flags are ignored."""
    evidence: list[tuple[bool, str]] = []
    stock_keys = {
        "availability", "stockstatus", "stock_status", "inventorystatus",
        "inventory_status", "fulfilmentstatus", "fulfillmentstatus",
        "deliveryavailability", "delivery_availability", "availablequantity",
        "available_quantity", "stockquantity", "stock_quantity",
        "inventoryquantity", "inventory_quantity", "quantityavailable",
        "quantity_available", "ats", "availabletosell",
    }
    quantity_keys = {
        "availablequantity", "available_quantity", "stockquantity",
        "stock_quantity", "inventoryquantity", "inventory_quantity",
        "quantityavailable", "quantity_available", "ats", "availabletosell",
    }
    positive_values = {
        "instock", "in_stock", "in stock", "available", "availableonline",
        "available_online", "available for delivery", "home delivery available",
    }
    negative_values = {
        "outofstock", "out_of_stock", "out of stock", "unavailable",
        "notavailable", "not_available", "soldout", "sold_out",
        "unavailableonline", "unavailable_online",
    }

    for path, key, raw in walk_json(data):
        compact_key = re.sub(r"[^a-z]", "", key)
        matched_key = key in stock_keys or compact_key in {re.sub(r"[^a-z]", "", k) for k in stock_keys}
        if not matched_key:
            continue

        if key in quantity_keys or "quantity" in key or compact_key in {"ats", "availabletosell"}:
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                evidence.append((raw > 0, f"inventory JSON {path}={raw}"))
            elif isinstance(raw, str) and re.fullmatch(r"\d+(?:\.\d+)?", raw.strip()):
                qty = float(raw)
                evidence.append((qty > 0, f"inventory JSON {path}={raw}"))
            continue

        if isinstance(raw, bool):
            evidence.append((raw, f"inventory JSON {path}={raw}"))
            continue

        if isinstance(raw, str):
            value = normalise(raw)
            compact = re.sub(r"[^a-z]", "", value)
            if value in positive_values or compact in {re.sub(r"[^a-z]", "", x) for x in positive_values}:
                evidence.append((True, f"inventory JSON {path}={raw}"))
            elif value in negative_values or compact in {re.sub(r"[^a-z]", "", x) for x in negative_values}:
                evidence.append((False, f"inventory JSON {path}={raw}"))

    return evidence


def detect_with_browser(url: str) -> tuple[bool | None, str]:
    DIAG_DIR.mkdir(exist_ok=True)
    captured: list[dict[str, Any]] = []
    inventory_evidence: list[tuple[bool, str]] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            locale="en-GB",
            timezone_id="Europe/London",
            viewport={"width": 1440, "height": 1200},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        def capture_response(response) -> None:
            try:
                content_type = (response.headers.get("content-type") or "").lower()
                resource_type = response.request.resource_type
                if resource_type not in {"xhr", "fetch"} and "json" not in content_type:
                    return
                text = response.text()
                record: dict[str, Any] = {
                    "url": response.url,
                    "status": response.status,
                    "resource_type": resource_type,
                    "content_type": content_type,
                }
                try:
                    data = json.loads(text)
                    evidence = inspect_inventory_json(data)
                    if evidence:
                        inventory_evidence.extend(
                            (value, f"{reason} from {response.url}") for value, reason in evidence
                        )
                        record["inventory_evidence"] = [reason for _, reason in evidence]
                    record["json"] = data
                except Exception:
                    record["body_preview"] = text[:3000]
                captured.append(record)
            except Exception:
                return

        page.on("response", capture_response)
        response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
        if response and response.status >= 400:
            raise RuntimeError(f"Dunelm returned HTTP {response.status}")
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(8000)

        title = page.title()
        body_text = normalise(page.locator("body").inner_text(timeout=15000))
        html = page.content()
        page.screenshot(path=str(DIAG_DIR / "dunelm-page.png"), full_page=True)
        (DIAG_DIR / "dunelm-page.html").write_text(html, encoding="utf-8")
        (DIAG_DIR / "dunelm-body.txt").write_text(body_text, encoding="utf-8")
        (DIAG_DIR / "network-responses.json").write_text(
            json.dumps(captured, indent=2, default=str)[:5_000_000], encoding="utf-8"
        )

        if "access denied" in normalise(title) or "access denied" in body_text[:1000]:
            browser.close()
            raise RuntimeError("Dunelm returned an Access Denied page")

        # Explicit negative page wording always wins over controls/buttons.
        negative_phrases = [
            "out of stock", "currently unavailable", "unavailable online",
            "not available for delivery", "home delivery unavailable",
            "notify me when back in stock", "email me when back in stock",
        ]
        for phrase in negative_phrases:
            if phrase in body_text:
                browser.close()
                return False, f"visible status: {phrase}"

        # Prefer explicit network inventory. Any explicit zero/out-of-stock wins.
        negatives = [reason for value, reason in inventory_evidence if value is False]
        positives = [reason for value, reason in inventory_evidence if value is True]
        if negatives:
            browser.close()
            return False, negatives[0]
        if positives:
            browser.close()
            return True, positives[0]

        # Strict visible signals. Add-to-basket alone is intentionally excluded.
        positive_phrases = [
            "available for home delivery", "home delivery available",
            "available for delivery", "delivery available",
            "in stock for delivery",
        ]
        for phrase in positive_phrases:
            if phrase in body_text:
                browser.close()
                return True, f"explicit visible status: {phrase}"

        # Structured product availability may be stale, so use it only for OOS.
        raw = html.lower().replace(" ", "")
        if "schema.org/outofstock" in raw or '"availability":"outofstock"' in raw:
            browser.close()
            return False, "structured availability: OutOfStock"

        browser.close()
        return None, "no explicit inventory or delivery-availability signal"


def main() -> int:
    print(VERSION)
    cfg = load_json(CONFIG_PATH)
    state = load_json(STATE_PATH)
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    test = os.environ.get("TEST_NOTIFICATION", "false").lower() == "true"
    if not webhook:
        raise RuntimeError("DISCORD_WEBHOOK_URL secret is missing")

    name = cfg["product_name"]
    url = cfg["product_url"]
    username = cfg.get("discord_username", "Dunelm Stock Monitor")

    if test:
        send_discord(webhook, f"✅ **Dunelm V3 monitor test successful**\n**{name}**\n<{url}>", username)
        print("Test notification sent.")
        return 0

    current, reason = detect_with_browser(url)
    print(f"Detection result: {current}; {reason}")

    now = datetime.now(timezone.utc).isoformat()
    if current is None:
        # Unknown is deliberately non-alerting and does not overwrite the last
        # confirmed stock state. Diagnostics are still uploaded by the workflow.
        state.update({"last_checked_utc": now, "last_attempt_status": "unknown", "last_reason": reason})
        save_json(STATE_PATH, state)
        print("No Discord alert: Dunelm did not expose a sufficiently reliable stock signal.")
        return 2

    previous = state.get("in_stock")
    initialised = bool(state.get("initialised"))
    should_alert = initialised and previous is False and current is True

    # No initial in-stock alert: this prevents a deployment/reset from creating
    # another false 'currently in stock' notification.
    if should_alert:
        send_discord(
            webhook,
            f"🚨 **DUNELM RESTOCK DETECTED**\n**{name}**\n"
            f"🟢 **Explicit stock signal confirmed**\nDetected by: `{reason}`\n<{url}>",
            username,
        )
        print("Restock alert sent.")
    else:
        print(f"No alert required. Previous={previous}, current={current}, initialised={initialised}")

    state.update({
        "initialised": True,
        "in_stock": current,
        "last_status": "in_stock" if current else "out_of_stock",
        "last_attempt_status": "confirmed",
        "last_reason": reason,
        "last_checked_utc": now,
    })
    save_json(STATE_PATH, state)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
