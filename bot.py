import logging
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
from watchlist import (
    load_watchlist,
    add_sku, remove_sku, compute_cart_total, get_latest_price,
)
import zepto_agent
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


# ── Free text — routed through zepto_agent's plan/execute/analyze pipeline ──

async def _send_picker(update: Update, context: ContextTypes.DEFAULT_TYPE, item_name: str, qty, candidates: list) -> None:
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
        "item_name": item_name,
        "qty":       qty,
        "candidates": candidates,
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
    """Removes items from the live Zepto cart via a single batched update_cart(quantity=0) call."""
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

    lines = ["*Removed from cart:*"] + [f"  ✅ {item['name']}" for item in cart_items_to_remove]
    if cart_not_found:
        lines.append("\n*Not found in cart:*")
        lines += [f"  ❌ {n}" for n in cart_not_found]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_text = update.message.text.strip()
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


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    pull_state()
    pull_blob(zepto_agent.CONTEXT_PATH, "order_context.txt")
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
