import requests, os, sys, json, time

TOKEN   = "8656045080:AAE2v57ofYXgI8kmk9X9hpArQXOzySm01Og"
CHAT_ID = "8788121945"
BASE    = f"https://api.telegram.org/bot{TOKEN}"

def send_alert(sku_name, current_price, historical_low, threshold, qty):
    total = current_price * qty
    text = (
        f"🔔 *Lowest price alert!*\n\n"
        f"*{sku_name}*\n"
        f"Today's price: ₹{current_price} ✅ All-time low\n"
        f"Your target: ₹{threshold}\n"
        f"Qty: {qty}\n\n"
        f"━━━━━━━━━━━━━━\n"
        f"*Total: ₹{total}*\n"
        f"━━━━━━━━━━━━━━\n\n"
        f"Should I place this order?"
    )
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Yes, place order", "callback_data": "yes"},
        {"text": "❌ Skip this time",   "callback_data": "skip"}
    ]]}
    r = requests.post(f"{BASE}/sendMessage", json={
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "reply_markup": keyboard
    })
    return r.json()["result"]["message_id"]

def wait_for_response(timeout=3600):
    offset, deadline = None, time.time() + timeout
    while time.time() < deadline:
        params = {"timeout": 30, "allowed_updates": ["callback_query"]}
        if offset:
            params["offset"] = offset
        updates = requests.get(f"{BASE}/getUpdates", params=params).json()
        for u in updates.get("result", []):
            offset = u["update_id"] + 1
            cb = u.get("callback_query")
            if cb:
                requests.post(f"{BASE}/answerCallbackQuery",
                    json={"callback_query_id": cb["id"]})
                return cb["data"]
    return "timeout"

if __name__ == "__main__":
    args          = json.loads(sys.argv[1])
    send_alert(
        args["sku_name"], args["current_price"],
        args["historical_low"], args["threshold"], args["qty"]
    )
    response = wait_for_response()
    print(response)