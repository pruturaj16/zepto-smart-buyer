"""
direct_zepto.py — Zero-cost Zepto MCP client.

Calls Zepto MCP server directly using the Python mcp package
WITHOUT going through the Anthropic API. No LLM involved.
Used by price_check.py for price fetches (free).

Install requirements (run once on Windows):
    pip install mcp httpx

Token: auto-refreshed via zepto_auth.py, same as zepto_mcp.py.
"""

import asyncio
import json
import logging

from config import ZEPTO_LATITUDE, ZEPTO_LONGITUDE, ZEPTO_STORE_ID
from zepto_auth import get_valid_token as get_cached_token

logger = logging.getLogger(__name__)

ZEPTO_MCP_URL = "https://mcp.zepto.co.in/mcp"


# ── Core async MCP caller ─────────────────────────────────────────────────────

async def _call_tool_async(tool_name: str, arguments: dict, token: str):
    """
    Open a single MCP session, call one tool, return the raw result object.
    Uses streamable HTTP transport (mcp >= 1.6).

    Zepto scopes shopping context (store, cart) to the MCP session, so every
    fresh session needs select_store before any product/cart tool will work
    ("Error: Store not selected.").
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {token}"}

    async with streamablehttp_client(ZEPTO_MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if tool_name != "select_store":
                await session.call_tool("select_store", {
                    "storeId": ZEPTO_STORE_ID,
                    "latitude": ZEPTO_LATITUDE,
                    "longitude": ZEPTO_LONGITUDE,
                })
            result = await session.call_tool(tool_name, arguments)

    return result


def _extract_text(result) -> str:
    """
    Extract text content from an MCP tool result.
    Handles TextContent, EmbeddedResource, and plain string returns.
    """
    if result is None:
        return ""

    # If it's already a string
    if isinstance(result, str):
        return result.strip()

    # Standard MCP result with .content list
    if hasattr(result, "content"):
        parts = []
        for block in result.content:
            # TextContent
            if hasattr(block, "text") and block.text:
                parts.append(block.text.strip())
            # EmbeddedResource or other types — serialize as JSON
            elif hasattr(block, "resource") and hasattr(block.resource, "text"):
                parts.append(block.resource.text.strip())
            else:
                # Fallback: dump whatever we got
                try:
                    parts.append(json.dumps(block.__dict__))
                except Exception:
                    parts.append(str(block))
        return "\n".join(p for p in parts if p).strip()

    # Fallback: serialize the whole thing
    try:
        return json.dumps(result.__dict__)
    except Exception:
        return str(result)


def call_tool(tool_name: str, arguments: dict) -> str:
    """
    Synchronous wrapper — use only from scripts with no running event loop
    (e.g. price_check.py, direct_zepto.py __main__ test).
    From bot.py always use the async versions below.
    """
    token  = get_cached_token()
    result = asyncio.run(_call_tool_async(tool_name, arguments, token))
    return _extract_text(result)


# ── Product price fetch (no LLM) ─────────────────────────────────────────────

def _parse_markdown_product(text: str) -> dict:
    """
    Parse price and stock from Zepto's markdown product response.
    Example response format:
        **Product Name**
        **Pricing:**
        - Price: ₹14 (30% off)
        - MRP: ₹20
        **Availability:**
        - In Stock: 12 available
    """
    import re

    # Extract selling price — "Price: ₹14" or "Price: ₹14 (30% off)"
    price_match = re.search(r"Price:\s*₹([\d.]+)", text)
    price = float(price_match.group(1)) if price_match else None

    # Fallback to MRP if no selling price
    if price is None:
        mrp_match = re.search(r"MRP:\s*₹([\d.]+)", text)
        price = float(mrp_match.group(1)) if mrp_match else None

    # Extract stock — "In Stock: 12 available" → in_stock if count > 0
    stock_match = re.search(r"In Stock:\s*(\d+)", text, re.IGNORECASE)
    if stock_match:
        in_stock = int(stock_match.group(1)) > 0
    elif re.search(r"out of stock|unavailable|not available", text, re.IGNORECASE):
        in_stock = False
    else:
        # Default to True if stock info is present but format differs
        in_stock = price is not None

    return {"price": price, "in_stock": in_stock}


def get_product_price(sku_id: str, max_retries: int = 3) -> dict | None:
    """
    Fetch current price and stock status for a known SKU ID directly.
    Returns {sku_id, price, in_stock} or None on failure.
    No LLM involved — 100% free.

    Zepto rate-limits (429 "Too Many Requests") under heavy batch use, e.g.
    price_check.py fetching a whole watchlist every 2 hours. Retry with
    backoff rather than treating a rate limit as a permanent failure.
    """
    delay = 3
    try:
        token = get_cached_token()
        for attempt in range(max_retries + 1):
            result = asyncio.run(_call_tool_async("get_product_details", {"product_variant_id": sku_id}, token))
            raw    = _extract_text(result)

            if "Too Many Requests" in raw:
                if attempt < max_retries:
                    logger.warning(f"[Direct] Rate-limited fetching {sku_id} (attempt {attempt + 1}/{max_retries}), retrying in {delay}s...")
                    import time
                    time.sleep(delay)
                    delay *= 2
                    continue
                logger.error(f"[Direct] Still rate-limited fetching {sku_id} after all retries")
                return None
            break

        if not raw:
            logger.error(f"[Direct] Empty response for {sku_id}")
            return None

        parsed = _parse_markdown_product(raw)

        if parsed["price"] is None:
            logger.error(f"[Direct] Could not extract price for {sku_id}. Raw: {raw[:200]}")
            return None

        return {
            "sku_id":   sku_id,
            "price":    parsed["price"],
            "in_stock": parsed["in_stock"]
        }

    except Exception as e:
        logger.error(f"[Direct] Failed to fetch price for {sku_id}: {e}")
        return None


async def get_product_details_async(sku_id: str, token: str, max_retries: int = 3) -> dict | None:
    """
    Fetch full product details for one SKU directly (no LLM) — used by the
    Telegram picker flow, which needs an image URL per candidate.
    Returns {sku_id, name, price, in_stock, image_url} or None on failure.

    get_product_details returns structured JSON (with an "images" array) when
    available; falls back to markdown text parsing (no image) otherwise.
    """
    delay = 3
    for attempt in range(max_retries + 1):
        result = await _call_tool_async("get_product_details", {"product_variant_id": sku_id}, token)

        structured = getattr(result, "structuredContent", None)
        if structured:
            images = structured.get("images") or []
            return {
                "sku_id":    sku_id,
                "name":      structured.get("name"),
                "price":     structured.get("sellingPrice", 0) / 100 if structured.get("sellingPrice") else None,
                "in_stock":  bool(structured.get("isInStock", True)),
                "image_url": images[0] if images else None,
            }

        raw = _extract_text(result)

        if "Too Many Requests" in raw:
            if attempt < max_retries:
                logger.warning(f"[Direct] Rate-limited fetching details for {sku_id} (attempt {attempt + 1}/{max_retries}), retrying in {delay}s...")
                await asyncio.sleep(delay)
                delay *= 2
                continue
            logger.error(f"[Direct] Still rate-limited fetching details for {sku_id} after all retries")
            return None

        if not raw:
            logger.error(f"[Direct] Empty details response for {sku_id}")
            return None

        parsed = _parse_markdown_product(raw)
        if parsed["price"] is None:
            logger.error(f"[Direct] Could not extract details for {sku_id}. Raw: {raw[:200]}")
            return None

        import re
        image_match = re.search(r"https://cdn\.zeptonow\.com\S+", raw)

        return {
            "sku_id":    sku_id,
            "name":      None,
            "price":     parsed["price"],
            "in_stock":  parsed["in_stock"],
            "image_url": image_match.group(0) if image_match else None,
        }

    return None


def get_prices_batch(skus: list) -> dict:
    """
    Fetch prices for multiple SKUs using direct MCP calls (no LLM).
    skus: list of {id, name, ...} dicts from watchlist
    Returns dict: {sku_id -> {price, in_stock}} or {sku_id -> None}

    Each SKU makes one direct MCP call. No Anthropic API. Free.
    """
    results = {}
    for sku in skus:
        sku_id = sku["id"]
        logger.info(f"[Direct] Fetching price for {sku['name']} ({sku_id})...")
        result = get_product_price(sku_id)
        if result:
            results[sku_id] = {
                "price":    result["price"],
                "in_stock": result["in_stock"]
            }
            logger.info(f"[Direct] {sku['name']}: ₹{result['price']} (in_stock={result['in_stock']})")
        else:
            results[sku_id] = None
            logger.warning(f"[Direct] {sku['name']}: fetch failed")
    return results


# ── Product search via direct MCP + Gemini parsing ───────────────────────────

async def search_item_async(name: str, token: str) -> str:
    """
    Search Zepto for a product by name — async, for use inside event loops (bot.py).
    Returns the raw markdown response from Zepto.
    """
    result = await _call_tool_async("search_products", {"query": name}, token)
    return _extract_text(result)


def search_item(name: str) -> str:
    """Sync wrapper — for use outside event loops (scripts, tests)."""
    return call_tool("search_products", {"query": name})


async def resolve_items_with_gemini(items: list) -> dict:
    """
    Async — call this with `await` from bot.py (Telegram runs an event loop).

    Resolves a shopping list to Zepto SKUs using:
      1. Direct MCP call to search_products — free, no LLM
      2. Gemini Flash to pick best match + extract structured data — free tier

    items: list of {name, qty} dicts
    Returns: {success: [{name, sku_id, price, in_stock, qty}], failed: [{name, qty, reason}]}
    """
    import google.generativeai as genai
    from config import GEMINI_API_KEY

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.0-flash")

    token = get_cached_token()

    success, failed = [], []

    for item in items:
        try:
            # Step 1: Search Zepto directly via async MCP call
            search_results = await search_item_async(item["name"], token)

            if not search_results or "Error" in search_results:
                failed.append({"name": item["name"], "qty": item["qty"], "reason": "no results"})
                continue

            # Step 2: Ask Gemini to pick the best match
            prompt = (
                f"From these Zepto search results, pick the best match for: '{item['name']}'\n\n"
                f"{search_results}\n\n"
                "Return ONLY a JSON object with exactly these fields:\n"
                "  name (string): exact product name from results\n"
                "  sku_id (string): the product_variant_id from results\n"
                "  price (number): selling price as a number, no currency symbol\n"
                "  in_stock (boolean): true if available\n\n"
                "If no good match exists, return: {\"error\": \"not found\"}\n"
                "Return raw JSON only, no markdown fences, no explanation."
            )

            response = model.generate_content(prompt)
            raw_json  = response.text.strip().strip("```json").strip("```").strip()
            data      = json.loads(raw_json)

            if "error" in data:
                failed.append({"name": item["name"], "qty": item["qty"], "reason": data["error"]})
                continue

            success.append({
                "name":     data["name"],
                "sku_id":   data["sku_id"],
                "price":    float(data["price"]),
                "in_stock": bool(data["in_stock"]),
                "qty":      item["qty"]
            })
            logger.info(f"[Gemini] ✅ '{item['name']}' → {data['name']} ₹{data['price']}")

        except Exception as e:
            logger.error(f"[Gemini] ❌ Failed to resolve '{item['name']}': {e}")
            failed.append({"name": item["name"], "qty": item["qty"], "reason": str(e)})

    return {"success": success, "failed": failed}


# ── Cart update (no LLM) ─────────────────────────────────────────────────────

async def add_skus_to_cart(skus: list) -> dict:
    """
    Async — call with `await` from bot.py.
    Add SKUs to Zepto cart directly. No LLM involved. Free.
    skus: list of {id, name, qty} dicts
    Returns {success, added, failed, cart_total}
    """
    token       = get_cached_token()
    total_added = 0
    failed      = []

    for sku in skus:
        try:
            result = await _call_tool_async("update_cart", {
                "sku_id":   sku["id"],
                "quantity": int(sku["qty"])
            }, token)
            logger.info(f"[Direct] ✅ Added to cart: {sku['name']} x{sku['qty']}")
            total_added += 1
        except Exception as e:
            logger.error(f"[Direct] ❌ Failed to add {sku['name']} to cart: {e}")
            failed.append(sku["name"])

    # View cart to get final total
    cart_total = 0
    try:
        cart_result = await _call_tool_async("view_cart", {}, token)
        cart_raw    = _extract_text(cart_result)
        # Extract total from markdown: "Total: ₹473" or "Cart Total: ₹473"
        import re
        match = re.search(r"(?:cart\s*)?total[:\s]+₹?([\d.]+)", cart_raw, re.IGNORECASE)
        if match:
            cart_total = float(match.group(1))
    except Exception:
        pass

    return {
        "success":    total_added > 0,
        "added":      total_added,
        "failed":     failed,
        "cart_total": cart_total
    }


def add_skus_to_cart_sync(skus: list) -> dict:
    """Sync wrapper — for use outside event loops (scripts, tests)."""
    return asyncio.run(add_skus_to_cart(skus))


# ── JSON helper ───────────────────────────────────────────────────────────────

def _parse_json(raw: str) -> dict:
    """Extract first JSON object from a response string."""
    import re
    raw = raw.strip()

    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fence:
        return json.loads(fence.group(1).strip())

    brace_start = raw.rfind("{")
    if brace_start != -1:
        candidate = raw[brace_start:]
        depth = 0
        for i, ch in enumerate(candidate):
            if ch == "{":   depth += 1
            elif ch == "}": depth -= 1
            if depth == 0:
                try:
                    return json.loads(candidate[:i+1])
                except json.JSONDecodeError:
                    break

    return json.loads(raw)


# ── Quick test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from watchlist import load_watchlist

    data = load_watchlist()
    if not data["skus"]:
        print("Watchlist is empty — add some items first.")
    else:
        # Test with first SKU in watchlist
        test_sku = data["skus"][0]
        print(f"\nTesting direct price fetch for: {test_sku['name']}")
        result = get_product_price(test_sku["id"])
        if result:
            print(f"✅ Price: ₹{result['price']}  In stock: {result['in_stock']}")
        else:
            print("❌ Failed to fetch price")
