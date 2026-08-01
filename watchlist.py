import json
import os
import re
from datetime import datetime, timezone

from config import WATCHLIST_PATH
from gcs_sync import push_state


def _parse_qty(qty) -> int:
    """
    Safely parse qty to a plain integer.
    Handles: 2, "2", "1 pack (10 pcs)", "1 pc (500 ml)", "1 set", etc.
    Always returns at least 1.
    """
    if isinstance(qty, int):
        return max(1, qty)
    m = re.match(r"(\d+)", str(qty).strip())
    return int(m.group(1)) if m else 1


def load_watchlist() -> dict:
    if not os.path.exists(WATCHLIST_PATH):
        data = {
            "previous_check_at": None,
            "cart_total_history": [],
            "last_alerted_at": None,
            "pending_oos_prompt": None,
            "skus": []
        }
        save_watchlist(data)
        return data

    with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    # ── Schema migration: old single-value previous_cart_total → cart_total_history ──
    if "cart_total_history" not in data:
        data["cart_total_history"] = []
        old_total = data.get("previous_cart_total")
        old_at    = data.get("previous_check_at")
        if old_total is not None:
            data["cart_total_history"].append({
                "total":     round(old_total, 2),
                "timestamp": old_at or datetime.now(timezone.utc).isoformat(),
                "sku_ids":   sorted(s["id"] for s in data.get("skus", [])),
                "has_oos":   False,
                "oos_names": []
            })
        data.pop("previous_cart_total", None)

    # ── Ensure new top-level keys exist on older files ────────────────────────
    data.setdefault("last_alerted_at",   None)
    data.setdefault("pending_oos_prompt", None)
    data.setdefault("previous_check_at", None)

    # ── Normalize per-SKU history entries (handle camelCase + missing keys) ──
    for sku in data.get("skus", []):
        # Ensure qty is a clean integer (handles "1 pack (10 pcs)", "1 set", etc.)
        sku["qty"] = _parse_qty(sku.get("qty", 1))

        for entry in sku.get("history", []):
            # inStock  →  in_stock
            if "inStock" in entry and "in_stock" not in entry:
                entry["in_stock"] = entry.pop("inStock")
            entry.setdefault("in_stock", True)
            # date  →  timestamp
            if "date" in entry and "timestamp" not in entry:
                entry["timestamp"] = entry.pop("date") + "T00:00:00+00:00"
            entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
            # Ensure price is a number if present (strip ₹ symbol if it exists)
            if "price" in entry and entry["price"] is not None:
                price_str = str(entry["price"]).replace("₹", "").strip()
                try:
                    entry["price"] = float(price_str)
                except ValueError:
                    entry["price"] = None  # Mark as invalid if conversion fails

    return data


def save_watchlist(data: dict) -> None:
    os.makedirs(os.path.dirname(WATCHLIST_PATH), exist_ok=True)
    tmp_path = WATCHLIST_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, WATCHLIST_PATH)
    push_state()


def add_sku(name: str, qty, sku_id: str) -> dict:
    data = load_watchlist()
    qty = _parse_qty(qty)  # Safely parse qty from any format

    for sku in data["skus"]:
        if sku["id"] == sku_id:
            sku["qty"] = qty
            save_watchlist(data)
            return {"status": "updated", "sku": sku}

    new_sku = {"id": sku_id, "name": name, "qty": qty, "history": []}
    data["skus"].append(new_sku)
    # Basket composition changed — historical totals are no longer comparable
    data["cart_total_history"] = []
    data["last_alerted_at"]    = None
    print(f"  [watchlist] New SKU added — cart history reset (basket changed).")
    save_watchlist(data)
    return {"status": "added", "sku": new_sku}


def remove_sku(query: str) -> dict:
    data = load_watchlist()
    query_lower = query.lower().strip()
    match = next((s for s in data["skus"] if query_lower in s["name"].lower()), None)
    if not match:
        return {"status": "not_found", "names": [s["name"] for s in data["skus"]]}
    data["skus"] = [s for s in data["skus"] if s["id"] != match["id"]]
    # Basket composition changed — historical totals are no longer comparable
    data["cart_total_history"] = []
    data["last_alerted_at"]    = None
    print(f"  [watchlist] SKU removed — cart history reset (basket changed).")
    save_watchlist(data)
    return {"status": "removed", "name": match["name"]}


def get_latest_price(sku: dict) -> dict | None:
    if not sku["history"]:
        return None
    return sku["history"][-1]


def append_price(sku_id: str, price: float | None, in_stock: bool) -> None:
    data = load_watchlist()
    for sku in data["skus"]:
        if sku["id"] == sku_id:
            sku["history"].append({
                "price":     price,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "in_stock":  in_stock
            })
            if len(sku["history"]) > 100:
                sku["history"] = sku["history"][-100:]
            break
    save_watchlist(data)


def append_cart_total(
    data: dict,
    total: float,
    has_oos: bool,
    oos_names: list[str] | None = None
) -> None:
    """Append a cart-total snapshot to the rolling history (mutates data in place)."""
    entry = {
        "total":     round(total, 2),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sku_ids":   sorted(s["id"] for s in data["skus"]),
        "has_oos":   has_oos,
        "oos_names": sorted(oos_names or [])
    }
    data.setdefault("cart_total_history", [])
    data["cart_total_history"].append(entry)
    # Keep at most 500 snapshots (~3 weeks at 2-hour intervals)
    if len(data["cart_total_history"]) > 500:
        data["cart_total_history"] = data["cart_total_history"][-500:]


def get_best_previous_total(
    data: dict,
    current_sku_ids: list[str],
    oos_names: list[str] | None = None
) -> tuple[float | None, str | None]:
    """
    Return (highest_previous_total, timestamp) from cart_total_history that:
      - Matches current basket composition (sku_ids)
      - Matches OOS pattern (empty list for clean runs, name list for OOS runs)
      - Is recorded AFTER last_alerted_at (so alerts don't repeat endlessly)
      - Is NOT the most-recently appended entry (that's the current run's snapshot)

    Returns (None, None) if no valid baseline exists.
    """
    history          = data.get("cart_total_history", [])
    last_alerted_at  = data.get("last_alerted_at")
    current_sku_set  = sorted(current_sku_ids)
    current_oos_set  = sorted(oos_names or [])

    # Exclude the last entry — it was just recorded for the current run
    candidates = history[:-1] if len(history) > 1 else []

    # Only consider baselines recorded AFTER the last alert was fired.
    # We use >= so that the entry which *triggered* the alert (same timestamp)
    # is excluded, preventing the same baseline from triggering again.
    if last_alerted_at:
        candidates = [e for e in candidates if e.get("timestamp", "") > last_alerted_at]

    # Match basket + OOS pattern
    matching = [
        e for e in candidates
        if sorted(e.get("sku_ids",   [])) == current_sku_set
        and sorted(e.get("oos_names", [])) == current_oos_set
    ]

    if not matching:
        return None, None

    # The entry with the highest total gives us the biggest potential saving
    best = max(matching, key=lambda e: e["total"])
    return best["total"], best["timestamp"]


def compute_cart_total(data: dict) -> dict:
    """
    Returns:
        total     — sum of (price × qty) for all in-stock items with a valid price
        items     — list of line-item dicts (name, qty, price, in_stock)
        oos_items — list of names for items that are OOS or have no price
    """
    total     = 0.0
    line_items = []
    oos_items  = []

    for sku in data["skus"]:
        latest   = get_latest_price(sku)
        price    = latest["price"]   if latest else None
        in_stock = latest.get("in_stock", True) if latest else False
        qty      = int(sku.get("qty", 1))  # Ensure qty is integer, default 1

        # Guard: API returned in_stock=True but price is missing (data glitch)
        if in_stock and price is None:
            print(f"  WARNING: '{sku['name']}' marked in_stock but price is None — treating as OOS.")
            in_stock = False

        line_items.append({
            "name":     sku["name"],
            "qty":      qty,
            "price":    price,
            "in_stock": in_stock
        })

        if in_stock and price is not None:
            total += float(price) * qty
        else:
            oos_items.append(sku["name"])

    return {
        "total":     round(total, 2),
        "items":     line_items,
        "oos_items": oos_items
    }
