import logging
import requests
from datetime import datetime, timezone

from config import TELEGRAM_TOKEN, CHAT_ID, DROP_THRESHOLD, LOG_PATH

# File + console logging — appends to same log as bot.py
_log_handler_file    = logging.FileHandler(LOG_PATH, encoding="utf-8")
_log_handler_console = logging.StreamHandler()
_log_fmt = logging.Formatter("%(asctime)s — %(levelname)s — %(message)s")
_log_handler_file.setFormatter(_log_fmt)
_log_handler_console.setFormatter(_log_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_log_handler_file, _log_handler_console])
from watchlist import (
    load_watchlist, save_watchlist,
    append_price, compute_cart_total, get_latest_price,
    append_cart_total, get_best_previous_total
)
from zepto_mcp import search_product, search_products_batch

# Try importing direct (free) MCP client — falls back to Anthropic if unavailable
try:
    from direct_zepto import get_prices_batch as _direct_prices
    _DIRECT_AVAILABLE = True
    logging.info("[Init] direct_zepto available — price checks will be FREE ✅")
except ImportError:
    _DIRECT_AVAILABLE = False
    logging.warning("[Init] mcp package not installed — falling back to Anthropic API")


# ── Telegram helpers ──────────────────────────────────────────────────────────

def _tg_post(method: str, payload: dict) -> dict | None:
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}",
        json=payload,
        timeout=10
    )
    if resp.ok:
        return resp.json()
    logging.info(f"  [Telegram] {method} failed: {resp.text}")
    return None


def send_telegram(text: str, keyboard: dict | None = None) -> dict | None:
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    if keyboard:
        payload["reply_markup"] = keyboard
    return _tg_post("sendMessage", payload)


def poll_callback(pending_message_id: int) -> str | None:
    """
    Poll Telegram for callback-query responses to a specific message.
    Returns 'yes' or 'no' if the user has responded, None if not yet.
    Acknowledges and consumes all matching updates.
    """
    resp = requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
        params={"timeout": 0, "allowed_updates": ["callback_query"]},
        timeout=10
    )
    if not resp.ok:
        logging.info(f"  [Telegram] getUpdates failed: {resp.text}")
        return None

    answer          = None
    last_update_id  = None

    for update in resp.json().get("result", []):
        last_update_id = update["update_id"]
        cb = update.get("callback_query", {})
        msg_id = cb.get("message", {}).get("message_id")

        if msg_id == pending_message_id:
            raw = cb.get("data", "").lower()
            answer = "yes" if raw == "yes" else "no"
            # Acknowledge so the spinner disappears in the Telegram UI
            _tg_post("answerCallbackQuery", {"callback_query_id": cb["id"]})

    # Mark all fetched updates as consumed so they don't repeat
    if last_update_id is not None:
        requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": last_update_id + 1, "timeout": 0},
            timeout=10
        )

    return answer


# ── Alert senders ─────────────────────────────────────────────────────────────

def send_oos_prompt(oos_names: list[str], partial_total: float) -> int | None:
    """
    Notify the user that some SKUs are OOS and ask whether to record/compare
    this partial cart total. Returns the Telegram message_id if sent.
    """
    bullet_list = "\n".join(f"  • {n}" for n in oos_names)
    text = (
        f"⚠️ *Some items are currently out of stock:*\n{bullet_list}\n\n"
        f"Cart total _(excluding OOS items)_: ₹{round(partial_total)}\n\n"
        "Should I record this partial total and compare it on future OOS runs?"
    )
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Yes, record it", "callback_data": "yes"},
        {"text": "❌ No, skip",       "callback_data": "no"}
    ]]}
    result = send_telegram(text, keyboard)
    if result:
        return result["result"]["message_id"]
    return None


def send_alert(
    current_total:  float,
    previous_total: float,
    previous_at:    str,
    items:          list
) -> bool:
    """Send a price-drop alert. Returns True if Telegram accepted it."""
    drop = round(previous_total - current_total)

    # Human-readable baseline label
    try:
        prev_dt    = datetime.fromisoformat(previous_at)
        prev_label = prev_dt.astimezone().strftime("%d %b %I:%M %p")
    except Exception:
        prev_label = "a previous check"

    lines = [f"🛒 *Your cart is ₹{drop} cheaper than {prev_label}.*\n"]

    for item in items:
        if not item["in_stock"] or item["price"] is None:
            lines.append(f"  {item['name']} x{item['qty']}   _out of stock_")
            continue
        prev  = item.get("previous_price")
        now   = round(item["price"] * item["qty"])
        if prev and prev != item["price"]:
            was   = round(prev * item["qty"])
            arrow = "↓" if item["price"] < prev else "↑"
            lines.append(f"  {item['name']} x{item['qty']}   ₹{now}   (was ₹{was}) {arrow}")
        else:
            lines.append(f"  {item['name']} x{item['qty']}   ₹{now}   (no change)")

    lines += [
        f"\n{'─' * 32}",
        f"  Cart total now:    ₹{round(current_total)}",
        f"  As of {prev_label}:  ₹{round(previous_total)}",
        f"  You save:          ₹{drop}",
        f"{'─' * 32}",
        "\nAdd items to cart?"
    ]

    keyboard = {"inline_keyboard": [[
        {"text": "🛒 Add to cart", "callback_data": "yes"},
        {"text": "❌ Skip",        "callback_data": "skip"}
    ]]}

    result = send_telegram("\n".join(lines), keyboard)
    if result:
        logging.info(f"  Alert sent — ₹{drop} drop vs {prev_label}")
        return True
    return False


# ── Price fetching ────────────────────────────────────────────────────────────

def fetch_all_prices(skus: list) -> dict:
    """
    Fetch prices for all SKUs.
    - If mcp package is installed: calls Zepto directly (FREE, no LLM)
    - Fallback: uses Anthropic API batch search
    Returns dict mapping sku_id -> {price, in_stock} or None.
    """
    if not skus:
        return {}

    # ── Path A: Direct MCP (FREE) ─────────────────────────────────────────────
    if _DIRECT_AVAILABLE:
        logging.info(f"[Price] Fetching {len(skus)} prices via direct MCP (free)...")
        try:
            return _direct_prices(skus)
        except Exception as e:
            logging.warning(f"[Price] Direct MCP failed ({e}) — falling back to Anthropic API")

    # ── Path B: Anthropic API batch (fallback, costs ~$0.001/call) ────────────
    logging.info(f"[Price] Fetching {len(skus)} prices via Anthropic API (fallback)...")
    items      = [{"name": sku["name"], "qty": 1} for sku in skus]
    sku_id_map = {sku["name"]: sku["id"] for sku in skus}

    try:
        batch_result = search_products_batch(items)
    except Exception as e:
        logging.error(f"[Price] Batch fetch failed — {e}")
        return {sku["id"]: None for sku in skus}

    results = {}

    for item in batch_result.get("success", []):
        sku_id = sku_id_map.get(item["name"])
        if sku_id:
            results[sku_id] = {
                "price":    float(item["price"]),
                "in_stock": bool(item.get("in_stock", True))
            }
            logging.info(f"  {item['name']}: ₹{item['price']} (in_stock={item['in_stock']})")

    for item in batch_result.get("failed", []):
        sku_id = sku_id_map.get(item["name"])
        if sku_id:
            results[sku_id] = None
            logging.info(f"  {item['name']}: fetch failed — {item.get('reason', 'not found')}")

    for sku in skus:
        if sku["id"] not in results:
            results[sku["id"]] = None

    return results


# ── Main run loop ─────────────────────────────────────────────────────────────

def run():
    logging.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Starting price check...")

    data = load_watchlist()

    if not data["skus"]:
        logging.info("  Watchlist is empty — nothing to check.")
        return

    # ── Step 1: Process any pending OOS prompt response ───────────────────────
    pending = data.get("pending_oos_prompt")
    if pending:
        msg_id = pending.get("message_id")
        logging.info(f"  Pending OOS prompt (msg_id={msg_id}) — checking for response...")
        answer = poll_callback(msg_id)

        if answer == "yes":
            # Record the partial total from that previous OOS run as a baseline
            append_cart_total(
                data,
                total     = pending["partial_total"],
                has_oos   = True,
                oos_names = pending.get("oos_names", [])
            )
            data["pending_oos_prompt"] = None
            save_watchlist(data)
            logging.info(f"  User said YES — OOS baseline ₹{round(pending['partial_total'])} recorded.")

        elif answer == "no":
            data["pending_oos_prompt"] = None
            save_watchlist(data)
            logging.info("  User said NO — OOS run discarded, no baseline recorded.")

        else:
            # Still waiting — reload fresh data and continue without blocking
            logging.info("  No response yet — proceeding with fresh price check.")
            data = load_watchlist()

    # ── Step 2: Fetch current prices ──────────────────────────────────────────
    data = load_watchlist()
    results = fetch_all_prices(data["skus"])

    for sku in data["skus"]:
        result = results.get(sku["id"])
        if result:
            append_price(sku["id"], result["price"], result.get("in_stock", True))
        else:
            # API fetch failed — preserve last known price as OOS entry
            latest = get_latest_price(sku)
            if latest:
                append_price(sku["id"], latest["price"], False)

    # ── Step 3: Compute cart total (excludes OOS items) ───────────────────────
    data = load_watchlist()
    cart          = compute_cart_total(data)
    current_total = cart["total"]
    oos_items     = cart["oos_items"]

    # Enrich items with the most-recent PREVIOUS in-stock price for per-item arrows
    for item in cart["items"]:
        sku_obj = next((s for s in data["skus"] if s["name"] == item["name"]), None)
        if sku_obj:
            in_stock_history = [
                e for e in sku_obj["history"]
                if e.get("in_stock") and e.get("price") is not None
            ]
            item["previous_price"] = (
                in_stock_history[-2]["price"] if len(in_stock_history) >= 2
                else item["price"]
            )

    # ── Step 4: Guard — every SKU is out of stock ─────────────────────────────
    all_oos = all(not item["in_stock"] for item in cart["items"])
    if all_oos:
        logging.info("  All items are OOS — skipping comparison to avoid a false alert.")
        send_telegram(
            "⚠️ *All watchlist items are currently out of stock.*\n"
            "Price check skipped — nothing to compare."
        )
        return

    # ── Step 5: Handle partial OOS ────────────────────────────────────────────
    if oos_items:
        logging.info(f"  OOS items: {oos_items}")
        current_sku_ids = sorted(s["id"] for s in data["skus"])

        # Check if there's an approved baseline for exactly this OOS pattern
        prev_oos_total, prev_oos_at = get_best_previous_total(
            data, current_sku_ids, oos_names=oos_items
        )

        if prev_oos_total is not None:
            # We have a user-approved OOS baseline → compare
            drop = prev_oos_total - current_total
            logging.info(
                f"  OOS cart: ₹{round(current_total)} | "
                f"OOS baseline: ₹{round(prev_oos_total)} | Drop: ₹{round(drop)}"
            )
            append_cart_total(data, current_total, has_oos=True, oos_names=oos_items)
            if drop >= DROP_THRESHOLD:
                alerted = send_alert(current_total, prev_oos_total, prev_oos_at, cart["items"])
                if alerted:
                    data["last_alerted_at"] = datetime.now(timezone.utc).isoformat()
            else:
                logging.info(f"  Drop ₹{round(drop)} below ₹{DROP_THRESHOLD} threshold — no alert.")
        else:
            # No baseline yet — ask the user
            logging.info(f"  No OOS baseline for this pattern — sending prompt to user.")
            msg_id = send_oos_prompt(oos_items, current_total)
            if msg_id:
                data["pending_oos_prompt"] = {
                    "message_id":   msg_id,
                    "partial_total": current_total,
                    "oos_names":    oos_items,
                    "asked_at":     datetime.now(timezone.utc).isoformat()
                }
                logging.info("  Prompt sent — awaiting user response.")
            else:
                logging.info("  Failed to send OOS prompt.")

        data["previous_check_at"] = datetime.now(timezone.utc).isoformat()
        save_watchlist(data)
        logging.info("Done.")
        return

    # ── Step 6: Fully in-stock — compare against ALL clean historical totals ──
    current_sku_ids = sorted(s["id"] for s in data["skus"])

    # Record the current total FIRST, then compare (get_best excludes last entry)
    append_cart_total(data, current_total, has_oos=False)
    save_watchlist(data)
    data = load_watchlist()

    previous_total, previous_at = get_best_previous_total(data, current_sku_ids)

    if previous_total is None:
        logging.info(f"  First clean run — baseline ₹{round(current_total)} recorded.")
    else:
        drop = previous_total - current_total
        logging.info(
            f"  Cart: ₹{round(current_total)} | "
            f"Best baseline: ₹{round(previous_total)} | Drop: ₹{round(drop)}"
        )
        if drop >= DROP_THRESHOLD:
            alerted = send_alert(current_total, previous_total, previous_at, cart["items"])
            if alerted:
                # Reset the comparison window so we don't re-alert every run
                data["last_alerted_at"] = datetime.now(timezone.utc).isoformat()
                save_watchlist(data)
        else:
            logging.info(f"  Drop ₹{round(drop)} below ₹{DROP_THRESHOLD} threshold — no alert.")

    data["previous_check_at"] = datetime.now(timezone.utc).isoformat()
    save_watchlist(data)
    logging.info("Done.")


if __name__ == "__main__":
    run()
