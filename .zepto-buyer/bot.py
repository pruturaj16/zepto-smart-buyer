import json
import logging
from datetime import datetime, timezone

from telegram import Update
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
from zepto_mcp import search_products_batch, add_to_cart, add_to_cart_and_order

import os
_log_handler_file    = logging.FileHandler(LOG_PATH, encoding="utf-8")
_log_handler_console = logging.StreamHandler()
_log_fmt = logging.Formatter("%(asctime)s — %(levelname)s — %(message)s")
_log_handler_file.setFormatter(_log_fmt)
_log_handler_console.setFormatter(_log_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_log_handler_file, _log_handler_console])


# ── /start ────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Welcome to *Zepto Smart Buyer!*\n\n"
        "I monitor your grocery list and alert you when the total cart price "
        "drops by ₹50 or more compared to 2 hours ago.\n\n"
        "*How to use:*\n"
        "• Just type your shopping list naturally:\n"
        "  _Add Eggoz 30 egg tray, Amul milk 1L x2, Aashirvaad atta 5kg_\n\n"
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

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_text = update.message.text.strip()
    await update.message.reply_text("Got it, looking these up on Zepto...")

    # Parse shopping list locally — no API call needed
    items = parse_shopping_list(user_text)
    if not items:
        await update.message.reply_text(
            "Couldn't parse that as a shopping list.\n"
            "Try: _Add Eggoz 30 egg tray, Amul milk 1L x2_",
            parse_mode="Markdown"
        )
        return

    # Step 2 — batch resolve all items on Zepto in one Anthropic API call
    try:
        result = search_products_batch(items)
    except Exception as e:
        logging.error(f"Batch search failed: {e}")
        await update.message.reply_text(
            f"Error searching products: {str(e)}\nPlease try again."
        )
        return

    # Step 3 — add resolved items to watchlist and prepare response
    # Build a lookup so we use the user's original qty, not Zepto's unit description
    user_qty_map = {i["name"].lower(): i["qty"] for i in items}
    added, failed = [], []

    for item in result.get("success", []):
        try:
            # Prefer the qty the user typed; fall back to whatever the API returned
            qty = user_qty_map.get(item["name"].lower(), item.get("qty", 1))
            add_sku(item["name"], qty, item["sku_id"])
            added.append(f"{item['name']} x{qty} — ₹{item['price']}")
        except Exception as e:
            logging.error(f"Failed to add {item['name']} to watchlist: {e}")
            failed.append(f"{item['name']} (add failed)")

    for item in result.get("failed", []):
        failed.append(f"{item['name']}")

    # Step 4 — confirm to user
    lines = []
    if added:
        lines.append("*Added to your watchlist:*")
        lines += [f"  ✅ {a}" for a in added]
    if failed:
        lines.append("\n*Could not find on Zepto:*")
        lines += [f"  ❌ {f}" for f in failed]
    lines.append(
        "\nI'll check prices every 2 hours and alert you when the cart drops by ₹50+."
    )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── Inline button callbacks — Yes / Skip ─────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)

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
    main()
