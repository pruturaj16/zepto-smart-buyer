import json
import logging
import re
import uuid
from datetime import datetime, timezone

import telegram
from telegram import (
    Update, InputMediaPhoto, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

from config import TELEGRAM_TOKEN, LOG_PATH
from parse_list import parse_shopping_list
from watchlist import (
    load_watchlist, save_watchlist,
    add_sku, remove_sku, compute_cart_total, get_latest_price
)
from zepto_mcp import find_candidates, add_to_cart, add_to_cart_and_order, remove_from_cart, ZeptoRateLimited
from direct_zepto import get_product_details_async
from zepto_auth import get_valid_token as get_cached_token
from gcs_sync import pull_state

import os
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

async def remove_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Usage: /remove <item name>\nExample: /remove Eggoz eggs"
        )
        return

    query = " ".join(context.args)
    result = remove_sku(query)

    if result["status"] == "removed":
        await update.message.reply_text(
            f"Removed *{result['name']}* from your list.", parse_mode="Markdown"
        )
    else:
        names = "\n".join(f"• {n}" for n in result["names"]) or "_(empty)_"
        await update.message.reply_text(
            f"No match for _{query}_.\n\nCurrent items:\n{names}",
            parse_mode="Markdown"
        )


# ── /status ───────────────────────────────────────────────────────────────────

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_watchlist()
    cart = compute_cart_total(data)
    current  = round(cart["total"])
    previous = data.get("previous_cart_total")
    checked  = data.get("previous_check_at", "Never")

    if checked != "Never":
        dt = datetime.fromisoformat(checked)
        checked = dt.strftime("%d %b %H:%M")

    lines = ["*Cart status*\n", f"Last checked: {checked}", f"Current total: ₹{current}"]

    if previous is not None:
        diff = round(previous - current)
        arrow = "↓" if diff > 0 else ("↑" if diff < 0 else "=")
        lines.append(f"Previous total: ₹{round(previous)}  {arrow} ₹{abs(diff)}")
    else:
        lines.append("Previous total: first check pending")

    lines.append(f"\nItems tracked: {len(data['skus'])}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── Free text — shopping list parser ─────────────────────────────────────────

CANDIDATES_PER_ITEM = 3


async def _send_picker(update: Update, context: ContextTypes.DEFAULT_TYPE, item_name: str, qty) -> None:
    """
    Search for `item_name`, fetch candidate SKUs with images, and send a
    photo album + a "which one?" button prompt. Never auto-picks a SKU —
    a vague name (e.g. "bread") can silently resolve to the wrong product,
    so the user always confirms the exact SKU themselves.
    """
    try:
        candidates = find_candidates(item_name, limit=CANDIDATES_PER_ITEM)
    except ZeptoRateLimited:
        await update.message.reply_text(
            f"❌ {item_name} — Zepto is busy right now, try again in a minute"
        )
        return
    except Exception as e:
        logging.error(f"[Picker] Search failed for '{item_name}': {e}")
        await update.message.reply_text(f"❌ {item_name} — search failed, try again")
        return

    if not candidates:
        await update.message.reply_text(f"❌ {item_name} — not found on Zepto")
        return

    # Fetch details sequentially (not gathered) — Zepto's search API is already
    # prone to 429s under load; firing 3 detail requests at once per item would
    # make that worse.
    token = get_cached_token()
    details = []
    for c in candidates:
        d = await get_product_details_async(c["sku_id"], token)
        if d and d.get("image_url"):
            details.append(d)

    if not details:
        await update.message.reply_text(f"❌ {item_name} — couldn't load product images, try again")
        return

    media = [
        InputMediaPhoto(
            media=d["image_url"],
            caption=f"{i + 1}. {d['name'] or item_name}\n"
                    f"₹{d['price']}" + ("" if d["in_stock"] else " (out of stock)")
        )
        for i, d in enumerate(details)
    ]
    await update.message.reply_media_group(media=media)

    req_id = uuid.uuid4().hex[:8]
    context.user_data.setdefault("pending_picks", {})[req_id] = {
        "item_name": item_name,
        "qty":       qty,
        "candidates": details,
    }

    buttons = [
        InlineKeyboardButton(str(i + 1), callback_data=f"pick:{req_id}:{i}")
        for i in range(len(details))
    ]
    buttons.append(InlineKeyboardButton("Skip", callback_data=f"pick:{req_id}:skip"))

    await update.message.reply_text(
        f"Which *{item_name}* did you mean?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([buttons])
    )


def _parse_cart_removal(text: str) -> dict | None:
    """
    Detect "remove X from cart" / "clear my cart" style messages so they're
    routed to actual Zepto cart removal instead of being misparsed as a
    shopping-list item to search for and add (the original bug report).
    Returns {"remove_all": bool, "items": [str]} or None if not a removal intent.
    """
    t = text.strip()

    if re.search(r"(?i)\b(clear|empty)\b.*\bcart\b", t) or re.search(r"(?i)\bremove\b.*\ball\b.*\bcart\b", t):
        return {"remove_all": True, "items": []}

    m = re.match(r"(?i)^(?:remove|delete)\s+(.+?)\s+from\s+(?:my\s+)?cart\s*$", t)
    if m:
        items = [i.strip() for i in re.split(r",|\band\b", m.group(1)) if i.strip()]
        return {"remove_all": False, "items": items}

    return None


async def _handle_cart_removal(update: Update, removal: dict) -> None:
    if removal["remove_all"]:
        await update.message.reply_text("Removing all items from your Zepto cart...")
    else:
        await update.message.reply_text(f"Removing {', '.join(removal['items'])} from your Zepto cart...")

    try:
        result = remove_from_cart(item_names=removal["items"] or None, remove_all=removal["remove_all"])
    except Exception as e:
        logging.error(f"[Bot] Cart removal failed: {e}")
        await update.message.reply_text(f"Couldn't update the cart. Error: {str(e)}")
        return

    if result.get("rate_limited"):
        await update.message.reply_text("Zepto is busy right now, try again in a minute.")
        return

    lines = []
    if result["removed"]:
        lines.append("*Removed from cart:*")
        lines += [f"  ✅ {n}" for n in result["removed"]]
    if result["not_found"]:
        lines.append("\n*Not found in cart:*")
        lines += [f"  ❌ {n}" for n in result["not_found"]]
    if not lines:
        lines = ["Nothing was removed — your cart may already be empty."]

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_text = update.message.text.strip()

    removal = _parse_cart_removal(user_text)
    if removal is not None:
        await _handle_cart_removal(update, removal)
        return

    # Parse shopping list locally — no API call needed
    items = parse_shopping_list(user_text)
    if not items:
        await update.message.reply_text(
            "Couldn't parse that as a shopping list.\n"
            "Try: _Add Eggoz 30 egg tray, Amul milk 1L x2_",
            parse_mode="Markdown"
        )
        return

    await update.message.reply_text("Got it, looking these up on Zepto...")

    for item in items:
        await _send_picker(update, context, item["name"], item["qty"])


# ── Inline button callbacks — Yes / Skip ─────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
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
        add_sku(name, pending["qty"], selected["sku_id"])
        await query.message.reply_text(
            f"✅ Added *{name}* x{pending['qty']} — ₹{selected['price']} to your watchlist.\n\n"
            "I'll check prices every 2 hours and alert you when the cart drops by ₹50+.",
            parse_mode="Markdown"
        )
        return

    if query.data == "skip":
        data = load_watchlist()
        cart = compute_cart_total(data)
        data["previous_cart_total"] = cart["total"]
        data["previous_check_at"]   = datetime.now(timezone.utc).isoformat()
        save_watchlist(data)
        await query.message.reply_text(
            "Skipped. Baseline updated to current prices.\n"
            "I'll alert you again when the cart drops another ₹50."
        )
        return

    if query.data == "yes":
        await query.message.reply_text("Adding items to your Zepto cart...")

        data = load_watchlist()
        skus_to_add = [
            s for s in data["skus"]
            if (latest := get_latest_price(s)) and latest["in_stock"]
        ]

        if not skus_to_add:
            await query.message.reply_text("No in-stock items to add to cart.")
            return

        try:
            logging.info(f"[Bot] Adding {len(skus_to_add)} items to cart via Anthropic API")
            cart_result = add_to_cart(skus_to_add)
            logging.info(f"[Bot] ✅ Cart response: {cart_result}")
            cart = compute_cart_total(data)

            item_lines = [
                f"  {item['name']} x{item['qty']}   ₹{round(item['price'] * item['qty'])}"
                for item in cart["items"]
                if item["in_stock"] and item["price"]
            ]

            confirmation = (
                "🛒 *Items added to your Zepto cart!*\n\n"
                + "\n".join(item_lines)
                + f"\n\n{'─' * 30}\n"
                + f"  Cart total:   ₹{round(cart['total'])}\n"
                + f"{'─' * 30}\n\n"
                + "Open the Zepto app to review and place your order."
            )

            await query.message.reply_text(confirmation, parse_mode="Markdown")

        except Exception as e:
            logging.error(f"[Bot] ❌ Add to cart failed: {e}")
            await query.message.reply_text(
                "Couldn't add items to cart. Please check the Zepto app directly.\n"
                f"Error: {str(e)}"
            )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    pull_state()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("list",   list_items))
    app.add_handler(CommandHandler("remove", remove_item))
    app.add_handler(CommandHandler("status", status))
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
