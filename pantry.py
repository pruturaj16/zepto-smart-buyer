"""
pantry.py — Pantry Mode data model for Cart-Watch.

Stores pantry_lists inside history.json alongside the existing watchlist keys,
so GCS sync is automatic. Fully decoupled from watchlist.py.
"""
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config import WATCHLIST_PATH
from gcs_sync import push_state

IST                = ZoneInfo("Asia/Kolkata")
NUDGE_WINDOW_HOURS = 24   # hours after scheduled time we'll still attempt to nudge
AUTO_PAUSE_SKIPS   = 3    # consecutive skips before auto-pause
MAX_PRICE_HISTORY  = 168  # per pantry item — 7 days at 2-hour checks
MAX_NUDGE_HISTORY  = 52   # last 52 nudge records per list (~1 year of weekly data)

# How close to the historical minimum price before we consider it a good time
# to nudge (3% tolerance — nudge if current total ≤ weekly-min × 1.03)
PRICE_TOLERANCE_PCT = 0.03
MIN_PRICE_SAMPLES   = 5   # below this, always nudge regardless of price


# ── Internal I/O ─────────────────────────────────────────────────────────────

def _load_raw() -> dict:
    try:
        with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_raw(data: dict) -> None:
    tmp = WATCHLIST_PATH + ".tmp"
    os.makedirs(os.path.dirname(WATCHLIST_PATH), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, WATCHLIST_PATH)
    push_state()


# ── Public read/write ─────────────────────────────────────────────────────────

def load_pantry_lists() -> list:
    return _load_raw().get("pantry_lists", [])


def save_pantry_lists(pantry_lists: list) -> None:
    data = _load_raw()
    data["pantry_lists"] = pantry_lists
    _save_raw(data)


def get_list_by_id(list_id: str) -> dict | None:
    return next((l for l in load_pantry_lists() if l["id"] == list_id), None)


def get_list_by_name(name: str) -> dict | None:
    """Case-insensitive substring match."""
    name_lower = name.lower().strip()
    return next((l for l in load_pantry_lists() if name_lower in l["name"].lower()), None)


# ── CRUD: lists ───────────────────────────────────────────────────────────────

def add_pantry_list(
    name: str,
    cadence_days: int,
    nudge_day_of_week: int,   # 0 = Monday … 6 = Sunday
    nudge_time_ist: str,       # "HH:MM"
) -> dict:
    lists = load_pantry_lists()
    new_list = {
        "id":                 uuid.uuid4().hex[:8],
        "name":               name,
        "cadence_days":       cadence_days,
        "nudge_day_of_week":  nudge_day_of_week,
        "nudge_time_ist":     nudge_time_ist,
        "nudge_window_hours": NUDGE_WINDOW_HOURS,
        "paused":             False,
        "created_at":         datetime.now(timezone.utc).isoformat(),
        "last_nudged_at":     None,
        "last_ordered_at":    None,
        "consecutive_skips":  0,
        "items":              [],
        "nudge_history":      [],
    }
    lists.append(new_list)
    save_pantry_lists(lists)
    return new_list


def delete_pantry_list(list_id: str) -> bool:
    lists = load_pantry_lists()
    new_lists = [l for l in lists if l["id"] != list_id]
    if len(new_lists) == len(lists):
        return False
    save_pantry_lists(new_lists)
    return True


def set_paused(list_id: str, paused: bool) -> bool:
    lists = load_pantry_lists()
    for l in lists:
        if l["id"] == list_id:
            l["paused"] = paused
            save_pantry_lists(lists)
            return True
    return False


# ── CRUD: items ───────────────────────────────────────────────────────────────

def add_item(list_id: str, sku_id: str, name: str, qty: int, store_product_id: str) -> bool:
    lists = load_pantry_lists()
    for l in lists:
        if l["id"] == list_id:
            for item in l["items"]:
                if item["sku_id"] == sku_id:
                    item["qty"] = qty
                    save_pantry_lists(lists)
                    return True
            l["items"].append({
                "sku_id":            sku_id,
                "name":              name,
                "qty":               qty,
                "store_product_id":  store_product_id,
                "approved_substitute": None,   # {sku_id, store_product_id, name} or None
                "price_history":     [],        # [{price, timestamp, in_stock}]
            })
            save_pantry_lists(lists)
            return True
    return False


def remove_item(list_id: str, sku_id: str) -> bool:
    lists = load_pantry_lists()
    for l in lists:
        if l["id"] == list_id:
            before = len(l["items"])
            l["items"] = [i for i in l["items"] if i["sku_id"] != sku_id]
            if len(l["items"]) < before:
                save_pantry_lists(lists)
                return True
    return False


def set_substitute(list_id: str, sku_id: str, substitute: dict | None) -> bool:
    """
    substitute: {sku_id, store_product_id, name}  — or None to clear.
    """
    lists = load_pantry_lists()
    for l in lists:
        if l["id"] == list_id:
            for item in l["items"]:
                if item["sku_id"] == sku_id:
                    item["approved_substitute"] = substitute
                    save_pantry_lists(lists)
                    return True
    return False


# ── Price history (for smart nudge timing) ───────────────────────────────────

def update_item_price(list_id: str, sku_id: str, price: float | None, in_stock: bool) -> None:
    """Append a price sample — called on every price check run."""
    lists = load_pantry_lists()
    for l in lists:
        if l["id"] == list_id:
            for item in l["items"]:
                if item["sku_id"] == sku_id:
                    item.setdefault("price_history", []).append({
                        "price":     price,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "in_stock":  in_stock,
                    })
                    if len(item["price_history"]) > MAX_PRICE_HISTORY:
                        item["price_history"] = item["price_history"][-MAX_PRICE_HISTORY:]
                    save_pantry_lists(lists)
                    return


def is_good_time_to_nudge(pantry_list: dict, current_total: float) -> bool:
    """
    True if the current pantry cart total is at or near the historical low
    for this cadence window, making it an optimal time to nudge.

    Uses per-item min prices from the last cadence_days of samples, summed
    across the list. Falls back to True when there's insufficient history
    (< MIN_PRICE_SAMPLES in-stock entries per item).
    """
    items    = pantry_list.get("items", [])
    lookback = timedelta(days=pantry_list.get("cadence_days", 7))
    cutoff   = datetime.now(timezone.utc) - lookback

    historical_min_total = 0.0
    for item in items:
        recent = [
            e["price"]
            for e in item.get("price_history", [])
            if e.get("in_stock")
            and e.get("price") is not None
            and datetime.fromisoformat(e["timestamp"]) >= cutoff
        ]
        if len(recent) < MIN_PRICE_SAMPLES:
            return True   # Not enough data — nudge now
        historical_min_total += min(recent) * item["qty"]

    return current_total <= historical_min_total * (1 + PRICE_TOLERANCE_PCT)


# ── Nudge scheduling ──────────────────────────────────────────────────────────

def get_lists_due_for_nudge(now_ist: datetime) -> list:
    """Return pantry lists whose nudge window is currently active."""
    return [l for l in load_pantry_lists() if not l["paused"] and _is_in_nudge_window(l, now_ist)]


def _is_in_nudge_window(pantry_list: dict, now_ist: datetime) -> bool:
    """
    A list is in its nudge window when:
    1. The current weekday is >= nudge_day_of_week (mod cadence)
    2. Current time >= nudge_time_ist on that day
    3. We are within nudge_window_hours of that scheduled time
    4. We have NOT already nudged during this cycle (last_nudged_at is before window start)
    """
    nudge_dow    = pantry_list["nudge_day_of_week"]
    nudge_h, nudge_m = map(int, pantry_list["nudge_time_ist"].split(":"))
    window_hours = pantry_list.get("nudge_window_hours", NUDGE_WINDOW_HOURS)
    last_nudged  = pantry_list.get("last_nudged_at")

    # Find the most recent past occurrence of nudge_dow at nudge_time_ist
    days_ago = (now_ist.weekday() - nudge_dow) % 7
    target   = now_ist.replace(hour=nudge_h, minute=nudge_m, second=0, microsecond=0) \
               - timedelta(days=days_ago)

    # If today is the nudge day but we're before the nudge time, look back 7 days
    if days_ago == 0 and now_ist < target:
        target -= timedelta(days=7)

    window_end = target + timedelta(hours=window_hours)

    if not (target <= now_ist <= window_end):
        return False

    # Already nudged this cycle?
    if last_nudged:
        last_nudged_dt = datetime.fromisoformat(last_nudged).astimezone(IST)
        if last_nudged_dt >= target:
            return False

    return True


# ── Nudge outcome tracking ────────────────────────────────────────────────────

def record_nudge_sent(list_id: str, oos_items: list[str], cart_total: float) -> None:
    lists = load_pantry_lists()
    for l in lists:
        if l["id"] == list_id:
            now = datetime.now(timezone.utc).isoformat()
            l["last_nudged_at"] = now
            l.setdefault("nudge_history", []).append({
                "nudged_at":   now,
                "outcome":     "pending",
                "cart_total":  round(cart_total, 2),
                "oos_items":   oos_items,
                "resolved_at": None,
            })
            if len(l["nudge_history"]) > MAX_NUDGE_HISTORY:
                l["nudge_history"] = l["nudge_history"][-MAX_NUDGE_HISTORY:]
            save_pantry_lists(lists)
            return


def record_nudge_outcome(list_id: str, outcome: str) -> None:
    """outcome: 'ordered' | 'skipped'"""
    lists = load_pantry_lists()
    for l in lists:
        if l["id"] == list_id:
            for entry in reversed(l.get("nudge_history", [])):
                if entry.get("outcome") == "pending":
                    entry["outcome"]     = outcome
                    entry["resolved_at"] = datetime.now(timezone.utc).isoformat()
                    break
            if outcome == "ordered":
                l["last_ordered_at"]   = datetime.now(timezone.utc).isoformat()
                l["consecutive_skips"] = 0
            elif outcome == "skipped":
                l["consecutive_skips"] = l.get("consecutive_skips", 0) + 1
            save_pantry_lists(lists)
            return


def maybe_autopause(list_id: str) -> bool:
    """Pause the list after AUTO_PAUSE_SKIPS consecutive skips. Returns True if paused."""
    lists = load_pantry_lists()
    for l in lists:
        if l["id"] == list_id:
            if l.get("consecutive_skips", 0) >= AUTO_PAUSE_SKIPS:
                l["paused"] = True
                save_pantry_lists(lists)
                return True
    return False
