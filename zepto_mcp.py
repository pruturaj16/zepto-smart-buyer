"""
Zepto MCP client.
Reads the cached OAuth token from mcp-remote auth store and calls
Zepto MCP via the Anthropic API mcp_servers parameter.
Token location: C:/Users/Owner/.mcp-auth/mcp-remote-<version>/<hash>_tokens.json
"""
import os
import re
import json
import glob
import anthropic
from config import ANTHROPIC_API_KEY

ZEPTO_MCP_URL = "https://mcp.zepto.co.in/mcp"
MCP_AUTH_DIR  = os.path.join(os.path.expanduser("~"), ".mcp-auth")


def get_cached_token() -> str | None:
    """
    mcp-remote stores tokens in ~/.mcp-auth/mcp-remote-<version>/<hash>_tokens.json
    Recursively searches all subdirectories for a *_tokens.json with access_token.
    """
    if not os.path.exists(MCP_AUTH_DIR):
        return None

    token_files = glob.glob(os.path.join(MCP_AUTH_DIR, "**", "*_tokens.json"), recursive=True)
    if not token_files:
        token_files = glob.glob(os.path.join(MCP_AUTH_DIR, "**", "*.json"), recursive=True)

    for path in token_files:
        try:
            with open(path, "r") as f:
                data = json.load(f)
            if "access_token" in data:
                return data["access_token"]
        except Exception:
            continue
    return None


def call_zepto_mcp(prompt: str, system: str) -> str:
    """
    Call Zepto MCP via Anthropic API using the cached OAuth token.
    Returns the full text content of the final response.
    """
    token = get_cached_token()
    if not token:
        raise RuntimeError(
            "No Zepto OAuth token found in ~/.mcp-auth/\n"
            "Fix: open Claude Desktop and run one Zepto search to refresh the token."
        )

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    response = client.beta.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        mcp_servers=[{
            "type": "url",
            "url": ZEPTO_MCP_URL,
            "name": "zepto",
            "authorization_token": token
        }],
        system=system,
        messages=[{"role": "user", "content": prompt}],
        betas=["mcp-client-2025-04-04"]
    )

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
