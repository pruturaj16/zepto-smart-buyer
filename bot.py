import json
import logging
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

import telegram
from telegram import (
    Update, InputMediaPhoto, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

from config import TELEGRAM_TOKEN, LOG_PATH
from watchlist import (
    load_watchlist,
    add_sku, remove_sku, remove_sku_by_id, compute_cart_total, get_latest_price,
    get_best_previous_total,
)
import zepto_agent
import pantry as pantry_store
from pantry_check import get_oos_suggestions, get_custom_search_suggestions
from zepto_auth import get_valid_token as get_cached_token
from gcs_sync import pull_state, pull_blob

_log_handler_file    = logging.FileHandler(LOG_PATH, encoding="utf-8")
_log_handler_console = logging.StreamHandler()
_log_fmt = logging.Formatter("%(asctime)s — %(levelname)s — %(message)s")
_log_handler_file.setFormatter(_log_fmt)
_log_handler_console.setFormatter(_log_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_log_handler_file, _log_handler_console])

# httpx/httpcore/telegram log full request URLs at INFO, and the Telegram Bot
# API embeds the bot token in the URL path (".../bot<TOKEN>/getMe") — silence
# them so the token never lands in the log file or systemd journal.
for _noisy in ("httpx", "httpcore", "telegram"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


# ── /pantry — show all pantry lists ──────────────────────────────────────────

_DAY_NAMES   = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_CADENCE_MAP = {7: "Weekly", 14: "Biweekly", 30: "Monthly"}


async def pantry_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /pantry              — show all lists
    /pantry new          — start setup wizard
    /pantry edit <name>  — add/remove items
    /pantry pause <name> — pause nudges
    /pantry resume <name>— resume nudges
    /pantry delete <name>— delete list (with confirm)
    """
    args = context.args or []
    sub  = args[0].lower() if args else ""

    if sub == "new":
        await _pantry_start_setup(update, context)
        return

    if sub in ("edit", "pause", "resume", "delete") and len(args) >= 2:
        name = " ".join(args[1:])
        lst  = pantry_store.get_list_by_name(name)
        if not lst:
            await update.message.reply_text(f"No pantry list found matching '{name}'.")
            return
        if sub == "pause":
            pantry_store.set_paused(lst["id"], True)
            await update.message.reply_text(f"⏸ Paused *{lst['name']}*.", parse_mode="Markdown")
        elif sub == "resume":
            pantry_store.set_paused(lst["id"], False)
            await update.message.reply_text(f"▶️ Resumed *{lst['name']}*.", parse_mode="Markdown")
        elif sub == "delete":
            keyboard = {"inline_keyboard": [[
                {"text": "🗑 Yes, delete", "callback_data": f"pantry:del_confirm:{lst['id']}"},
                {"text": "Cancel",         "callback_data": f"pantry:del_cancel"},
            ]]}
            await update.message.reply_text(
                f"Delete *{lst['name']}* and all its items?",
                parse_mode="Markdown", reply_markup=keyboard
            )
        elif sub == "edit":
            context.user_data["pantry_session"] = {
                "step":    "awaiting_items",
                "list_id": lst["id"],
            }
            await update.message.reply_text(
                f"✏️ Editing *{lst['name']}*.\n\n"
                "Type items to add (e.g. _Amul milk 1L x2_), or /remove to remove them.\n"
                "Send /done when finished.",
                parse_mode="Markdown",
            )
        return

    # Default: show all lists
    pull_state()
    lists = pantry_store.load_pantry_lists()
    if not lists:
        await update.message.reply_text(
            "No pantry lists yet.\nUse /pantry new to create one."
        )
        return

    lines = ["*Your pantry lists:*\n"]
    for l in lists:
        status   = "⏸ paused" if l["paused"] else f"next nudge: {_next_nudge_label(l)}"
        cadence  = _CADENCE_MAP.get(l["cadence_days"], f"every {l['cadence_days']}d")
        day_name = _DAY_NAMES[l["nudge_day_of_week"]]
        ordered  = l.get("last_ordered_at")
        ordered_label = (
            datetime.fromisoformat(ordered).astimezone(IST).strftime("%d %b") if ordered
            else "never"
        )
        lines.append(
            f"📋 *{l['name']}* — {len(l['items'])} items, {cadence} ({day_name}s)\n"
            f"   Last ordered: {ordered_label}  |  {status}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


def _next_nudge_label(pantry_list: dict) -> str:
    """Human-readable label for the next nudge time, e.g. 'Sun 17 Aug 9:00 AM'."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo as _ZI
    now   = datetime.now(_ZI("Asia/Kolkata"))
    dow   = pantry_list["nudge_day_of_week"]
    h, m  = map(int, pantry_list["nudge_time_ist"].split(":"))
    days_until = (dow - now.weekday()) % 7 or 7
    nxt   = (now + timedelta(days=days_until)).replace(hour=h, minute=m, second=0, microsecond=0)
    return nxt.strftime("%a %d %b %I:%M %p IST")


# ── /pantry new — multi-step wizard ──────────────────────────────────────────

async def _pantry_start_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["pantry_session"] = {"step": "awaiting_name"}
    await update.message.reply_text(
        "Let's set up a new pantry list!\n\nWhat do you want to call it?\n"
        "_(e.g. Weekly Grocery, Monthly Staples)_",
        parse_mode="Markdown",
    )


async def _pantry_handle_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = update.message.text.strip()
    context.user_data["pantry_session"]["name"] = name
    context.user_data["pantry_session"]["step"] = "awaiting_cadence"
    keyboard = {"inline_keyboard": [[
        {"text": "Every week",    "callback_data": "ps:cad:7"},
        {"text": "Every 2 weeks", "callback_data": "ps:cad:14"},
        {"text": "Every month",   "callback_data": "ps:cad:30"},
    ]]}
    await update.message.reply_text(
        f"Got it — *{name}*.\n\nHow often should I remind you?",
        parse_mode="Markdown", reply_markup=keyboard
    )


async def _pantry_handle_items(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route item text through zepto_agent but add results to the pantry list."""
    session  = context.user_data["pantry_session"]
    list_id  = session["list_id"]
    user_text = update.message.text.strip()

    await update.message.reply_text("Got it, looking these up on Zepto...")

    try:
        result = await zepto_agent.run(user_text)
    except Exception as e:
        logging.error(f"[Bot/Pantry] zepto_agent.run failed: {e}")
        await update.message.reply_text("Something went wrong — try again.")
        return

    if result.get("intent") != "add_items":
        await update.message.reply_text(
            "Couldn't read that as a list of items. Try something like:\n"
            "_Amul milk 1L x2, Eggoz 30 egg tray, Aashirvaad atta 5kg_",
            parse_mode="Markdown",
        )
        return

    candidates_by_item = result.get("candidates", {})
    for item_name, candidates in candidates_by_item.items():
        qty = next(
            (i["qty"] for i in result.get("items", []) if i["name"].lower() == item_name.lower()),
            1,
        )
        await _send_picker(update, context, item_name, qty, candidates,
                           destination={"type": "pantry", "list_id": list_id})
    for name in result.get("not_found", []):
        await update.message.reply_text(f"❌ {name} — not found on Zepto")


# ── /done — finish pantry item entry ─────────────────────────────────────────

async def done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = context.user_data.get("pantry_session", {})
    if session.get("step") != "awaiting_items":
        await update.message.reply_text("Nothing to finish right now.")
        return

    list_id = session["list_id"]
    lst     = pantry_store.get_list_by_id(list_id)
    context.user_data.pop("pantry_session", None)

    if not lst:
        await update.message.reply_text("List not found.")
        return

    h, m     = map(int, lst["nudge_time_ist"].split(":"))
    day_name = _DAY_NAMES[lst["nudge_day_of_week"]]
    await update.message.reply_text(
        f"✅ *{lst['name']}* is set up with {len(lst['items'])} item(s).\n"
        f"I'll nudge you every {day_name} at {lst['nudge_time_ist']} IST "
        f"when prices are at their lowest.",
        parse_mode="Markdown",
    )


# ── /start ────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Welcome to *Zepto Smart Buyer!*\n\n"
        "I monitor your grocery list and alert you when the total cart price "
        "drops by ₹50 or more compared to 2 hours ago.\n\n"
        "*How to use:*\n"
        "• Just type your shopping list naturally:\n"
        "  _Add Eggoz 30 egg tray, Amul milk 1L x2, Aashirvaad atta 5kg_\n\n"
        "• Remove items from your Zepto cart:\n"
        "  _Remove diet coke from my cart_ or _Clear my cart_\n\n"
        "• /list — see your watchlist with latest prices\n"
        "• /remove <item> — remove an item\n"
        "• /status — cart totals and last check time\n\n"
        "I check prices every 2 hours automatically and alert you when it's a good time to buy.",
        parse_mode="Markdown"
    )


# ── /list ─────────────────────────────────────────────────────────────────────

async def list_items(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pull_state()
    data = load_watchlist()
    if not data["skus"]:
        await update.message.reply_text(
            "Your watchlist is empty.\nJust type a shopping list to get started!"
        )
        return

    lines = ["*Your watchlist:*\n"]
    for sku in data["skus"]:
        latest = get_latest_price(sku)
        if latest:
            stock = "" if latest["in_stock"] else " _(out of stock)_"
            price_str = f"₹{latest['price']}{stock}"
        else:
            price_str = "_not checked yet_"
        lines.append(f"• {sku['name']} x{sku['qty']} — {price_str}")

    cart = compute_cart_total(data)
    lines.append(f"\n*Cart total: ₹{round(cart['total'])}*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /remove ───────────────────────────────────────────────────────────────────

async def _remove_from_live_cart(sku_id: str, token: str) -> bool:
    """
    Best-effort: look up the storeProductId for a known productVariantId
    (watchlist entries only persist the productVariantId) and zero it out
    in the live Zepto cart. Returns False on any failure so the caller can
    still confirm the watchlist-side removal without blocking on this.
    """
    try:
        details = await zepto_agent.execute_tool_calls(
            [{"tool": "get_product_details", "arguments": {"product_variant_id": sku_id}}], token
        )
        store_product_id = json.loads(details[0]["result_text"]).get("storeProductId")
        if not store_product_id:
            return False
        result = await zepto_agent.execute_tool_calls([{
            "tool": "update_cart",
            "arguments": {
                "deviceId": zepto_agent.DEVICE_ID,
                "cartItems": [{
                    "productVariantId": sku_id,
                    "storeProductId":   store_product_id,
                    "quantity":         0,
                }],
            },
        }], token)
        return "ERROR" not in result[0]["result_text"]
    except Exception as e:
        logging.error(f"[Bot] Failed to remove {sku_id} from live cart: {e}")
        return False


async def remove_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Usage: /remove <item name>\nExample: /remove Eggoz eggs"
        )
        return

    query = " ".join(context.args)
    result = remove_sku(query)

    if result["status"] != "removed":
        names = "\n".join(f"• {n}" for n in result["names"]) or "_(empty)_"
        await update.message.reply_text(
            f"No match for _{query}_.\n\nCurrent items:\n{names}",
            parse_mode="Markdown"
        )
        return

    token   = get_cached_token()
    cart_ok = await _remove_from_live_cart(result["id"], token)
    suffix  = "list and Zepto cart" if cart_ok else "list (couldn't confirm removal from the Zepto cart — check the app)"
    await update.message.reply_text(f"Removed *{result['name']}* from your {suffix}.", parse_mode="Markdown")


# ── /status ───────────────────────────────────────────────────────────────────

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pull_state()
    data = load_watchlist()
    cart = compute_cart_total(data)
    current  = round(cart["total"])
    checked  = data.get("previous_check_at", "Never")

    if checked != "Never":
        dt = datetime.fromisoformat(checked)
        checked = dt.astimezone(IST).strftime("%d %b %H:%M IST")

    current_sku_ids = sorted(s["id"] for s in data["skus"])
    previous, _ = get_best_previous_total(data, current_sku_ids, oos_names=cart["oos_items"])

    lines = ["*Cart status*\n", f"Last checked: {checked}", f"Current total: ₹{current}"]

    if previous is not None:
        diff = round(previous - current)
        arrow = "↓" if diff > 0 else ("↑" if diff < 0 else "=")
        lines.append(f"Previous total: ₹{round(previous)}  {arrow} ₹{abs(diff)}")
    else:
        lines.append("Previous total: first check pending")

    lines.append(f"\nItems tracked: {len(data['skus'])}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── Free text — routed through zepto_agent's plan/execute/analyze pipeline ──

async def _send_picker(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    item_name: str,
    qty,
    candidates: list,
    destination: dict | None = None,   # None → watchlist; {"type":"pantry","list_id":"..."} → pantry
) -> None:
    """
    Send a photo album of pre-fetched candidate SKUs + a "which one?" button
    prompt. Never auto-picks a SKU — a vague name (e.g. "bread") can
    silently resolve to the wrong product, so the user always confirms the
    exact SKU themselves. Candidates already carry price/image/stock from
    the search results (via zepto_agent) — no extra Zepto call needed here.
    """
    media = [
        InputMediaPhoto(
            media=c["image_url"],
            caption=f"{i + 1}. {c['name']}\n"
                    f"₹{c['price']}" + ("" if c["in_stock"] else " (out of stock)")
        )
        for i, c in enumerate(candidates)
        if c.get("image_url")
    ]

    if not media:
        await update.message.reply_text(f"❌ {item_name} — couldn't load product images, try again")
        return

    await update.message.reply_media_group(media=media)

    req_id = uuid.uuid4().hex[:8]
    context.user_data.setdefault("pending_picks", {})[req_id] = {
        "item_name":   item_name,
        "qty":         qty,
        "candidates":  candidates,
        "destination": destination or {"type": "watchlist"},
    }

    buttons = [
        InlineKeyboardButton(str(i + 1), callback_data=f"pick:{req_id}:{i}")
        for i in range(len(candidates))
    ]
    buttons.append(InlineKeyboardButton("Skip", callback_data=f"pick:{req_id}:skip"))

    await update.message.reply_text(
        f"Which *{item_name}* did you mean?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([buttons])
    )


async def _execute_cart_removal(update: Update, cart_items_to_remove: list, cart_not_found: list) -> None:
    """
    Removes items from the live Zepto cart via a single batched
    update_cart(quantity=0) call, then drops the same SKUs from the
    watchlist too (by productVariantId) — otherwise price_check.py keeps
    tracking and alerting on an item the user just removed from their cart.
    """
    if not cart_items_to_remove:
        lines = ["Nothing to remove — your cart may already be empty."]
        if cart_not_found:
            lines.append("\n*Not found in cart:*")
            lines += [f"  ❌ {n}" for n in cart_not_found]
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    token = get_cached_token()
    tool_calls = [{
        "tool": "update_cart",
        "arguments": {
            "deviceId": zepto_agent.DEVICE_ID,
            "cartItems": [
                {
                    "productVariantId": item["productVariantId"],
                    "storeProductId":   item["storeProductId"],
                    "quantity":         0,
                }
                for item in cart_items_to_remove
            ],
        },
    }]
    results = await zepto_agent.execute_tool_calls(tool_calls, token)

    if "ERROR" in results[0]["result_text"]:
        logging.error(f"[Bot] Cart removal failed: {results[0]['result_text']}")
        await update.message.reply_text("Couldn't update the cart — Zepto is busy right now, try again in a minute.")
        return

    for item in cart_items_to_remove:
        remove_sku_by_id(item["productVariantId"])

    lines = ["*Removed from cart & watchlist:*"] + [f"  ✅ {item['name']}" for item in cart_items_to_remove]
    if cart_not_found:
        lines.append("\n*Not found in cart:*")
        lines += [f"  ❌ {n}" for n in cart_not_found]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_text = update.message.text.strip()

    # ── Intercept active pantry session states ────────────────────────────────
    session = context.user_data.get("pantry_session", {})
    step    = session.get("step")

    if step == "awaiting_name":
        await _pantry_handle_name(update, context)
        return

    if step == "awaiting_items":
        await _pantry_handle_items(update, context)
        return

    if step == "awaiting_custom_sub":
        # User typed a custom substitute search query
        list_id  = session["list_id"]
        orig_sku = session["orig_sku_id"]
        context.user_data.pop("pantry_session", None)
        await _handle_custom_sub_search(update, context, list_id, orig_sku, user_text)
        return

    await update.message.reply_text("Got it, looking these up on Zepto...")

    try:
        result = await zepto_agent.run(user_text)
    except Exception as e:
        logging.error(f"[Bot] zepto_agent.run failed: {e}")
        await update.message.reply_text(f"Something went wrong: {str(e)}\nPlease try again.")
        return

    intent = result.get("intent", "unknown")

    if intent == "add_items":
        candidates_by_item = result.get("candidates", {})
        for item_name, candidates in candidates_by_item.items():
            qty = next(
                (i["qty"] for i in result.get("items", []) if i["name"].lower() == item_name.lower()),
                1
            )
            await _send_picker(update, context, item_name, qty, candidates)
        for name in result.get("not_found", []):
            await update.message.reply_text(f"❌ {name} — not found on Zepto")

    elif intent in ("remove_items", "clear_cart"):
        await _execute_cart_removal(
            update, result.get("cart_items_to_remove", []), result.get("cart_not_found", [])
        )

    else:
        await update.message.reply_text(
            "Couldn't understand that as a shopping request.\n"
            "Try: _Add Eggoz 30 egg tray, Amul milk 1L x2_ or _Remove milk from my cart_",
            parse_mode="Markdown"
        )


# ── Custom substitute search helper ──────────────────────────────────────────

async def _handle_custom_sub_search(
    update:   Update,
    context:  ContextTypes.DEFAULT_TYPE,
    list_id:  str,
    orig_sku: str,
    query:    str,
) -> None:
    """Search Zepto with a user-typed query and present results as a substitute picker."""
    await update.message.reply_text(f"Searching for '{query}'...")
    candidates = get_custom_search_suggestions(query)

    if not candidates:
        await update.message.reply_text(
            "No results found. Try a different query or tap /pantry to manage your list."
        )
        return

    # Build a simple text picker (no photos — custom search results don't carry image URLs)
    req_id = uuid.uuid4().hex[:8]
    context.user_data.setdefault("pantry_sub_picks", {})[req_id] = {
        "list_id":     list_id,
        "orig_sku_id": orig_sku,
        "candidates":  candidates,
    }

    lines = ["*Choose a substitute:*\n"]
    for i, c in enumerate(candidates, 1):
        tag   = " _(ordered before)_" if c.get("from_history") else ""
        stock = "" if c.get("in_stock") else " _(OOS)_"
        lines.append(f"{i}. {c['name']} — ₹{round(c['price'])}{tag}{stock}")

    buttons = [
        InlineKeyboardButton(str(i + 1), callback_data=f"psub:{req_id}:{i - 1}")
        for i in range(1, len(candidates) + 1)
    ]
    buttons.append(InlineKeyboardButton("Skip", callback_data=f"psub:{req_id}:skip"))

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([buttons]),
    )


async def _show_sub_picker(
    query:    object,      # telegram CallbackQuery
    context:  ContextTypes.DEFAULT_TYPE,
    list_id:  str,
    sku_id:   str,
) -> None:
    """
    Show substitute suggestions for an OOS pantry item.
    Called when the user taps the '🔍 Substitute for …' button in a nudge message.
    """
    # Look up the pantry item name from the list
    lst  = pantry_store.get_list_by_id(list_id)
    item = next((i for i in (lst or {}).get("items", []) if i["sku_id"].startswith(sku_id)), None)
    item_name = item["name"] if item else sku_id
    full_sku  = item["sku_id"] if item else sku_id

    await query.message.reply_text(f"Finding substitutes for *{item_name}*…", parse_mode="Markdown")
    candidates = get_oos_suggestions(item_name)

    if not candidates:
        # No auto-suggestions — prompt user to type a custom query
        context.user_data["pantry_session"] = {
            "step":        "awaiting_custom_sub",
            "list_id":     list_id,
            "orig_sku_id": full_sku,
        }
        await query.message.reply_text(
            f"Couldn't find auto-suggestions for *{item_name}*.\n\n"
            "Type what you're looking for — e.g. _'eggs under ₹200'_ or _'organic eggs'_",
            parse_mode="Markdown",
        )
        return

    req_id = uuid.uuid4().hex[:8]
    context.user_data.setdefault("pantry_sub_picks", {})[req_id] = {
        "list_id":     list_id,
        "orig_sku_id": full_sku,
        "candidates":  candidates,
    }

    lines = [f"*Substitutes for {item_name}:*\n"]
    for i, c in enumerate(candidates, 1):
        tag   = " _(ordered before)_" if c.get("from_history") else ""
        stock = "" if c.get("in_stock") else " _(OOS)_"
        lines.append(f"{i}. {c['name']} — ₹{round(c['price'])}{tag}{stock}")

    row_picks = [
        InlineKeyboardButton(str(i + 1), callback_data=f"psub:{req_id}:{i - 1}")
        for i in range(1, len(candidates) + 1)
    ]
    row_custom = [
        InlineKeyboardButton("🔎 Type my own search", callback_data=f"psub_custom:{list_id}:{full_sku[:20]}")
    ]

    await query.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([row_picks, row_custom]),
    )


# ── Inline button callbacks — Yes / Skip ─────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    # ── Pantry setup wizard callbacks (cadence / day / time) ─────────────────
    if query.data.startswith("ps:"):
        await query.edit_message_reply_markup(reply_markup=None)
        parts   = query.data.split(":")
        ps_type = parts[1]
        val     = parts[2] if len(parts) > 2 else ""
        session = context.user_data.get("pantry_session", {})

        if ps_type == "cad":
            session["cadence_days"] = int(val)
            session["step"]         = "awaiting_day"
            context.user_data["pantry_session"] = session
            rows = [[
                {"text": d, "callback_data": f"ps:day:{i}"}
                for i, d in enumerate(_DAY_NAMES)
            ]]
            await query.message.reply_text(
                "Which day should I nudge you?",
                reply_markup={"inline_keyboard": rows},
            )

        elif ps_type == "day":
            session["nudge_day_of_week"] = int(val)
            session["step"]              = "awaiting_time"
            context.user_data["pantry_session"] = session
            times = [("7 AM", "07:00"), ("9 AM", "09:00"), ("11 AM", "11:00"),
                     ("6 PM", "18:00"), ("8 PM", "20:00")]
            rows  = [[{"text": label, "callback_data": f"ps:time:{hm}"} for label, hm in times]]
            await query.message.reply_text(
                "What time should I nudge you?",
                reply_markup={"inline_keyboard": rows},
            )

        elif ps_type == "time":
            session["nudge_time_ist"] = val
            # Create the list now
            new_list = pantry_store.add_pantry_list(
                name              = session.get("name", "My Pantry"),
                cadence_days      = session.get("cadence_days", 7),
                nudge_day_of_week = session.get("nudge_day_of_week", 6),
                nudge_time_ist    = val,
            )
            session["step"]    = "awaiting_items"
            session["list_id"] = new_list["id"]
            context.user_data["pantry_session"] = session
            day_name = _DAY_NAMES[new_list["nudge_day_of_week"]]
            await query.message.reply_text(
                f"✅ Created *{new_list['name']}*!\n\n"
                f"Now add your items — just type them naturally:\n"
                f"_Amul milk 1L x3, Eggoz 30 egg tray, Aashirvaad atta 5kg_\n\n"
                f"Send /done when you're finished. I'll nudge you every {day_name} "
                f"at {val} IST when prices are at their lowest.",
                parse_mode="Markdown",
            )
        return

    # ── Pantry nudge action callbacks ─────────────────────────────────────────
    if query.data.startswith("pantry:"):
        await query.edit_message_reply_markup(reply_markup=None)
        parts  = query.data.split(":")
        action = parts[1]

        if action == "load" and len(parts) >= 3:
            list_id = parts[2]
            await _pantry_load_cart(query, context, list_id)

        elif action == "skip" and len(parts) >= 3:
            list_id = parts[2]
            pantry_store.record_nudge_outcome(list_id, "skipped")
            was_paused = pantry_store.maybe_autopause(list_id)
            lst = pantry_store.get_list_by_id(list_id)
            name = lst["name"] if lst else list_id
            if was_paused:
                await query.message.reply_text(
                    f"⏸ *{name}* paused after {pantry_store.AUTO_PAUSE_SKIPS} consecutive skips.\n"
                    "Resume with /pantry resume.",
                    parse_mode="Markdown",
                )
            else:
                await query.message.reply_text(
                    f"Skipped — see you next week for *{name}*! 👋",
                    parse_mode="Markdown",
                )

        elif action == "edit" and len(parts) >= 3:
            list_id = parts[2]
            lst     = pantry_store.get_list_by_id(list_id)
            if lst:
                context.user_data["pantry_session"] = {
                    "step":    "awaiting_items",
                    "list_id": list_id,
                }
                await query.message.reply_text(
                    f"✏️ Editing *{lst['name']}*. Type items to add, or send /done when finished.",
                    parse_mode="Markdown",
                )

        elif action == "sub" and len(parts) >= 4:
            list_id  = parts[2]
            sku_id   = parts[3]
            await _show_sub_picker(query, context, list_id, sku_id)

        elif action == "del_confirm" and len(parts) >= 3:
            list_id = parts[2]
            lst     = pantry_store.get_list_by_id(list_id)
            name    = lst["name"] if lst else list_id
            pantry_store.delete_pantry_list(list_id)
            await query.message.reply_text(f"🗑 *{name}* deleted.", parse_mode="Markdown")

        elif action == "del_cancel":
            await query.message.reply_text("Cancelled.")

        return

    # ── Pantry substitute picker callbacks ────────────────────────────────────
    if query.data.startswith("psub_custom:"):
        await query.edit_message_reply_markup(reply_markup=None)
        _, list_id, sku_short = query.data.split(":", 2)
        # Find the full sku_id from the list
        lst     = pantry_store.get_list_by_id(list_id)
        item    = next((i for i in (lst or {}).get("items", []) if i["sku_id"].startswith(sku_short)), None)
        full_sk = item["sku_id"] if item else sku_short
        context.user_data["pantry_session"] = {
            "step":        "awaiting_custom_sub",
            "list_id":     list_id,
            "orig_sku_id": full_sk,
        }
        await query.message.reply_text(
            "Type what you're looking for — e.g. _'eggs under ₹200'_ or _'organic eggs'_",
            parse_mode="Markdown",
        )
        return

    if query.data.startswith("psub:"):
        await query.edit_message_reply_markup(reply_markup=None)
        parts  = query.data.split(":")
        req_id = parts[1]
        choice = parts[2] if len(parts) > 2 else "skip"

        pending = context.user_data.get("pantry_sub_picks", {}).pop(req_id, None)
        if pending is None:
            await query.message.reply_text("This selection has expired — tap 🔍 again in the nudge.")
            return

        if choice == "skip":
            await query.message.reply_text("Skipped — the item won't be loaded to cart this time.")
            return

        selected  = pending["candidates"][int(choice)]
        list_id   = pending["list_id"]
        orig_sku  = pending["orig_sku_id"]

        # Get store_product_id via get_product_details (needed for cart update)
        token = get_cached_token()
        try:
            details = await zepto_agent.execute_tool_calls(
                [{"tool": "get_product_details", "arguments": {"product_variant_id": selected["sku_id"]}}],
                token,
            )
            import json as _json
            store_product_id = _json.loads(details[0]["result_text"]).get("storeProductId", "")
        except Exception:
            store_product_id = ""

        sub = {
            "sku_id":           selected["sku_id"],
            "store_product_id": store_product_id,
            "name":             selected["name"],
        }

        # Ask if this should be the permanent substitute
        context.user_data.setdefault("pantry_sub_perm_pending", {})[req_id] = {
            "list_id":  list_id,
            "orig_sku": orig_sku,
            "sub":      sub,
        }
        keyboard = {"inline_keyboard": [[
            {"text": "✅ Yes, always use this",  "callback_data": f"psub_perm:{req_id}:yes"},
            {"text": "No, just this time",       "callback_data": f"psub_perm:{req_id}:no"},
        ]]}
        await query.message.reply_text(
            f"Got it — *{selected['name']}* (₹{round(selected['price'])}) selected.\n\n"
            "Save this as the permanent substitute for future nudges?",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        return

    if query.data.startswith("psub_perm:"):
        await query.edit_message_reply_markup(reply_markup=None)
        _, req_id, save = query.data.split(":", 2)
        pending = context.user_data.get("pantry_sub_perm_pending", {}).pop(req_id, None)
        if not pending:
            await query.message.reply_text("Session expired.")
            return
        if save == "yes":
            pantry_store.set_substitute(pending["list_id"], pending["orig_sku"], pending["sub"])
            await query.message.reply_text(
                f"✅ *{pending['sub']['name']}* saved as permanent substitute.",
                parse_mode="Markdown",
            )
        else:
            await query.message.reply_text("Got it — just for this order.")
        return

    # ── Original watchlist pick callbacks ─────────────────────────────────────
    await query.edit_message_reply_markup(reply_markup=None)

    if query.data.startswith("pick:"):
        _, req_id, choice = query.data.split(":", 2)
        pending = context.user_data.get("pending_picks", {}).pop(req_id, None)

        if pending is None:
            await query.message.reply_text("This selection has expired — please send the item again.")
            return

        if choice == "skip":
            await query.message.reply_text(f"Skipped {pending['item_name']}.")
            return

        selected = pending["candidates"][int(choice)]
        name = selected["name"] or pending["item_name"]

        # Route to pantry or watchlist depending on where the pick originated
        dest = pending.get("destination", {"type": "watchlist"})
        if dest.get("type") == "pantry":
            pantry_store.add_item(
                dest["list_id"],
                selected["sku_id"],
                name,
                pending["qty"],
                selected.get("store_product_id", ""),
            )
            await query.message.reply_text(
                f"✅ Added *{name}* × {pending['qty']} to your pantry list.\n"
                "Type more items or send /done when finished.",
                parse_mode="Markdown",
            )
            return

        add_sku(name, pending["qty"], selected["sku_id"])

        token = get_cached_token()
        cart_result = await zepto_agent.execute_tool_calls([{
            "tool": "update_cart",
            "arguments": {
                "deviceId": zepto_agent.DEVICE_ID,
                "cartItems": [{
                    "productVariantId": selected["sku_id"],
                    "storeProductId":   selected["store_product_id"],
                    "quantity":         pending["qty"],
                }],
            },
        }], token)

        if "ERROR" in cart_result[0]["result_text"]:
            logging.error(f"[Bot] Cart add failed for {name}: {cart_result[0]['result_text']}")
            await query.message.reply_text(
                f"⚠️ Added *{name}* x{pending['qty']} to your watchlist, but couldn't add it to "
                "your Zepto cart — Zepto is busy right now, try again in a minute.",
                parse_mode="Markdown"
            )
            return

        await query.message.reply_text(
            f"✅ Added *{name}* x{pending['qty']} — ₹{selected['price']} to your Zepto cart and watchlist.\n\n"
            "I'll check prices every 2 hours and alert you when the cart drops by ₹50+.",
            parse_mode="Markdown"
        )
        return

    # Note: the price-drop alert (send_alert) is now informational only —
    # items are already kept in the live Zepto cart from the moment they're
    # added to the watchlist (see the "pick:" branch above), so there's no
    # more "yes, add to cart" / "skip" button to handle here.


# ── Pantry load-cart action ───────────────────────────────────────────────────

async def _pantry_load_cart(query, context: ContextTypes.DEFAULT_TYPE, list_id: str) -> None:
    """
    Push all in-stock pantry items (including approved substitutes) to the
    live Zepto cart via update_cart, then record the nudge outcome as 'ordered'.
    """
    lst = pantry_store.get_list_by_id(list_id)
    if not lst:
        await query.message.reply_text("List not found.")
        return

    token = get_cached_token()

    # Fetch current prices to identify OOS items and use substitutes
    sku_list      = [{"id": i["sku_id"], "name": i["name"], "qty": i["qty"]} for i in lst["items"]]
    price_results = {}
    try:
        from price_check import fetch_all_prices
        price_results = fetch_all_prices(sku_list)
    except Exception as e:
        logging.error(f"[Bot/Pantry] fetch_all_prices failed: {e}")

    cart_items = []
    skipped    = []

    for item in lst["items"]:
        result = price_results.get(item["sku_id"])
        if result and result.get("in_stock"):
            cart_items.append({
                "productVariantId": item["sku_id"],
                "storeProductId":   item["store_product_id"],
                "quantity":         item["qty"],
            })
        else:
            sub = item.get("approved_substitute")
            if sub:
                sub_result = price_results.get(sub["sku_id"])
                if sub_result and sub_result.get("in_stock"):
                    cart_items.append({
                        "productVariantId": sub["sku_id"],
                        "storeProductId":   sub["store_product_id"],
                        "quantity":         item["qty"],
                    })
                    continue
            skipped.append(item["name"])

    if not cart_items:
        await query.message.reply_text(
            "⚠️ All items are currently out of stock — nothing was loaded to cart."
        )
        return

    result = await zepto_agent.execute_tool_calls([{
        "tool": "update_cart",
        "arguments": {
            "deviceId":  zepto_agent.DEVICE_ID,
            "cartItems": cart_items,
        },
    }], token)

    if "ERROR" in result[0]["result_text"]:
        logging.error(f"[Bot/Pantry] Cart load failed: {result[0]['result_text']}")
        await query.message.reply_text(
            "Couldn't load the cart — Zepto is busy, try again in a minute."
        )
        return

    pantry_store.record_nudge_outcome(list_id, "ordered")

    lines = [f"✅ *{len(cart_items)} item(s) loaded into your Zepto cart!*",
             "Open the app to check out. 🛒"]
    if skipped:
        lines.append(f"\n⚠️ Skipped (OOS, no sub): {', '.join(skipped)}")
    await query.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    pull_state()
    pull_blob(zepto_agent.CONTEXT_PATH, "order_context.txt")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("list",   list_items))
    app.add_handler(CommandHandler("remove", remove_item))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("pantry", pantry_cmd))
    app.add_handler(CommandHandler("done",   done_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    logging.info("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    try:
        main()
    except telegram.error.InvalidToken:
        # The library's own exception embeds the raw token in its message —
        # swallow it here so an invalid/rotated token doesn't get printed to
        # the journal via the default traceback.
        logging.error("TELEGRAM_TOKEN was rejected by the Telegram API — check config.py / Secret Manager.")
        raise SystemExit(1)
