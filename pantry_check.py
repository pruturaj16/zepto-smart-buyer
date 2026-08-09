"""
pantry_check.py — Pantry Mode nudge engine for Cart-Watch.

Called from price_check.run() after each 2-hour price check, so it
piggybacks on the existing Cloud Scheduler / GCP VM infrastructure with
no additional deployment needed.

For each pantry list whose nudge window is active:
  1. Fetches current prices for all pantry items (free, direct MCP).
  2. Stores price samples for smart nudge timing.
  3. Skips nudge if prices are above the historical low for this cycle
     (waits for a cheaper window within the 24-hour nudge grace period).
  4. For OOS items, ranks substitutes by:
       a. Products the user has ordered before (order_context.txt match)
       b. Other results sorted by price (ratings not available via MCP)
  5. Sends the Telegram nudge with inline buttons.
"""
import logging
import os
import re
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import anthropic

from config import ANTHROPIC_API_KEY, CHAT_ID, TELEGRAM_TOKEN, WATCHLIST_PATH
from gcs_sync import pull_state
from pantry import (
    get_list_by_id,
    get_lists_due_for_nudge,
    is_good_time_to_nudge,
    maybe_autopause,
    record_nudge_sent,
    update_item_price,
)
from price_check import fetch_all_prices
from zepto_auth import get_valid_token as get_cached_token

IST = ZoneInfo("Asia/Kolkata")

logger = logging.getLogger(__name__)

ORDER_CONTEXT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(WATCHLIST_PATH)), "order_context.txt"
)


# ── Order history helpers ─────────────────────────────────────────────────────

def _load_order_history_names() -> set[str]:
    """
    Return lowercased past-ordered product names from order_context.txt.
    Used to give priority to substitute suggestions the user has bought before.
    """
    if not os.path.exists(ORDER_CONTEXT_PATH):
        return set()
    with open(ORDER_CONTEXT_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()
    return {line.strip().lstrip("- ").strip().lower() for line in lines if line.strip()}


def _matches_order_history(candidate_name: str, past_names: set[str]) -> bool:
    """True if candidate_name overlaps with any past-ordered product name."""
    cand_lower = candidate_name.lower()
    return any(
        past in cand_lower or cand_lower in past
        for past in past_names
    )


# ── Category extraction (for OOS substitute search) ──────────────────────────

def _extract_category(item_name: str, order_history_text: str = "") -> str:
    """
    Ask Claude Haiku for a 1-3 word search query that would find substitute
    products for item_name. When order_history_text is provided (raw contents
    of order_context.txt), Claude biases the query toward brands the user has
    previously ordered. Falls back to the first word of the item name.

    Example (with history showing Eggoz/Henfruit orders):
      "Eggoz 30 egg tray" → "Eggoz eggs"  (vs. generic "eggs" without history)
    """
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        if order_history_text:
            system = (
                "You suggest short Zepto grocery search queries (1-3 words). "
                "The user has previously ordered these products:\n"
                f"{order_history_text}\n\n"
                "When the OOS item belongs to a category where the user has ordered "
                "specific brands before, bias the search query toward those brands. "
                "Reply with ONLY the search query, nothing else."
            )
        else:
            system = "Reply with ONLY a 1-3 word Zepto search query, nothing else."

        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=15,
            system=system,
            messages=[{
                "role": "user",
                "content": f"Find substitutes for: '{item_name}'",
            }],
        )
        query = msg.content[0].text.strip().strip('"').strip("'").lower()
        return query if query else item_name.split()[0].lower()
    except Exception as e:
        logger.warning(f"[Pantry] _extract_category failed ({e}), falling back to first word")
        return item_name.split()[0].lower()


# ── OOS substitute suggestions ────────────────────────────────────────────────

def get_oos_suggestions(oos_item_name: str) -> list[dict]:
    """
    Find up to 3 substitute suggestions for an OOS pantry item.

    Priority order:
      1. Products the user has previously ordered (order_context.txt match)
      2. Other search results, sorted by price ascending
         (Zepto MCP does not expose ratings, so price is the fallback signal)

    Returns list of {name, sku_id, price, in_stock, from_history}.
    store_product_id is fetched later (when user confirms the sub) to avoid
    extra API calls for suggestions the user never picks.
    """
    past_names = _load_order_history_names()

    # Load raw order history for the Haiku system prompt so it can bias
    # the search query toward brands the user has actually ordered before.
    order_history_text = ""
    if os.path.exists(ORDER_CONTEXT_PATH):
        with open(ORDER_CONTEXT_PATH, "r", encoding="utf-8") as f:
            order_history_text = f.read()

    category_query = _extract_category(oos_item_name, order_history_text)
    logger.info(f"  [Pantry] OOS sub search: '{category_query}' (for '{oos_item_name}')")

    try:
        from zepto_mcp import search_products_batch
        result = search_products_batch([{"name": category_query, "qty": 1}])
        raw_candidates = result.get("success", [])
    except Exception as e:
        logger.warning(f"  [Pantry] search_products_batch failed: {e}")
        return []

    # Filter out the OOS item itself (name overlap)
    oos_lower = oos_item_name.lower()
    candidates = [
        c for c in raw_candidates
        if oos_lower not in c["name"].lower() and c["name"].lower() not in oos_lower
    ]

    # Annotate with history flag and split into two buckets
    from_history = []
    others       = []
    for c in candidates:
        enriched = {**c, "from_history": _matches_order_history(c["name"], past_names)}
        if enriched["from_history"]:
            from_history.append(enriched)
        else:
            others.append(enriched)

    # Sort each bucket by price (cheapest first — proxy for value without ratings)
    from_history.sort(key=lambda x: x["price"])
    others.sort(key=lambda x: x["price"])

    return (from_history + others)[:3]


# ── Price filtering for custom search ─────────────────────────────────────────

def _parse_price_ceiling(query: str) -> float | None:
    """
    Extract a price ceiling from a user query string.
    Handles patterns like 'under 250', 'below ₹200', '< 300', 'less than 150'.
    Returns the ceiling as a float, or None if not found.
    """
    m = re.search(
        r"(?:under|below|less\s+than|<|max|upto|up\s+to)\s*[₹]?\s*(\d+(?:\.\d+)?)",
        query,
        re.IGNORECASE,
    )
    return float(m.group(1)) if m else None


def get_custom_search_suggestions(query: str) -> list[dict]:
    """
    Search Zepto with a user-supplied query string (e.g., 'organic eggs under 200').
    Applies any price ceiling mentioned in the query, then ranks by order history.
    """
    past_names    = _load_order_history_names()
    price_ceiling = _parse_price_ceiling(query)

    try:
        from zepto_mcp import search_products_batch
        result     = search_products_batch([{"name": query, "qty": 1}])
        candidates = result.get("success", [])
    except Exception as e:
        logger.warning(f"  [Pantry] custom search failed: {e}")
        return []

    if price_ceiling is not None:
        candidates = [c for c in candidates if c["price"] <= price_ceiling]

    for c in candidates:
        c["from_history"] = _matches_order_history(c["name"], past_names)

    candidates.sort(key=lambda x: (0 if x["from_history"] else 1, x["price"]))
    return candidates[:3]


# ── Telegram nudge message ────────────────────────────────────────────────────

def send_pantry_nudge(
    pantry_list:  dict,
    in_stock:     list[dict],   # [{name, qty, price, sku_id, store_product_id}]
    oos_with_subs: list[dict],  # [{sku_id, name, qty, suggestions: [{...}]}]
    total:        float,
) -> dict | None:
    """
    Build and send the pantry nudge Telegram message.
    Returns the raw Telegram API response dict, or None on failure.
    """
    list_id = pantry_list["id"]
    lines   = [f"🛒 *{pantry_list['name']} — time to restock!*\n"]

    for item in in_stock:
        subtotal = round(item["price"] * item["qty"])
        lines.append(f"✅ {item['name']} × {item['qty']} — ₹{subtotal}")

    for item in oos_with_subs:
        lines.append(f"⚠️ {item['name']} — *OUT OF STOCK*")
        subs = item.get("suggestions", [])
        if subs:
            best = subs[0]
            tag  = " _(ordered before)_" if best.get("from_history") else ""
            lines.append(f"   → {best['name']} — ₹{round(best['price'])}{tag}")
        else:
            lines.append("   → No substitute found")

    lines.append(f"\n*Cart total (in-stock items): ₹{round(total)}*")

    text = "\n".join(lines)

    # ── Inline keyboard ───────────────────────────────────────────────────────
    row_main = [
        {"text": "✅ Load cart & order", "callback_data": f"pantry:load:{list_id}"},
        {"text": "✏️ Edit list",         "callback_data": f"pantry:edit:{list_id}"},
        {"text": "⏭ Skip this week",    "callback_data": f"pantry:skip:{list_id}"},
    ]
    rows = [row_main]

    for item in oos_with_subs:
        # Telegram callback_data limit is 64 bytes
        sku_short = item["sku_id"][:20]
        rows.append([{
            "text":          f"🔍 Substitute for {item['name'][:22]}",
            "callback_data": f"pantry:sub:{list_id}:{sku_short}",
        }])

    payload = {
        "chat_id":      CHAT_ID,
        "text":         text,
        "parse_mode":   "Markdown",
        "reply_markup": {"inline_keyboard": rows},
    }
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json=payload,
        timeout=10,
    )
    if resp.ok:
        return resp.json()
    logger.warning(f"  [Pantry] Telegram nudge failed: {resp.text}")
    return None


# ── Per-list check logic ──────────────────────────────────────────────────────

def _check_pantry_list(pantry_list: dict, token: str) -> None:
    list_id = pantry_list["id"]
    items   = pantry_list.get("items", [])

    if not items:
        logger.info(f"  [Pantry] '{pantry_list['name']}' has no items — skipping.")
        return

    logger.info(f"  [Pantry] Checking '{pantry_list['name']}' ({len(items)} items)...")

    # ── Fetch current prices ──────────────────────────────────────────────────
    sku_list      = [{"id": i["sku_id"], "name": i["name"], "qty": i["qty"]} for i in items]
    price_results = fetch_all_prices(sku_list)

    # Store price samples (used by is_good_time_to_nudge on future runs)
    for item in items:
        result = price_results.get(item["sku_id"])
        if result:
            update_item_price(list_id, item["sku_id"], result["price"], result.get("in_stock", False))
        else:
            update_item_price(list_id, item["sku_id"], None, False)

    # ── Partition: in-stock vs OOS (with approved-substitute override) ────────
    in_stock_items = []
    oos_items      = []

    for item in items:
        result = price_results.get(item["sku_id"])
        if result and result.get("in_stock") and result.get("price") is not None:
            in_stock_items.append({**item, "price": result["price"]})
        else:
            # Use pre-approved substitute if available and in-stock
            sub = item.get("approved_substitute")
            if sub:
                sub_result = price_results.get(sub["sku_id"])
                if sub_result and sub_result.get("in_stock"):
                    in_stock_items.append({
                        "name":             f"{sub['name']} _(sub for {item['name']})_",
                        "sku_id":           sub["sku_id"],
                        "store_product_id": sub["store_product_id"],
                        "qty":              item["qty"],
                        "price":            sub_result["price"],
                    })
                    logger.info(f"    Approved sub active: {item['name']} → {sub['name']}")
                    continue
            oos_items.append(item)

    current_total = sum(i["price"] * i["qty"] for i in in_stock_items)

    # ── Smart timing: skip if prices are above the historical low ─────────────
    # Re-read the list from disk so we have the updated price_history we just wrote.
    fresh_list = get_list_by_id(list_id) or pantry_list
    if not is_good_time_to_nudge(fresh_list, current_total):
        # Check if the nudge window is more than halfway through — if so, nudge anyway
        # to avoid the user missing the week entirely.
        last_nudged = fresh_list.get("last_nudged_at")
        window_h    = fresh_list.get("nudge_window_hours", NUDGE_WINDOW_HOURS)
        if last_nudged:
            hours_since = (
                datetime.now(timezone.utc) - datetime.fromisoformat(last_nudged)
            ).total_seconds() / 3600
            if hours_since < (fresh_list.get("cadence_days", 7) * 24) * 0.9:
                logger.info(
                    f"  [Pantry] '{fresh_list['name']}' ₹{round(current_total)} above "
                    f"historical low — waiting for better price window."
                )
                return
        logger.info(
            f"  [Pantry] '{fresh_list['name']}' past timing deadline — nudging anyway "
            f"(₹{round(current_total)})."
        )
    else:
        logger.info(
            f"  [Pantry] '{fresh_list['name']}' ₹{round(current_total)} is at/near "
            f"weekly low — nudging now."
        )

    # ── Get OOS substitute suggestions ───────────────────────────────────────
    oos_with_subs = []
    for item in oos_items:
        subs = get_oos_suggestions(item["name"])
        oos_with_subs.append({**item, "suggestions": subs})
        logger.info(
            f"    OOS '{item['name']}': {len(subs)} suggestion(s) "
            f"({sum(1 for s in subs if s.get('from_history'))} from order history)"
        )

    # ── Send nudge ────────────────────────────────────────────────────────────
    logger.info(
        f"  [Pantry] Sending nudge '{fresh_list['name']}': "
        f"{len(in_stock_items)} in-stock, {len(oos_items)} OOS, ₹{round(current_total)}"
    )
    resp = send_pantry_nudge(fresh_list, in_stock_items, oos_with_subs, current_total)
    if resp:
        record_nudge_sent(list_id, [i["name"] for i in oos_items], current_total)
        logger.info(f"  [Pantry] Nudge sent for '{fresh_list['name']}'.")
    else:
        logger.error(f"  [Pantry] Failed to send nudge for '{fresh_list['name']}'.")


# ── Entry point ───────────────────────────────────────────────────────────────

def run() -> None:
    logger.info(
        f"[{datetime.now(IST).strftime('%Y-%m-%d %H:%M IST')}] Pantry check starting..."
    )
    pull_state()

    now_ist    = datetime.now(IST)
    due_lists  = get_lists_due_for_nudge(now_ist)

    if not due_lists:
        logger.info("  [Pantry] No lists due for nudge.")
        return

    token = get_cached_token()

    for i, pantry_list in enumerate(due_lists):
        if i > 0:
            import time
            time.sleep(60)   # Avoid Telegram rate limits between multiple lists
        _check_pantry_list(pantry_list, token)

    logger.info("  [Pantry] Check complete.")


if __name__ == "__main__":
    run()
