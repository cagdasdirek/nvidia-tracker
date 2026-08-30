#!/usr/bin/env python3
"""Non-periodic runner for the RTX 5090 watcher.

This module deliberately keeps a single, consistent HTTP identity. It only
changes the *timing policy* used by tracker.py so requests are load-smoothed
rather than clock-like. It does not rotate fingerprints, proxies, cookies, or
other identifiers.

It also adds a strict purchase gate around tracker.py: a stock alert is only
allowed when NVIDIA inventory exposes both a positive availability signal and
an explicit HTTP(S) purchase URL. A bare product listing or stale cart text is
not sufficient.
"""

from __future__ import annotations

import math
import os
import random

import tracker

# Hard safety bounds for request cadence. These can be overridden from Actions
# but defaults are intentionally conservative.
NORMAL_MIN = int(os.environ.get("TIMING_NORMAL_MIN", "28"))
NORMAL_MAX = int(os.environ.get("TIMING_NORMAL_MAX", "95"))
STABLE_MIN = int(os.environ.get("TIMING_STABLE_MIN", "50"))
STABLE_MAX = int(os.environ.get("TIMING_STABLE_MAX", "130"))
BURST_MIN = int(os.environ.get("TIMING_BURST_MIN", "18"))
BURST_MAX = int(os.environ.get("TIMING_BURST_MAX", "48"))
IDLE_CHANCE = float(os.environ.get("TIMING_IDLE_CHANCE", "0.08"))
IDLE_MULT_MIN = float(os.environ.get("TIMING_IDLE_MULT_MIN", "1.35"))
IDLE_MULT_MAX = float(os.environ.get("TIMING_IDLE_MULT_MAX", "1.9"))

_last_interval: float | None = None


def _clamp(value: float, low: int, high: int) -> int:
    return max(low, min(high, int(round(value))))


def _lognormal_between(low: int, high: int, *, center_bias: float = 0.52) -> int:
    """Sample a bounded right-skewed delay without producing extreme tails."""
    low = max(1, low)
    high = max(low + 1, high)
    target = low + (high - low) * center_bias

    # A modest sigma gives meaningful variation while keeping most samples
    # around the useful middle of the range.
    sigma = 0.36
    mu = math.log(max(1.0, target)) - (sigma * sigma) / 2

    for _ in range(8):
        sample = random.lognormvariate(mu, sigma)
        if low <= sample <= high:
            return int(round(sample))

    return _clamp(sample, low, high)


def nonperiodic_interval(*, burst_left: int, stable_oos: int) -> int:
    """Drop-in replacement for tracker.mode_interval().

    Properties:
    - no fixed-period +/- jitter band;
    - bounded, right-skewed delays;
    - slight correlation with the previous delay so cadence does not alternate
      unnaturally between extremes;
    - occasional longer idle only during ordinary/stable OOS operation;
    - burst mode remains bounded and never gets the idle extension.
    """
    global _last_interval

    if burst_left > 0:
        low, high = BURST_MIN, BURST_MAX
        center_bias = 0.45
        allow_idle = False
    elif stable_oos >= tracker.STABLE_AFTER:
        low, high = STABLE_MIN, STABLE_MAX
        center_bias = 0.56
        allow_idle = True
    else:
        low, high = NORMAL_MIN, NORMAL_MAX
        center_bias = 0.50
        allow_idle = True

    fresh = _lognormal_between(low, high, center_bias=center_bias)

    # Decorrelate rather than fully resample each time: combine the new sample
    # with a small random portion of the previous interval. This avoids both a
    # metronomic cadence and implausible extreme-to-extreme alternation.
    if _last_interval is not None:
        memory = random.uniform(0.12, 0.32)
        fresh = _clamp((1.0 - memory) * fresh + memory * _last_interval, low, high)

    # Rare longer idle period while clearly OOS. This reduces unnecessary load.
    # Never use it in burst mode, where a backend/SKU signal may indicate a drop.
    if allow_idle and random.random() < IDLE_CHANCE:
        fresh = _clamp(
            fresh * random.uniform(IDLE_MULT_MIN, IDLE_MULT_MAX),
            low,
            int(high * IDLE_MULT_MAX),
        )

    _last_interval = float(fresh)
    return fresh


# Strict purchase gate: tracker.py's base detector intentionally treats any
# positive inventory bit as "in_stock". For notifications we require a real
# purchase destination as well, so an indexed button or backend-only signal
# cannot produce a false stock alert.
_original_check_inventory = tracker.check_inventory


def strict_check_inventory(sku: str):
    state, info = _original_check_inventory(sku)
    if state != "in_stock":
        return state, info

    item = info.get("item") if isinstance(info, dict) else None
    item = item if isinstance(item, dict) else {}
    purchase_url = next(
        (
            str(item.get(key) or "").strip()
            for key in ("product_url", "directPurchaseLink", "purchaseLink")
            if str(item.get(key) or "").strip().lower().startswith(("http://", "https://"))
        ),
        "",
    )

    if not purchase_url:
        return "out_of_stock", {
            "sku": sku,
            "entries": [item],
            "strict_reason": "positive inventory signal without an explicit purchasable URL",
        }

    info["buy_url"] = purchase_url
    info["strict_purchase_verified"] = True
    return "in_stock", info


# GitHub mobile notifications can depend on client settings. The real stock
# path therefore creates/assigns the issue as before and, only on first creation,
# adds a second @mention comment to generate an additional notification event.
_original_stock_alert = tracker.stock_alert


def reinforced_stock_alert(info: dict) -> None:
    existed = tracker.find_issue(tracker.STOCK_TITLE, open_only=True)
    _original_stock_alert(info)
    if existed:
        return

    created = tracker.find_issue(tracker.STOCK_TITLE, open_only=True)
    if not created:
        return

    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "cagdasdirek")
    number = created.get("number")
    try:
        tracker.github_request(
            "POST",
            f"/issues/{number}/comments",
            {
                "body": (
                    f"@{owner} 🚨 **SECOND ALERT: verified RTX 5090 FE purchase link is live.**\n\n"
                    f"BUY NOW: {info.get('buy_url')}"
                )
            },
        )
    except Exception as exc:
        print(f"[{tracker.now_iso()}] Secondary GitHub mention failed: {exc!r}", flush=True)


tracker.mode_interval = nonperiodic_interval
tracker.check_inventory = strict_check_inventory
tracker.stock_alert = reinforced_stock_alert

if __name__ == "__main__":
    tracker.main()
