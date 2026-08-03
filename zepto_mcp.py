"""
Zepto MCP client.
Gets a live OAuth access token via zepto_auth (auto-refreshed from a stored
refresh_token) and calls Zepto MCP via the Anthropic API mcp_servers parameter.
"""
import re
import json
import logging
import time
import anthropic
from config import ANTHROPIC_API_KEY, ZEPTO_STORE_ID, ZEPTO_LATITUDE, ZEPTO_LONGITUDE, ZEPTO_MCP_DEBUG
from zepto_auth import get_valid_token as get_cached_token

logger = logging.getLogger(__name__)

ZEPTO_MCP_URL = "https://mcp.zepto.co.in/mcp"


class ZeptoRateLimited(Exception):
    """Raised when Zepto's API keeps returning 429 after all retries."""
    pass

# Zepto scopes shopping context (store, cart) to the MCP session — every tool
# call other than select_store fails with "Store not selected" otherwise.
_SELECT_STORE_PREAMBLE = (
    f"Before doing anything else, call select_store with storeId=\"{ZEPTO_STORE_ID}\", "
    f"latitude={ZEPTO_LATITUDE}, longitude={ZEPTO_LONGITUDE}. This is required as the very "
    "first tool call, even though the user didn't ask for it — every other Zepto tool call "
    "fails until the store is selected. Then proceed with the task below.\n\n"
)


def call_zepto_mcp(prompt: str, system: str, max_retries: int = 3) -> str:
    """
    Call Zepto MCP via Anthropic API using the cached OAuth token.
    Returns the full text content of the final response.

    Zepto's MCP tool calls happen server-side (inside the Anthropic API call),
    so a 429 shows up as an error block embedded in the response, not as a
    raised exception here. Detect it and retry the whole request with backoff;
    raise ZeptoRateLimited if it's still happening after all retries.
    """
    token = get_cached_token()
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    delay = 3
    for attempt in range(max_retries + 1):
        response = client.beta.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            mcp_servers=[{
                "type": "url",
                "url": ZEPTO_MCP_URL,
                "name": "zepto",
                "authorization_token": token
            }],
            system=_SELECT_STORE_PREAMBLE + system,
            messages=[{"role": "user", "content": prompt}],
            betas=["mcp-client-2025-04-04"]
        )

        if ZEPTO_MCP_DEBUG:
            # Full response includes mcp_tool_use / mcp_tool_result content
            # blocks — the actual Zepto responses Claude saw, not just the
            # final text Claude chose to return.
            logger.info(f"[MCP-DEBUG] raw response: {response.model_dump_json()[:4000]}")

        if "Too Many Requests" in response.model_dump_json():
            if attempt < max_retries:
                logger.warning(f"[MCP] Zepto rate-limited (attempt {attempt + 1}/{max_retries}), retrying in {delay}s...")
                time.sleep(delay)
                delay *= 2
                continue
            logger.error("[MCP] Zepto still rate-limited after all retries")
            raise ZeptoRateLimited("Zepto API is rate-limited right now")

        # Collect all text blocks — the JSON is typically in the last one
        text_blocks = [
            block.text for block in response.content
            if hasattr(block, "text") and block.text.strip()
        ]
        return "\n".join(text_blocks).strip()


def parse_json(raw: str) -> dict:
    """
    Extract the first JSON object from a string that may contain
    prose, markdown code fences, or extra explanation around the JSON.
    Works with responses like:
      - plain {"key": "value"}
      - ```json\n{"key": "value"}\n```
      - "Here is the result:\n```json\n{...}\n```"
    """
    raw = raw.strip()

    # Try code fence: ```json { ... } ```
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fence:
        return json.loads(fence.group(1).strip())

    # Try to find the last JSON object in the string (most complete one)
    # Find all {...} blocks and try parsing from the last one backwards
    brace_start = raw.rfind("{")
    if brace_start != -1:
        candidate = raw[brace_start:]
        # Walk forward to find matching closing brace
        depth = 0
        for i, ch in enumerate(candidate):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(candidate[:i+1])
                    except json.JSONDecodeError:
                        break

    # Last resort: parse whole string
    return json.loads(raw)


def search_product(name: str) -> dict:
    """
    Search Zepto for a product by name.
    Returns {sku_id, name, price, in_stock}
    """
    raw = call_zepto_mcp(
        prompt=f"Search for this product: {name}",
        system=(
            "You are a price lookup agent. Search for the given product using search_products. "
            "Pick the best matching result. "
            "Return ONLY a JSON object at the end with exactly these keys: "
            "sku_id (string), name (string), price (number), in_stock (boolean). "
            "The JSON must be inside a ```json code block. No other text after the code block."
        )
    )
    return parse_json(raw)


def search_products_batch(items: list) -> dict:
    """
    Batch search for multiple products in a single API call.
    items: list of dicts with keys {name, qty}
    Returns {
        "success": [
            {name, sku_id, price, in_stock, qty},
            ...
        ],
        "failed": [
            {name, qty, reason},
            ...
        ]
    }
    """
    item_queries = "\n".join(f"- {item['name']}" for item in items)

    try:
        raw = call_zepto_mcp(
            prompt=f"Search for these products:\n{item_queries}",
            system=(
                "You are a price lookup agent. Search for each given product using search_multiple_products. "
                "For each product, pick the best matching result if found. "
                "Return ONLY a JSON object with exactly these keys:\n"
                "- success: array of {name (string), sku_id (string), price (number), in_stock (boolean)} "
                "for found items. Do NOT include a qty field — it will be set by the caller.\n"
                "- failed: array of {name (string), reason (string)} for items not found.\n"
                "The JSON must be inside a ```json code block. No other text after the code block."
            )
        )
    except ZeptoRateLimited:
        return {
            "success": [],
            "failed": [{"name": item["name"], "qty": item["qty"], "reason": "rate_limited"} for item in items]
        }

    result = parse_json(raw)

    # Ensure defaults
    if "success" not in result:
        result["success"] = []
    if "failed" not in result:
        result["failed"] = []

    # Merge qty from original items into success results
    item_map = {item["name"]: item["qty"] for item in items}
    for success in result["success"]:
        if "qty" not in success and success["name"] in item_map:
            success["qty"] = item_map[success["name"]]

    return result


def add_to_cart(skus: list) -> dict:
    """
    Add all SKUs to cart only — does NOT place the order.
    User places the order manually from the Zepto app.
    skus: list of dicts with keys id, name, qty
    Returns {cart_total, item_count, items}
    """
    import logging
    logger = logging.getLogger(__name__)

    sku_lines = "\n".join(
        f"- {s['name']}, qty={s['qty']}, sku_id={s['id']}"
        for s in skus
    )

    logger.info(f"[Cart] Adding {len(skus)} items to cart (no order placement)")
    logger.info(f"[Cart] Items:\n{sku_lines}")

    raw = call_zepto_mcp(
        prompt=f"Add these items to the cart only. Do NOT place the order:\n{sku_lines}",
        system=(
            "You are a cart management agent. For each item call update_cart to add it. "
            "Do NOT call place_order or create_order under any circumstances. "
            "After adding all items, call view_cart to get the final cart state. "
            "Return ONLY a JSON object inside a ```json code block with keys: "
            "cart_total (number), item_count (number), items (array of {name, qty, price}). "
            "No other text after the code block."
        )
    )

    logger.info(f"[Cart] Raw MCP response: {raw[:500]}")

    try:
        result = parse_json(raw)
        logger.info(f"[Cart] ✅ Cart updated: {result}")
        return result
    except Exception as e:
        logger.error(f"[Cart] ❌ Failed to parse cart response: {e}")
        logger.error(f"[Cart] Full response: {raw}")
        raise


def add_to_cart_and_order(skus: list) -> dict:
    """
    Add all SKUs to cart and place the order.
    skus: list of dicts with keys id, name, qty
    Returns {order_id, total, eta}
    """
    import logging
    logger = logging.getLogger(__name__)

    sku_lines = "\n".join(
        f"- {s['name']}, qty={s['qty']}, sku_id={s['id']}"
        for s in skus
    )

    logger.info(f"[Order] Attempting to place order for {len(skus)} items")
    logger.info(f"[Order] Items:\n{sku_lines}")

    raw = call_zepto_mcp(
        prompt=f"Add these items to cart and place the order:\n{sku_lines}",
        system=(
            "You are an order placement agent. For each item call add_to_cart, "
            "then call place_order once. "
            "Return ONLY a JSON object inside a ```json code block with keys: "
            "order_id (string), total (number), eta (string). "
            "No other text after the code block."
        )
    )

    logger.info(f"[Order] Raw MCP response: {raw[:500]}")

    try:
        result = parse_json(raw)
        logger.info(f"[Order] ✅ Parsed result: {result}")
        return result
    except Exception as e:
        logger.error(f"[Order] ❌ Failed to parse order response: {e}")
        logger.error(f"[Order] Full response: {raw}")
        raise
