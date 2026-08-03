"""
zepto_agent.py — explicit, controlled Zepto shopping pipeline.

Replaces the old approach (Anthropic's `mcp_servers` beta parameter, where
Claude autonomously calls Zepto's MCP server-side inside one opaque API
call) with a pipeline we control end to end:

  1-3. plan()    — ONE Anthropic call (no MCP tools attached) reads the
                    user's message + their past-order context and decides
                    intent, which Zepto MCP tool(s) to call, and with what
                    arguments (e.g. 3 items mentioned -> search_multiple_products).
  4.   execute()  — WE call those Zepto MCP tools directly (direct_zepto's
                    call_tool_with_retry), with real 429 retry/backoff.
  5-6. analyze()  — a second Anthropic call reads the raw tool results and
                    either says "ready" (with candidates / cart items to
                    act on) or asks for a small number of additional tool
                    calls (bounded — see MAX_EXTRA_ROUNDS).

No Zepto MCP tool call happens without our own code deciding to make it —
Claude only ever reasons over data we already fetched.
"""
import json
import logging
import os

import anthropic

from config import ANTHROPIC_API_KEY, WATCHLIST_PATH
from zepto_auth import get_valid_token as get_cached_token
from direct_zepto import call_tool_with_retry, _extract_text

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"
MAX_EXTRA_ROUNDS = 1  # bounds worst-case Zepto tool calls per user message

# update_cart requires a deviceId ("fallback cart key when a transportSessionId
# is not available") — we're a server-side bot, not a browser session, so we
# just use one fixed, stable value across all calls.
DEVICE_ID = "zepto-buyer-bot"

CONTEXT_PATH = os.path.join(os.path.dirname(os.path.abspath(WATCHLIST_PATH)), "order_context.txt")

_TOOL_CATALOG = """\
Available Zepto MCP tools you may request:
- search_products: {"query": string} — search for ONE concrete product. Use when
  exactly one item is mentioned.
- search_multiple_products: {"queries": [string, ...]} — search for SEVERAL
  different products in one call. Use whenever 2+ distinct items are mentioned
  (e.g. "add milk and bread" -> queries=["milk","bread"]). Prefer this over
  multiple separate search_products calls.
- view_cart: {} — view current cart contents. Use this (and only this) for
  remove_items / clear_cart intents, to find items to remove.
"""


def _load_order_context() -> str:
    if not os.path.exists(CONTEXT_PATH):
        return ""
    with open(CONTEXT_PATH, "r", encoding="utf-8") as f:
        return f.read().strip()


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def _parse_json(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


# ── Steps 1-3: understand intent, pick tool(s), build parameters ────────────

def plan(user_text: str) -> dict:
    """
    Returns {"intent": ..., "items": [{"name","qty"}], "tool_calls": [{"tool","arguments"}]}
    """
    order_context = _load_order_context()
    context_block = (
        f"The user has ordered these products before — if their message closely "
        f"matches one, use the EXACT name below as the search query for a "
        f"stronger match:\n{order_context}\n\n"
        if order_context else ""
    )

    system = (
        "You are the planning module for a Zepto grocery shopping Telegram bot. "
        "Given the user's message, decide their intent and exactly which Zepto "
        "MCP tool call(s) are needed right now to fulfill it.\n\n"
        f"{_TOOL_CATALOG}\n"
        f"{context_block}"
        "Respond with ONLY a JSON object, no other text:\n"
        "{\n"
        '  "intent": "add_items" | "remove_items" | "clear_cart" | "unknown",\n'
        '  "items": [{"name": string, "qty": integer}],  // requested items; empty for clear_cart/unknown\n'
        '  "tool_calls": [{"tool": string, "arguments": object}]  // the Zepto tool call(s) to make now\n'
        "}\n"
        "For add_items: choose search_products for a single item, search_multiple_products for 2+.\n"
        "For remove_items or clear_cart: tool_calls must be exactly [{\"tool\": \"view_cart\", \"arguments\": {}}].\n"
        "For unknown: tool_calls must be []."
    )

    response = _client().messages.create(
        model=MODEL,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_text}],
    )
    raw = "".join(b.text for b in response.content if hasattr(b, "text"))
    return _parse_json(raw)


# ── Step 4: execute the planned (or follow-up) tool calls directly ─────────

async def execute_tool_calls(tool_calls: list, token: str) -> list:
    """
    Runs each {"tool","arguments"} directly against Zepto MCP (no LLM
    mediation), with retry/backoff. Returns a list of
    {"tool", "arguments", "result_text"} for the analysis step to read.

    Async, no internal asyncio.run() — this and run() below are always
    called from bot.py, which already has an event loop running
    (Application.run_polling()); calling asyncio.run() from inside that
    loop would raise "asyncio.run() cannot be called from a running event
    loop", so callers must `await` this directly instead.
    """
    results = []
    for call in tool_calls:
        tool, arguments = call["tool"], call.get("arguments", {})
        result = await call_tool_with_retry(tool, arguments, token)
        if result is None:
            results.append({"tool": tool, "arguments": arguments, "result_text": "ERROR: rate-limited after retries"})
            continue
        structured = getattr(result, "structuredContent", None)
        text = json.dumps(structured) if structured else _extract_text(result)
        # Generous cap, not a tight one — search results carry the actual
        # candidate data (price/image/stock per product) the analyze step
        # needs; truncating mid-JSON would silently drop candidates.
        results.append({"tool": tool, "arguments": arguments, "result_text": text[:15000]})
    return results


# ── Steps 5-6: analyze results, loop if needed, produce the final action ───

def analyze(user_text: str, plan_result: dict, tool_results: list) -> dict:
    """
    Returns one of:
      {"status": "need_more_calls", "additional_tool_calls": [...]}
      {"status": "ready", "candidates": {name: [{"sku_id","store_product_id","name",
                            "price","image_url","in_stock"}]}, "not_found": [...]}   (add_items)
      {"status": "ready", "cart_items_to_remove": [{"name","productVariantId","storeProductId"}],
                            "cart_not_found": [...]}                                 (remove_items/clear_cart)
    """
    results_block = "\n\n".join(
        f"Tool: {r['tool']}({json.dumps(r['arguments'])})\nResult:\n{r['result_text']}"
        for r in tool_results
    )

    system = (
        "You are analyzing Zepto MCP tool results to help a shopping Telegram bot "
        "respond to the user. You do not call tools yourself — you only read the "
        "results below and either ask for ONE more targeted tool call, or finalize.\n\n"
        f"User's original message: {user_text}\n"
        f"Detected intent: {plan_result.get('intent')}\n"
        f"Requested items: {json.dumps(plan_result.get('items', []))}\n\n"
        f"Tool results so far:\n{results_block}\n\n"
        "Respond with ONLY a JSON object, no other text.\n\n"
        "If intent is add_items:\n"
        "  search_products/search_multiple_products results already include "
        "productVariantId, storeProductId, name, price (paisa), imageUrl, and "
        "availableQuantity per product — copy these fields directly into your "
        "answer, do not invent or omit any. For each requested item, pick up to 3 "
        "best-matching candidates.\n"
        '  {"status": "ready", "candidates": {"<item name>": [{"sku_id": string, '
        '"store_product_id": string, "name": string, "price": number (rupees, price/100), '
        '"image_url": string, "in_stock": boolean (availableQuantity > 0)}]}, '
        '"not_found": [string]}\n'
        "  If a search result is empty/poor for an item AND you have not already "
        "requested a follow-up this conversation, you may instead respond:\n"
        '  {"status": "need_more_calls", "additional_tool_calls": '
        '[{"tool": "search_products", "arguments": {"query": "<reworded query>"}}]}\n\n'
        "If intent is remove_items or clear_cart:\n"
        "  Read the view_cart result. For remove_items, match requested item names "
        "against cart contents (best match by name). For clear_cart, include every "
        "item currently in the cart.\n"
        '  {"status": "ready", "cart_items_to_remove": '
        '[{"name": string, "productVariantId": string, "storeProductId": string}], '
        '"cart_not_found": [string]}'
    )

    response = _client().messages.create(
        model=MODEL,
        max_tokens=3072,
        system=system,
        messages=[{"role": "user", "content": "Analyze the results above and respond with the JSON object."}],
    )
    raw = "".join(b.text for b in response.content if hasattr(b, "text"))
    return _parse_json(raw)


# ── Orchestrator ─────────────────────────────────────────────────────────

async def run(user_text: str) -> dict:
    """
    Runs the full plan -> execute -> analyze (with a bounded follow-up
    round) pipeline. Returns the final analysis dict, plus "intent" and
    "items" merged in from the plan, so callers don't need to track both.

    plan()/analyze() are synchronous Anthropic calls (no MCP tools attached,
    so no awaiting needed there) — only the Zepto tool execution is async.
    """
    plan_result = plan(user_text)
    intent = plan_result.get("intent", "unknown")

    if intent == "unknown" or not plan_result.get("tool_calls"):
        return {"status": "ready", "intent": intent, "items": plan_result.get("items", [])}

    token = get_cached_token()
    tool_results = await execute_tool_calls(plan_result["tool_calls"], token)

    rounds = 0
    analysis = analyze(user_text, plan_result, tool_results)
    while analysis.get("status") == "need_more_calls" and rounds < MAX_EXTRA_ROUNDS:
        more = await execute_tool_calls(analysis.get("additional_tool_calls", []), token)
        tool_results.extend(more)
        rounds += 1
        analysis = analyze(user_text, plan_result, tool_results)

    analysis["intent"] = intent
    analysis["items"]  = plan_result.get("items", [])
    return analysis
