#!/usr/bin/env python3
"""NVIDIA Spain RTX 5090 Founders Edition stock watcher.

Runs in GitHub Actions. A scheduled run starts every 5 minutes and performs
five checks 60 seconds apart, giving approximately one stock check per minute.

Alerts are created as GitHub issues assigned to the repository owner. The issue
body mentions the owner as well, so normal GitHub notification settings can
surface the alert on mobile/email.
"""

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

LOCALE = "es-es"
SKU = "5090LDLCFE"
MARKETPLACE_URL = (
    "https://marketplace.nvidia.com/es-es/consumer/graphics-cards/"
    "?locale=es-es&page=1&limit=12&gpu=RTX+5090&manufacturer=NVIDIA"
)
STOCK_URL = (
    "https://api.store.nvidia.com/partner/v1/feinventory"
    f"?status=1&skus={SKU}&locale={LOCALE}"
)

STOCK_TITLE = "🚨 RTX 5090 FE IN STOCK — NVIDIA Spain"
PREALERT_TITLE = "⚠️ RTX 5090 FE SKU/API CHANGE — CHECK NVIDIA NOW"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def request_json(url, method="GET", payload=None, headers=None, timeout=20):
    merged = dict(HEADERS)
    if headers:
        merged.update(headers)

    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        merged["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=merged, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else None


def check_stock():
    try:
        data = request_json(STOCK_URL)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        return "error", {"error": repr(exc)}

    if not isinstance(data, dict):
        return "error", {"error": "Unexpected NVIDIA JSON root", "response": data}

    if data.get("success") is False:
        return "error", {"error": "NVIDIA API returned success=false", "response": data}

    entries = data.get("listMap")
    if entries is None:
        return "error", {"error": "listMap missing", "response": data}

    if isinstance(entries, dict):
        entries = [entries]

    if not isinstance(entries, list):
        return "error", {"error": "Unexpected listMap type", "response": data}

    # NVIDIA has historically returned an empty listMap when the current FE SKU
    # is disabled/changed. Stock trackers have observed this around some drops,
    # so it is useful as an early-warning signal, but it is NOT treated as stock.
    if not entries:
        return "prealert", {
            "reason": "Current Spain FE SKU returned an empty listMap; SKU may have changed",
            "response": data,
        }

    for item in entries:
        if not isinstance(item, dict):
            continue

        active = bool(item.get("is_active"))
        available = bool(item.get("isAvailable"))

        try:
            stock = int(item.get("stock") or 0)
        except (TypeError, ValueError):
            stock = 0

        buy_url = (
            item.get("product_url")
            or item.get("directPurchaseLink")
            or item.get("purchaseLink")
            or ""
        )

        # Different versions of NVIDIA's endpoint have exposed availability via
        # is_active, isAvailable, stock, or a populated purchase URL. Accept all.
        if active or available or stock > 0 or bool(buy_url):
            return "in_stock", {
                "item": item,
                "buy_url": buy_url or MARKETPLACE_URL,
            }

    return "out_of_stock", {"entries": entries}


def github_request(method, path, payload=None):
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        raise RuntimeError("GITHUB_TOKEN/GITHUB_REPOSITORY not available")

    url = f"https://api.github.com/repos/{repo}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "rtx-5090-fe-spain-watcher",
    }

    data = None if payload is None else json.dumps(payload).encode("utf-8")
    if data is not None:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as response:
        raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else None


def open_issues():
    try:
        result = github_request("GET", "/issues?state=open&per_page=100")
        if isinstance(result, list):
            return [x for x in result if "pull_request" not in x]
    except Exception as exc:
        print(f"[{now_iso()}] GitHub issue lookup failed: {exc!r}", flush=True)
    return []


def find_open_issue(title):
    for issue in open_issues():
        if issue.get("title") == title:
            return issue
    return None


def ensure_issue(title, body):
    existing = find_open_issue(title)
    if existing:
        return existing.get("number")

    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "cagdasdirek")
    payload = {
        "title": title,
        "body": body,
        "assignees": [owner],
    }
    try:
        created = github_request("POST", "/issues", payload)
        number = created.get("number") if isinstance(created, dict) else None
        print(f"[{now_iso()}] ALERT issue created #{number}: {title}", flush=True)
        return number
    except Exception as exc:
        print(f"[{now_iso()}] Could not create alert issue: {exc!r}", flush=True)
        return None


def close_issue(title):
    issue = find_open_issue(title)
    if not issue:
        return

    number = issue.get("number")
    try:
        github_request("PATCH", f"/issues/{number}", {"state": "closed"})
        print(f"[{now_iso()}] Closed reset issue #{number}: {title}", flush=True)
    except Exception as exc:
        print(f"[{now_iso()}] Could not close issue #{number}: {exc!r}", flush=True)


def handle_state(state, info):
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "cagdasdirek")

    if state == "in_stock":
        item = info.get("item") or {}
        buy_url = info.get("buy_url") or MARKETPLACE_URL
        body = (
            f"@{owner} **RTX 5090 Founders Edition appears to be IN STOCK in NVIDIA Spain.**\n\n"
            f"# 👉 BUY NOW: {buy_url}\n\n"
            f"NVIDIA Marketplace fallback: {MARKETPLACE_URL}\n\n"
            f"Detected: `{now_iso()}`\n\n"
            "NVIDIA API response item:\n"
            f"```json\n{json.dumps(item, ensure_ascii=False, indent=2)[:5000]}\n```"
        )
        ensure_issue(STOCK_TITLE, body)
        close_issue(PREALERT_TITLE)
        return

    if state == "prealert":
        body = (
            f"@{owner} NVIDIA Spain's known RTX 5090 FE SKU `{SKU}` returned an **empty listMap**.\n\n"
            "This is **not proof that stock is live**, but NVIDIA has changed/disabled FE SKUs around "
            "drops before, so open the store immediately.\n\n"
            f"# 👉 CHECK NVIDIA NOW: {MARKETPLACE_URL}\n\n"
            f"Detected: `{now_iso()}`"
        )
        ensure_issue(PREALERT_TITLE, body)
        return

    if state == "out_of_stock":
        # Reset old alerts after stock/API state normalizes. This allows a new
        # issue (and therefore a new notification) on the next real drop.
        close_issue(STOCK_TITLE)
        close_issue(PREALERT_TITLE)


def main():
    checks = max(1, int(os.environ.get("CHECKS_PER_RUN", "5")))
    interval = max(10, int(os.environ.get("CHECK_INTERVAL_SECONDS", "60")))

    print(f"NVIDIA endpoint: {STOCK_URL}", flush=True)
    print(f"Marketplace: {MARKETPLACE_URL}", flush=True)

    for index in range(checks):
        state, info = check_stock()
        short_info = json.dumps(info, ensure_ascii=False)[:1500]
        print(
            f"[{now_iso()}] check {index + 1}/{checks}: {state} | {short_info}",
            flush=True,
        )

        # Network/API errors are logged but never converted into false alerts.
        if state != "error":
            handle_state(state, info)

        if index < checks - 1:
            time.sleep(interval)


if __name__ == "__main__":
    main()
