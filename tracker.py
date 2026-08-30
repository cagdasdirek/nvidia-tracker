#!/usr/bin/env python3
"""Smart NVIDIA Spain RTX 5090 Founders Edition watcher.

Goals:
- Discover NVIDIA's current RTX 5090 SKU dynamically.
- Treat FE inventory as the authoritative stock signal.
- Poll adaptively instead of hammering a fixed interval.
- Respect 429 Retry-After and back off hard on 403/5xx/network failures.
- Automatically probe/recover after a cooldown (circuit breaker / auto-heal).
- Use NVIDIA product-search + product page as independent health/SKU fallbacks.
- Persist the last known SKU and recovery state in a closed GitHub issue.
- Never rotate proxies, bypass CAPTCHAs, or try to defeat access controls.
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
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
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

FALLBACK_SKUS = ["LCFEGF50LD90", "5090LDLCFE", "LDLC5090FE", "NVGFT590"]

STOCK_TITLE = "🚨 RTX 5090 FE IN STOCK — NVIDIA Spain"
PREALERT_TITLE = "⚠️ RTX 5090 FE BACKEND CHANGE — CHECK NVIDIA NOW"
HEALTH_TITLE = "🛠️ RTX 5090 tracker degraded — NVIDIA API cooling down"
STATE_TITLE = "🛰️ RTX 5090 tracker state (do not delete)"

# Smart-poll defaults. Workflow env can override these without editing code.
BASE_INTERVAL = int(os.environ.get("SMART_BASE_SECONDS", "45"))
BASE_JITTER = int(os.environ.get("SMART_JITTER_SECONDS", "12"))
STABLE_INTERVAL = int(os.environ.get("SMART_STABLE_SECONDS", "65"))
STABLE_AFTER = int(os.environ.get("SMART_STABLE_AFTER", "8"))
BURST_INTERVAL = int(os.environ.get("SMART_BURST_SECONDS", "24"))
BURST_JITTER = int(os.environ.get("SMART_BURST_JITTER", "6"))
BURST_CYCLES = int(os.environ.get("SMART_BURST_CYCLES", "8"))
MAX_COOLDOWN = int(os.environ.get("SMART_MAX_COOLDOWN", "900"))

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


def now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def cache_bust(url: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}_t={int(time.time() * 1000)}{random.randint(100, 999)}"


def strict_true(value: Any) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return False


def retry_after_seconds(headers: Any, default: int) -> int:
    raw = None
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except Exception:
        pass
    if not raw:
        return default
    raw = str(raw).strip()
    if raw.isdigit():
        return max(default, int(raw))
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(default, int((dt - now()).total_seconds()))
    except Exception:
        return default


def request_raw(
    url: str,
    *,
    headers: dict[str, str],
    timeout: int = 12,
    transient_retries: int = 1,
) -> tuple[int, bytes, dict[str, str]]:
    """Conservative GET. Never immediately retry 403/429."""
    for attempt in range(transient_retries + 1):
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return (
                    int(response.status),
                    response.read(),
                    {k.lower(): v for k, v in response.headers.items()},
                )
        except urllib.error.HTTPError as exc:
            # 403/429 are explicit stop/backoff signals; retrying them is counterproductive.
            if exc.code in {403, 429}:
                raise
            if exc.code not in {500, 502, 503, 504} or attempt >= transient_retries:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt >= transient_retries:
                raise
        time.sleep(1.0 + random.random())
    raise RuntimeError("request retry loop exhausted")


def request_json(url: str, *, cachebuster: bool = True) -> Any:
    target = cache_bust(url) if cachebuster else url
    _, raw, _ = request_raw(target, headers=NVIDIA_HEADERS)
    return json.loads(raw.decode("utf-8"))


def request_text(url: str) -> str:
    _, raw, _ = request_raw(cache_bust(url), headers=PAGE_HEADERS, transient_retries=0)
    return raw.decode("utf-8", errors="replace")


def normalize_upcs(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    return [str(x).strip() for x in values if str(x).strip()]


def discover_product() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        data = request_json(PRODUCT_SEARCH_URL)
    except urllib.error.HTTPError as exc:
        return None, {
            "source": "product-search",
            "http_status": exc.code,
            "retry_after": retry_after_seconds(exc.headers, 120 if exc.code == 429 else 300),
            "error": repr(exc),
        }
    except Exception as exc:
        return None, {"source": "product-search", "error": repr(exc)}

    try:
        products = data.get("searchedProducts", {}).get("productDetails", [])
    except AttributeError:
        return None, {"source": "product-search", "error": "unexpected JSON structure"}
    if not isinstance(products, list):
        return None, {"source": "product-search", "error": "productDetails is not a list"}

    candidates: list[dict[str, Any]] = []
    for product in products:
        if not isinstance(product, dict):
            continue
        gpu = str(product.get("gpu", "")).strip().lower()
        manufacturer = str(product.get("manufacturer", "")).strip().lower()
        title = str(product.get("productTitle", "")).lower()
        if (gpu in {"rtx 5090", "geforce rtx 5090"} or "rtx 5090" in title) and (
            not manufacturer or manufacturer == "nvidia"
        ):
            candidates.append(product)
    if not candidates:
        return None, {"source": "product-search", "error": "RTX 5090 result missing"}

    def score(p: dict[str, Any]) -> int:
        blob = " ".join(str(p.get(k, "")).lower() for k in ("manufacturer", "productTitle", "productName"))
        return (5 if str(p.get("manufacturer", "")).lower() == "nvidia" else 0) + (
            3 if "founders" in blob else 0
        )

    product = max(candidates, key=score)
    sku = str(product.get("productSKU", "")).strip()
    if not sku:
        return None, {"source": "product-search", "error": "productSKU empty"}
    retailers = product.get("retailers") if isinstance(product.get("retailers"), list) else []
    return {
        "sku": sku,
        "upcs": normalize_upcs(product.get("productUPC")),
        "retailers": retailers,
        "title": product.get("productTitle") or GPU_NAME,
    }, None


def extract_page_mpn(page_html: str) -> str | None:
    plain = html_lib.unescape(re.sub(r"<[^>]+>", " ", page_html))
    plain = re.sub(r"\s+", " ", plain)
    for pattern in (
        r"\bMPN\s*:?\s*([A-Z0-9_-]{6,32})\b",
        r'"(?:mpn|productSKU)"\s*:\s*"([A-Za-z0-9_-]{6,32})"',
    ):
        match = re.search(pattern, plain, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def check_product_page() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        page = request_text(PRODUCT_PAGE_URL)
    except urllib.error.HTTPError as exc:
        return None, {
            "source": "product-page",
            "http_status": exc.code,
            "retry_after": retry_after_seconds(exc.headers, 300),
            "error": repr(exc),
        }
    except Exception as exc:
        return None, {"source": "product-page", "error": repr(exc)}

    plain = html_lib.unescape(re.sub(r"<[^>]+>", " ", page)).lower()
    return {
        "mpn": extract_page_mpn(page),
        "mentions_5090": "rtx 5090" in plain,
        "has_cart_text": any(x in plain for x in ("añadir al carrito", "comprar ahora")),
        "has_oos_text": any(x in plain for x in ("agotado", "producto agotado", "out of stock")),
    }, None


def inventory_url(sku: str) -> str:
    return INVENTORY_BASE_URL + urllib.parse.quote(sku, safe="")


def check_inventory(sku: str) -> tuple[str, dict[str, Any]]:
    try:
        data = request_json(inventory_url(sku))
    except urllib.error.HTTPError as exc:
        default = 120 if exc.code == 429 else (300 if exc.code == 403 else 90)
        return "error", {
            "sku": sku,
            "http_status": exc.code,
            "retry_after": retry_after_seconds(exc.headers, default),
            "error": repr(exc),
        }
    except Exception as exc:
        return "error", {"sku": sku, "error": repr(exc)}

    if not isinstance(data, dict):
        return "error", {"sku": sku, "error": "unexpected inventory JSON root"}
    if data.get("success") is False:
        return "error", {"sku": sku, "error": "inventory success=false", "response": data}
    entries = data.get("listMap")
    if isinstance(entries, dict):
        entries = [entries]
    if entries is None or not isinstance(entries, list):
        return "error", {"sku": sku, "error": "listMap missing/malformed", "response": data}
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
        if active or available or stock > 0:
            return "in_stock", {
                "sku": sku,
                "item": item,
                "buy_url": item.get("product_url")
                or item.get("directPurchaseLink")
                or item.get("purchaseLink")
                or MARKETPLACE_URL,
                "active": active,
                "available": available,
                "stock": stock,
            }
    return "out_of_stock", {"sku": sku, "entries": entries}


# ---------------- GitHub state / alerts ----------------

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
        return [x for x in result if isinstance(x, dict) and "pull_request" not in x] if isinstance(result, list) else []
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
        return created.get("number") if isinstance(created, dict) else None
    except Exception as exc:
        print(f"[{now_iso()}] Could not create issue {title!r}: {exc!r}", flush=True)
        return None


def ensure_alert_issue(title: str, body: str) -> int | None:
    existing = find_issue(title, open_only=True)
    return existing.get("number") if existing else create_issue(title, body, notify_owner=True)


def close_alert_issue(title: str) -> None:
    issue = find_issue(title, open_only=True)
    if not issue:
        return
    try:
        github_request("PATCH", f"/issues/{issue.get('number')}", {"state": "closed"})
    except Exception as exc:
        print(f"[{now_iso()}] Could not close {title!r}: {exc!r}", flush=True)


def load_state() -> dict[str, Any]:
    issue = find_issue(STATE_TITLE)
    if not issue:
        return {}
    match = re.search(r"```json\s*(\{.*?\})\s*```", issue.get("body") or "", flags=re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def save_state(state: dict[str, Any]) -> None:
    body = "Internal state for the RTX 5090 watcher. This issue is intentionally closed.\n\n" + (
        f"```json\n{json.dumps(state, ensure_ascii=False, indent=2)}\n```"
    )
    issue = find_issue(STATE_TITLE)
    try:
        if not issue:
            number = create_issue(STATE_TITLE, body, notify_owner=False)
            if number:
                github_request("PATCH", f"/issues/{number}", {"state": "closed"})
        else:
            github_request("PATCH", f"/issues/{issue.get('number')}", {"body": body, "state": "closed"})
    except Exception as exc:
        print(f"[{now_iso()}] State persistence failed: {exc!r}", flush=True)


def backend_change_alert(reason: str, details: dict[str, Any]) -> None:
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "cagdasdirek")
    ensure_alert_issue(
        PREALERT_TITLE,
        f"@{owner} **NVIDIA Spain RTX 5090 FE backend changed.**\n\n"
        "This is not proof of stock, but it can be a drop precursor.\n\n"
        f"# 👉 CHECK NVIDIA NOW: {MARKETPLACE_URL}\n\nReason: **{reason}**\n\n"
        f"Detected: `{now_iso()}`\n\n```json\n{json.dumps(details, ensure_ascii=False, indent=2)[:5000]}\n```",
    )


def stock_alert(info: dict[str, Any]) -> None:
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "cagdasdirek")
    buy_url = info.get("buy_url") or MARKETPLACE_URL
    ensure_alert_issue(
        STOCK_TITLE,
        f"@{owner} **RTX 5090 Founders Edition is IN STOCK according to NVIDIA inventory.**\n\n"
        f"# 👉 BUY NOW: {buy_url}\n\nFallback: {MARKETPLACE_URL}\n\n"
        f"Detected: `{now_iso()}`\nSKU: `{info.get('sku', '')}`\n\n"
        f"```json\n{json.dumps(info.get('item') or {}, ensure_ascii=False, indent=2)[:5000]}\n```",
    )
    close_alert_issue(PREALERT_TITLE)
    close_alert_issue(HEALTH_TITLE)


def health_alert(details: dict[str, Any]) -> None:
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "cagdasdirek")
    ensure_alert_issue(
        HEALTH_TITLE,
        f"@{owner} The NVIDIA inventory source is temporarily failing or rate-limiting. "
        "The watcher has entered **safe cooldown mode** and will auto-probe/recover.\n\n"
        f"Manual check: {MARKETPLACE_URL}\n\nDetected: `{now_iso()}`\n\n"
        f"```json\n{json.dumps(details, ensure_ascii=False, indent=2)[:5000]}\n```",
    )


# ---------------- Smart interval / auto-heal ----------------

def bounded_jitter(base: int, spread: int) -> int:
    return max(15, base + random.randint(-spread, spread))


def error_cooldown(error_streak: int, info: dict[str, Any]) -> int:
    explicit = int(info.get("retry_after") or 0)
    status = info.get("http_status")
    if status == 403:
        base = 300
    elif status == 429:
        base = 120
    else:
        base = 45
    exponential = base * (2 ** max(0, error_streak - 1))
    return min(MAX_COOLDOWN, max(explicit, exponential))


def mode_interval(*, burst_left: int, stable_oos: int) -> int:
    if burst_left > 0:
        return bounded_jitter(BURST_INTERVAL, BURST_JITTER)
    if stable_oos >= STABLE_AFTER:
        return bounded_jitter(STABLE_INTERVAL, BASE_JITTER)
    return bounded_jitter(BASE_INTERVAL, BASE_JITTER)


def main() -> None:
    checks = max(1, int(os.environ.get("CHECKS_PER_RUN", "7")))
    discovery_every = max(1, int(os.environ.get("DISCOVERY_EVERY_CHECKS", "3")))
    page_every = max(1, int(os.environ.get("PAGE_EVERY_CHECKS", "7")))

    state = load_state()
    current_sku = str(state.get("sku") or "").strip() or FALLBACK_SKUS[0]
    current_upcs = normalize_upcs(state.get("upcs"))
    error_streak = int(state.get("inventory_error_streak") or 0)
    stable_oos = int(state.get("stable_oos") or 0)
    burst_left = int(state.get("burst_left") or 0)
    cooldown_until = parse_iso(state.get("cooldown_until"))
    dirty = False

    print(f"Marketplace: {MARKETPLACE_URL}", flush=True)
    print(f"Starting SKU: {current_sku}", flush=True)

    for index in range(checks):
        cycle = index + 1
        print(f"\n[{now_iso()}] ===== smart cycle {cycle}/{checks} =====", flush=True)

        # Circuit breaker: while cooling down, do not hammer inventory. Probe only when due.
        if cooldown_until and now() < cooldown_until:
            remaining = int((cooldown_until - now()).total_seconds())
            print(f"[{now_iso()}] SAFE COOLDOWN: {remaining}s remaining", flush=True)
            if index == 0 or index % page_every == 0:
                page_info, page_error = check_product_page()
                print(f"[{now_iso()}] fallback page: {page_info or page_error}", flush=True)
            sleep_for = min(max(20, remaining), 90)
            if index < checks - 1:
                time.sleep(sleep_for)
            continue

        # SKU discovery is deliberately slower than inventory polling.
        if index % discovery_every == 0:
            product, discovery_error = discover_product()
            if product:
                discovered_sku = product["sku"]
                discovered_upcs = product.get("upcs", [])
                previous_sku = str(state.get("sku") or "").strip()
                print(f"[{now_iso()}] discovery OK: sku={discovered_sku} upcs={discovered_upcs}", flush=True)
                if previous_sku and discovered_sku != previous_sku:
                    backend_change_alert(
                        "Product-search SKU changed",
                        {"old_sku": previous_sku, "new_sku": discovered_sku, "upcs": discovered_upcs},
                    )
                    burst_left = BURST_CYCLES
                    stable_oos = 0
                current_sku = discovered_sku
                current_upcs = discovered_upcs
                if previous_sku != discovered_sku or state.get("upcs") != discovered_upcs:
                    state["sku"] = discovered_sku
                    state["upcs"] = discovered_upcs
                    state["last_discovered"] = now_iso()
                    dirty = True
            else:
                print(f"[{now_iso()}] discovery degraded: {discovery_error}", flush=True)

        inv_state, inv_info = check_inventory(current_sku)
        print(f"[{now_iso()}] inventory: {inv_state} | {json.dumps(inv_info, ensure_ascii=False)[:1800]}", flush=True)

        if inv_state == "in_stock":
            error_streak = 0
            stable_oos = 0
            cooldown_until = None
            burst_left = BURST_CYCLES
            stock_alert(inv_info)
            close_alert_issue(HEALTH_TITLE)
        elif inv_state == "out_of_stock":
            if error_streak:
                print(f"[{now_iso()}] AUTO-HEAL: inventory recovered after {error_streak} errors", flush=True)
            error_streak = 0
            cooldown_until = None
            stable_oos += 1
            close_alert_issue(STOCK_TITLE)
            close_alert_issue(HEALTH_TITLE)
        elif inv_state == "empty":
            error_streak = 0
            stable_oos = 0
            burst_left = max(burst_left, BURST_CYCLES)
            backend_change_alert("Inventory listMap is empty for current live SKU", inv_info)
        else:
            stable_oos = 0
            error_streak += 1
            cooldown = error_cooldown(error_streak, inv_info)
            cooldown_until = now() + timedelta(seconds=cooldown)
            print(
                f"[{now_iso()}] CIRCUIT OPEN: status={inv_info.get('http_status')} "
                f"streak={error_streak}; cooldown={cooldown}s",
                flush=True,
            )
            health_alert(
                {
                    "inventory_error_streak": error_streak,
                    "cooldown_seconds": cooldown,
                    "cooldown_until": cooldown_until.isoformat(timespec="seconds"),
                    "current_sku": current_sku,
                    "last_inventory_error": inv_info,
                }
            )

        # Lightweight independent page health/MPN check, intentionally infrequent.
        if index % page_every == 0:
            page_info, page_error = check_product_page()
            print(f"[{now_iso()}] product page: {page_info or page_error}", flush=True)
            if page_info:
                page_mpn = str(page_info.get("mpn") or "").strip()
                known = str(state.get("sku") or current_sku).strip()
                if page_mpn and known and page_mpn != known:
                    backend_change_alert(
                        "Product-page MPN differs from last known SKU",
                        {"known_sku": known, "page_mpn": page_mpn},
                    )
                    burst_left = max(burst_left, BURST_CYCLES)

        if burst_left > 0:
            burst_left -= 1

        state.update(
            {
                "sku": current_sku,
                "upcs": current_upcs,
                "inventory_error_streak": error_streak,
                "stable_oos": stable_oos,
                "burst_left": burst_left,
                "cooldown_until": cooldown_until.isoformat(timespec="seconds") if cooldown_until else None,
                "last_cycle": now_iso(),
            }
        )
        dirty = True

        if index < checks - 1:
            interval = mode_interval(burst_left=burst_left, stable_oos=stable_oos)
            # If we just opened the circuit, next cycle waits at least the cooldown slice.
            if cooldown_until:
                interval = min(max(interval, 60), max(60, int((cooldown_until - now()).total_seconds())))
            print(
                f"[{now_iso()}] next check in {interval}s "
                f"(burst_left={burst_left}, stable_oos={stable_oos}, errors={error_streak})",
                flush=True,
            )
            time.sleep(interval)

    if dirty:
        save_state(state)


if __name__ == "__main__":
    main()
