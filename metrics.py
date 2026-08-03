"""
metrics.py — computes dashboard metrics from local state (watchlist price
history + alert log) and the cached Zepto order history. Called by
price_check.py after each run; writes metrics.json, synced to GCS for the
dashboard Cloud Function to read.
"""
import json
import logging
import os
from datetime import datetime, timezone

from config import WATCHLIST_PATH
from watchlist import load_watchlist, get_latest_price
from orders_sync import sync_order_history
from gcs_sync import push_blob

logger = logging.getLogger(__name__)

METRICS_PATH = os.path.join(os.path.dirname(os.path.abspath(WATCHLIST_PATH)), "metrics.json")
_BLOB_NAME   = "metrics.json"

ALERT_EXPIRY_HOURS = 24  # a pending alert this old with no button tap counts as "ignored"


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _order_metrics(orders: list) -> dict:
    valid = [o for o in orders if (o.get("status") or "").upper() != "CANCELLED"]

    total_spent = sum(o["grand_total"] for o in valid)
    total_saved = sum(o["total_saved"] for o in valid)

    discount_pcts = [
        (o["total_saved"] / o["grand_total_pre_discount"]) * 100
        for o in valid
        if o.get("grand_total_pre_discount")
    ]
    avg_discount_pct = round(sum(discount_pcts) / len(discount_pcts), 1) if discount_pcts else None

    placed_dates = [o["placed_at"] for o in valid if o.get("placed_at")]
    days_since_last_order = None
    if placed_dates:
        last = max(_parse_iso(d) for d in placed_dates)
        days_since_last_order = (datetime.now(timezone.utc) - last).days

    return {
        "order_count":           len(valid),
        "total_spent":           round(total_spent, 2),
        "total_savings":         round(total_saved, 2),
        "avg_discount_pct":      avg_discount_pct,
        "days_since_last_order": days_since_last_order,
        "spend_timeline": [
            {"placed_at": o["placed_at"], "amount": o["grand_total"]}
            for o in sorted(valid, key=lambda o: o.get("placed_at") or "")
            if o.get("placed_at")
        ],
    }


def _alert_conversion(alerts: list) -> dict:
    now = datetime.now(timezone.utc)
    acted = skipped = expired = pending = 0

    for a in alerts:
        outcome = a.get("outcome")
        if outcome == "acted":
            acted += 1
        elif outcome == "skipped":
            skipped += 1
        elif outcome == "pending":
            try:
                age_hours = (now - _parse_iso(a["sent_at"])).total_seconds() / 3600
            except Exception:
                age_hours = 0
            if age_hours >= ALERT_EXPIRY_HOURS:
                expired += 1
            else:
                pending += 1

    resolved = acted + skipped + expired
    conversion_rate = round((acted / resolved) * 100, 1) if resolved else None

    return {
        "alerts_sent":         len(alerts),
        "alerts_acted":        acted,
        "alerts_skipped":      skipped,
        "alerts_expired":      expired,
        "alerts_pending":      pending,
        "conversion_rate_pct": conversion_rate,
    }


def _price_volatility(skus: list) -> list:
    results = []
    for sku in skus:
        prices = [
            e["price"] for e in sku.get("history", [])
            if e.get("in_stock") and e.get("price") is not None
        ]
        if len(prices) < 2:
            continue
        lo, hi, avg = min(prices), max(prices), sum(prices) / len(prices)
        if avg <= 0:
            continue
        results.append({
            "name":           sku["name"],
            "min_price":      lo,
            "max_price":      hi,
            "volatility_pct": round((hi - lo) / avg * 100, 1),
            "data_points":    len(prices),
        })
    return sorted(results, key=lambda r: r["volatility_pct"], reverse=True)


def _cart_total_timeline(cart_total_history: list) -> list:
    return [
        {"timestamp": e["timestamp"], "total": e["total"]}
        for e in cart_total_history
        if not e.get("has_oos")
    ][-100:]  # last 100 clean snapshots — enough for a useful trend line


def _current_watchlist_value(skus: list) -> float:
    total = 0.0
    for sku in skus:
        latest = get_latest_price(sku)
        if latest and latest.get("in_stock") and latest.get("price"):
            total += latest["price"] * sku.get("qty", 1)
    return round(total, 2)


def compute_metrics() -> dict:
    data   = load_watchlist()
    orders = sync_order_history()

    return {
        "generated_at":        datetime.now(timezone.utc).isoformat(),
        **_order_metrics(orders),
        **_alert_conversion(data.get("alerts", [])),
        "price_volatility":     _price_volatility(data.get("skus", [])),
        "cart_total_timeline":  _cart_total_timeline(data.get("cart_total_history", [])),
        "watchlist_value":      _current_watchlist_value(data.get("skus", [])),
        "items_tracked":        len(data.get("skus", [])),
    }


def write_metrics() -> None:
    metrics = compute_metrics()
    os.makedirs(os.path.dirname(METRICS_PATH), exist_ok=True)
    tmp_path = METRICS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, METRICS_PATH)
    push_blob(METRICS_PATH, _BLOB_NAME)
    logger.info(
        f"[Metrics] Wrote metrics.json — {metrics['order_count']} orders, "
        f"₹{metrics['total_savings']} saved, "
        f"{metrics['conversion_rate_pct']}% alert conversion"
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(compute_metrics(), indent=2))
