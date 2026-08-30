#!/usr/bin/env python3
"""Advanced NVIDIA Spain RTX 5090 Founders Edition stock watcher.

Strategy:
- Discover the current NVIDIA product SKU dynamically from the Marketplace product-search API.
- Poll the FE inventory API using that live SKU.
- Persist the last discovered SKU in a closed GitHub issue so changes survive Actions runs.
- Treat SKU changes / empty inventory maps as early-warning signals, not as stock.
- Use strict boolean parsing: NVIDIA commonly returns strings such as "true" / "false".
- Never treat a mere product URL or an Add-to-Cart label as proof of stock.
- Monitor source health because NVIDIA/Akamai can block datacenter monitoring with 403s.

The GitHub workflow controls frequency. No purchase automation is performed.
"""

from __future__ import annotations

import html as html_lib
import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

LOCALE = "es-es"
GPU_NAME = "RTX 5090"

MARKETPLACE_URL = (
    "https://marketplace.nvidia.com/es-es/consumer/graphics-cards/"
    "?locale=es-es&page=1&limit=12&gpu=RTX+5090&manufacturer=NVIDIA"
)
PRODUCT_PAGE_URL = (
    "https://marketplace.nvidia.com/es-es/consumer/graphics-cards/"
    "nvidia-geforce-rtx-5090/"
)
PRODUCT_SEARCH_URL = (
    "https://api.nvidia.partners/edge/product/search"
    "?page=1&limit=100&locale=es-es&Manufacturer=Nvidia"
)
INVENTORY_BASE_URL = (
    "https://api.store.nvidia.com/partner/v1/feinventory"
    "?status=1&locale=es-es&skus="
)

# Current product-page MPN observed in Spain plus historical FE identifiers.
# These are only fallbacks if dynamic discovery is temporarily unavailable.
FALLBACK_SKUS = [
    "LCFEGF50LD90",
    "5090LDLCFE",
    "LDLC5090FE",
    "NVGFT590",
]

STOCK_TITLE = "🚨 RTX 5090 FE IN STOCK — NVIDIA Spain"
PREALERT_TITLE = "⚠️ RTX 5090 FE BACKEND CHANGE — CHECK NVIDIA NOW"
HEALTH_TITLE = "🛠️ RTX 5090 tracker degraded — NVIDIA API blocked/failing"
STATE_TITLE = "🛰️ RTX 5090 tracker state (do not delete)"

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)

NVIDIA_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Origin": "https://marketplace.nvidia.com",
    "Referer": "https://marketplace.nvidia.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}

PAGE_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def cache_bust(url: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}_t={int(time.time() * 1000)}{random.randint(100, 999)}"


def strict_true(value: Any) -> bool:
    """Parse NVIDIA booleans safely; string 'false' must never become True."""
    if value is True:
        return True
    if value is False or value is None:
        return False
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return False


def request_raw(
    url: str,
    *,
    headers: dict[str, str],
    timeout: int = 12,
    attempts: int = 2,
) -> tuple[int, bytes, dict[str, str]]:
    """GET with conservative retries for transient errors, not anti-bot 403s."""
    last_error: Exception | None = None
    for attempt in range(attempts):
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return (
                    int(response.status),
                    response.read(),
                    {k.lower(): v for k, v in response.headers.items()},
                )
        except urllib.error.HTTPError as exc:
            # Retrying 403s usually just increases blocking. Retry only transient statuses.
            if exc.code not in {429, 500, 502, 503, 504} or attempt == attempts - 1:
                raise
            last_error = exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == attempts - 1:
                raise
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"request failed: {last_error!r}")


def request_json(url: str, *, cachebuster: bool = True) -> Any:
    target = cache_bust(url) if cachebuster else url
    _, raw, _ = request_raw(target, headers=NVIDIA_HEADERS)
    return json.loads(raw.decode("utf-8"))


def request_text(url: str) -> str:
    _, raw, _ = request_raw(cache_bust(url), headers=PAGE_HEADERS)
    return raw.decode("utf-8", errors="replace")


def normalize_upcs(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    return [str(x).strip() for x in values if str(x).strip()]


def discover_product() -> tuple[dict[str, Any] | None, str | None]:
    """Find the live RTX 5090 NVIDIA product and SKU from NVIDIA's search API."""
    try:
        data = request_json(PRODUCT_SEARCH_URL)
    except Exception as exc:
        return None, f"product-search request failed: {exc!r}"

    try:
        products = data.get("searchedProducts", {}).get("productDetails", [])
    except AttributeError:
        return None, "product-search returned unexpected JSON structure"

    if not isinstance(products, list):
        return None, "productDetails is not a list"

    candidates: list[dict[str, Any]] = []
    for product in products:
        if not isinstance(product, dict):
            continue
        gpu = str(product.get("gpu", "")).strip().lower()
        manufacturer = str(product.get("manufacturer", "")).strip().lower()
        title = str(product.get("productTitle", "")).lower()

        gpu_match = gpu in {"rtx 5090", "geforce rtx 5090"} or "rtx 5090" in title
        nvidia_match = not manufacturer or manufacturer == "nvidia"
        if gpu_match and nvidia_match:
            candidates.append(product)

    if not candidates:
        return None, "RTX 5090 NVIDIA product missing from product-search API"

    # Prefer an explicit NVIDIA/Founders Edition result when more than one appears.
    def score(p: dict[str, Any]) -> int:
        blob = " ".join(
            str(p.get(k, "")).lower()
            for k in ("manufacturer", "productTitle", "productName", "productSKU")
        )
        return (
            (5 if str(p.get("manufacturer", "")).lower() == "nvidia" else 0)
            + (3 if "founders" in blob else 0)
            + (2 if "5090" in str(p.get("productSKU", "")) else 0)
        )

    product = max(candidates, key=score)
    sku = str(product.get("productSKU", "")).strip()
    if not sku:
        return None, "RTX 5090 product found but productSKU is empty"

    retailers = product.get("retailers") if isinstance(product.get("retailers"), list) else []
    return {
        "sku": sku,
        "upcs": normalize_upcs(product.get("productUPC")),
        "title": product.get("productTitle") or product.get("productName") or GPU_NAME,
        "retailers": retailers,
        "raw": product,
    }, None


def extract_page_mpn(page_html: str) -> str | None:
    """Best-effort independent MPN discovery from NVIDIA's product detail page."""
    plain = html_lib.unescape(re.sub(r"<[^>]+>", " ", page_html))
    plain = re.sub(r"\s+", " ", plain)
    patterns = [
        r"\bMPN\s*:?\s*([A-Z0-9_-]{6,32})\b",
        r'"(?:mpn|productSKU)"\s*:\s*"([A-Za-z0-9_-]{6,32})"',
    ]
    for pattern in patterns:
        match = re.search(pattern, plain, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def check_product_page() -> tuple[dict[str, Any] | None, str | None]:
    try:
        page = request_text(PRODUCT_PAGE_URL)
    except Exception as exc:
        return None, f"product page request failed: {exc!r}"

    plain_lower = html_lib.unescape(re.sub(r"<[^>]+>", " ", page)).lower()
    return {
        "mpn": extract_page_mpn(page),
        "mentions_5090": "rtx 5090" in plain_lower,
        # These are only diagnostics. The button may exist while disabled/out of stock.
        "has_cart_text": any(x in plain_lower for x in ("añadir al carrito", "comprar ahora")),
        "has_oos_text": any(x in plain_lower for x in ("agotado", "producto agotado", "out of stock")),
    }, None


def inventory_url(sku: str) -> str:
    return INVENTORY_BASE_URL + urllib.parse.quote(sku, safe="")


def check_inventory(sku: str) -> tuple[str, dict[str, Any]]:
    try:
        data = request_json(inventory_url(sku))
    except urllib.error.HTTPError as exc:
        return "error", {"sku": sku, "http_status": exc.code, "error": repr(exc)}
    except Exception as exc:
        return "error", {"sku": sku, "error": repr(exc)}

    if not isinstance(data, dict):
        return "error", {"sku": sku, "error": "unexpected inventory JSON root"}
    if data.get("success") is False:
        return "error", {"sku": sku, "error": "inventory success=false", "response": data}

    entries = data.get("listMap")
    if isinstance(entries, dict):
        entries = [entries]
    if entries is None:
        return "error", {"sku": sku, "error": "listMap missing", "response": data}
    if not isinstance(entries, list):
        return "error", {"sku": sku, "error": "listMap malformed", "response": data}
    if not entries:
        return "empty", {"sku": sku, "entries": []}

    for item in entries:
        if not isinstance(item, dict):
            continue
        active = strict_true(item.get("is_active"))
        available = strict_true(item.get("isAvailable"))
        try:
            stock = int(item.get("stock") or 0)
        except (TypeError, ValueError):
            stock = 0

        # Strong stock evidence only. A product_url alone is NOT stock evidence.
        if active or available or stock > 0:
            buy_url = (
                item.get("product_url")
                or item.get("directPurchaseLink")
                or item.get("purchaseLink")
                or MARKETPLACE_URL
            )
            return "in_stock", {
                "sku": sku,
                "item": item,
                "buy_url": buy_url,
                "active": active,
                "available": available,
                "stock": stock,
            }

    return "out_of_stock", {"sku": sku, "entries": entries}


# ------------------------- GitHub state / alerts -------------------------


def github_request(method: str, path: str, payload: Any = None) -> Any:
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        raise RuntimeError("GITHUB_TOKEN/GITHUB_REPOSITORY unavailable")

    url = f"https://api.github.com/repos/{repo}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "rtx-5090-fe-spain-watcher",
    }
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=15) as response:
        raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else None


def all_issues() -> list[dict[str, Any]]:
    try:
        result = github_request("GET", "/issues?state=all&per_page=100")
        if isinstance(result, list):
            return [x for x in result if isinstance(x, dict) and "pull_request" not in x]
    except Exception as exc:
        print(f"[{now_iso()}] GitHub issue lookup failed: {exc!r}", flush=True)
    return []


def find_issue(title: str, *, open_only: bool = False) -> dict[str, Any] | None:
    for issue in all_issues():
        if issue.get("title") == title and (not open_only or issue.get("state") == "open"):
            return issue
    return None


def create_issue(title: str, body: str, *, notify_owner: bool = True) -> int | None:
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "cagdasdirek")
    payload: dict[str, Any] = {"title": title, "body": body}
    if notify_owner:
        payload["assignees"] = [owner]
    try:
        created = github_request("POST", "/issues", payload)
        number = created.get("number") if isinstance(created, dict) else None
        print(f"[{now_iso()}] Created issue #{number}: {title}", flush=True)
        return number
    except Exception as exc:
        print(f"[{now_iso()}] Could not create issue '{title}': {exc!r}", flush=True)
        return None


def ensure_alert_issue(title: str, body: str) -> int | None:
    existing = find_issue(title, open_only=True)
    if existing:
        return existing.get("number")
    return create_issue(title, body, notify_owner=True)


def close_alert_issue(title: str) -> None:
    issue = find_issue(title, open_only=True)
    if not issue:
        return
    number = issue.get("number")
    try:
        github_request("PATCH", f"/issues/{number}", {"state": "closed"})
        print(f"[{now_iso()}] Closed issue #{number}: {title}", flush=True)
    except Exception as exc:
        print(f"[{now_iso()}] Could not close issue #{number}: {exc!r}", flush=True)


def load_state() -> dict[str, Any]:
    issue = find_issue(STATE_TITLE)
    if not issue:
        return {}
    body = issue.get("body") or ""
    match = re.search(r"```json\s*(\{.*?\})\s*```", body, flags=re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def save_state(state: dict[str, Any]) -> None:
    body = (
        "Internal state for the RTX 5090 watcher. This issue is intentionally closed.\n\n"
        f"```json\n{json.dumps(state, ensure_ascii=False, indent=2)}\n```"
    )
    issue = find_issue(STATE_TITLE)
    try:
        if not issue:
            number = create_issue(STATE_TITLE, body, notify_owner=False)
            if number:
                github_request("PATCH", f"/issues/{number}", {"state": "closed"})
        else:
            number = issue.get("number")
            github_request("PATCH", f"/issues/{number}", {"body": body, "state": "closed"})
    except Exception as exc:
        print(f"[{now_iso()}] Could not persist tracker state: {exc!r}", flush=True)


def backend_change_alert(reason: str, details: dict[str, Any]) -> None:
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "cagdasdirek")
    body = (
        f"@{owner} **NVIDIA Spain changed something in the RTX 5090 FE backend.**\n\n"
        "This is **not proof that stock is live**, but SKU/backend changes have preceded FE drops, "
        "so open NVIDIA immediately.\n\n"
        f"# 👉 CHECK NVIDIA NOW: {MARKETPLACE_URL}\n\n"
        f"Reason: **{reason}**\n\n"
        f"Detected: `{now_iso()}`\n\n"
        f"```json\n{json.dumps(details, ensure_ascii=False, indent=2)[:5000]}\n```"
    )
    ensure_alert_issue(PREALERT_TITLE, body)


def stock_alert(info: dict[str, Any]) -> None:
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "cagdasdirek")
    buy_url = info.get("buy_url") or MARKETPLACE_URL
    item = info.get("item") or {}
    body = (
        f"@{owner} **RTX 5090 Founders Edition is reported IN STOCK by NVIDIA's inventory API.**\n\n"
        f"# 👉 BUY NOW: {buy_url}\n\n"
        f"NVIDIA Spain fallback: {MARKETPLACE_URL}\n\n"
        f"Detected: `{now_iso()}`\n"
        f"SKU: `{info.get('sku', '')}`\n\n"
        f"```json\n{json.dumps(item, ensure_ascii=False, indent=2)[:5000]}\n```"
    )
    ensure_alert_issue(STOCK_TITLE, body)
    close_alert_issue(PREALERT_TITLE)
    close_alert_issue(HEALTH_TITLE)


def health_alert(details: dict[str, Any]) -> None:
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "cagdasdirek")
    body = (
        f"@{owner} The watcher has repeatedly failed to reach NVIDIA's inventory source. "
        "Akamai/NVIDIA may be blocking the GitHub runner, so stock detection is temporarily degraded.\n\n"
        f"Manual check: {MARKETPLACE_URL}\n\n"
        f"Detected: `{now_iso()}`\n\n"
        f"```json\n{json.dumps(details, ensure_ascii=False, indent=2)[:5000]}\n```"
    )
    ensure_alert_issue(HEALTH_TITLE, body)


# ------------------------------ Main loop ------------------------------


def main() -> None:
    checks = max(1, int(os.environ.get("CHECKS_PER_RUN", "5")))
    interval = max(20, int(os.environ.get("CHECK_INTERVAL_SECONDS", "60")))
    discovery_every = max(1, int(os.environ.get("DISCOVERY_EVERY_CHECKS", "1")))
    page_every = max(1, int(os.environ.get("PAGE_EVERY_CHECKS", "2")))

    state = load_state()
    current_sku = str(state.get("sku") or "").strip() or FALLBACK_SKUS[0]
    current_upcs = normalize_upcs(state.get("upcs"))

    inventory_errors = 0
    empty_inventory_streak = 0
    discovery_errors = 0

    print(f"Marketplace: {MARKETPLACE_URL}", flush=True)
    print(f"Starting SKU: {current_sku}", flush=True)

    for index in range(checks):
        cycle = index + 1
        print(f"\n[{now_iso()}] ===== cycle {cycle}/{checks} =====", flush=True)

        # 1) Dynamic product/SKU discovery. This is the key defense against stale hardcoded SKUs.
        if index % discovery_every == 0:
            product, discovery_error = discover_product()
            if product:
                discovery_errors = 0
                discovered_sku = product["sku"]
                discovered_upcs = product.get("upcs", [])
                print(
                    f"[{now_iso()}] discovery OK: sku={discovered_sku} upcs={discovered_upcs}",
                    flush=True,
                )

                previous_sku = str(state.get("sku") or "").strip()
                if previous_sku and discovered_sku != previous_sku:
                    backend_change_alert(
                        "Product-search SKU changed",
                        {"old_sku": previous_sku, "new_sku": discovered_sku, "upcs": discovered_upcs},
                    )

                current_sku = discovered_sku
                current_upcs = discovered_upcs
                if previous_sku != discovered_sku or state.get("upcs") != discovered_upcs:
                    state = {
                        "sku": discovered_sku,
                        "upcs": discovered_upcs,
                        "last_discovered": now_iso(),
                    }
                    save_state(state)
            else:
                discovery_errors += 1
                print(f"[{now_iso()}] discovery ERROR: {discovery_error}", flush=True)

        # 2) Inventory is the authoritative stock signal.
        inv_state, inv_info = check_inventory(current_sku)
        print(
            f"[{now_iso()}] inventory {current_sku}: {inv_state} | "
            f"{json.dumps(inv_info, ensure_ascii=False)[:1800]}",
            flush=True,
        )

        if inv_state == "in_stock":
            inventory_errors = 0
            empty_inventory_streak = 0
            stock_alert(inv_info)
        elif inv_state == "out_of_stock":
            inventory_errors = 0
            empty_inventory_streak = 0
            close_alert_issue(STOCK_TITLE)
            close_alert_issue(HEALTH_TITLE)
        elif inv_state == "empty":
            inventory_errors = 0
            empty_inventory_streak += 1
            # Empty listMap has historically been a useful SKU-change/drop precursor.
            # Require two observations to avoid alerting on a one-off CDN hiccup.
            if empty_inventory_streak >= 2:
                backend_change_alert(
                    "Inventory listMap stayed empty for the current SKU",
                    {"sku": current_sku, "streak": empty_inventory_streak},
                )
        else:
            inventory_errors += 1
            empty_inventory_streak = 0
            if inventory_errors >= 4:
                health_alert(
                    {
                        "inventory_error_streak": inventory_errors,
                        "discovery_error_streak": discovery_errors,
                        "current_sku": current_sku,
                        "last_inventory": inv_info,
                    }
                )

        # 3) Independent product-page observation. Never use the cart label alone as stock proof.
        if index % page_every == 0:
            page_info, page_error = check_product_page()
            if page_info:
                print(f"[{now_iso()}] product page: {page_info}", flush=True)
                page_mpn = str(page_info.get("mpn") or "").strip()
                previous_sku = str(state.get("sku") or "").strip()
                # Only use page MPN as a backend-change alert when dynamic discovery is failing;
                # otherwise product-search API remains the source of truth for SKU.
                if discovery_errors >= 2 and page_mpn and previous_sku and page_mpn != previous_sku:
                    backend_change_alert(
                        "Product-page MPN changed while search API is unavailable",
                        {"old_sku": previous_sku, "page_mpn": page_mpn},
                    )
            else:
                print(f"[{now_iso()}] product page ERROR: {page_error}", flush=True)

        # When everything is healthy and clearly OOS, reset the pre-alert only after several
        # normal cycles so a short backend-change signal remains visible long enough to notice.
        if (
            inv_state == "out_of_stock"
            and discovery_errors == 0
            and cycle >= 4
            and empty_inventory_streak == 0
        ):
            close_alert_issue(PREALERT_TITLE)

        if index < checks - 1:
            time.sleep(interval)


if __name__ == "__main__":
    main()
