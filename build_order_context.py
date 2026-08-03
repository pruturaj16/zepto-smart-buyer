"""
build_order_context.py — one-time script to build a compact "past orders"
context blob for the planning system prompt in zepto_agent.py.

Paginates list_order_history (direct MCP call, no LLM) until a page comes
back with zero orders, with a 5-second delay between consecutive calls to
stay well clear of Zepto's rate limits. Extracts unique product names
(from each order's productsNamesAndCounts) so the planner can match a
vague user query ("add milk") to the user's actual past-ordered product
name ("Amul Gold Milk 500ml") for a stronger search_products query.

Run manually (not part of the periodic price-check cycle):
    python build_order_context.py

Writes order_context.txt locally and pushes it to GCS so both VMs see it.
"""
import asyncio
import json
import logging
import os
import time

from config import WATCHLIST_PATH
from zepto_auth import get_valid_token as get_cached_token
from direct_zepto import call_tool_with_retry, _extract_text
from gcs_sync import push_blob

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CONTEXT_PATH = os.path.join(os.path.dirname(os.path.abspath(WATCHLIST_PATH)), "order_context.txt")
_BLOB_NAME   = "order_context.txt"

PAGE_SIZE      = 20
DELAY_SECONDS  = 5


def _parse_json_result(result):
    structured = getattr(result, "structuredContent", None)
    if structured:
        return structured
    return json.loads(_extract_text(result))


async def _fetch_all_orders_async() -> list:
    token = get_cached_token()
    page = 1
    all_orders = []

    while True:
        logger.info(f"[OrderContext] Fetching page {page}...")
        result = await call_tool_with_retry(
            "list_order_history", {"limit": PAGE_SIZE, "pageNumber": page}, token
        )
        if result is None:
            logger.error(f"[OrderContext] Page {page} failed after retries — stopping here.")
            break

        orders = _parse_json_result(result).get("orders", [])
        if not orders:
            logger.info(f"[OrderContext] Page {page} is empty — reached the end.")
            break

        all_orders.extend(orders)
        logger.info(f"[OrderContext] Page {page}: {len(orders)} orders (running total: {len(all_orders)})")
        page += 1

        time.sleep(DELAY_SECONDS)

    return all_orders


def build_context() -> str:
    orders = asyncio.run(_fetch_all_orders_async())

    names = set()
    for order in orders:
        for product in order.get("productsNamesAndCounts", []):
            name = product.get("name")
            if name:
                names.add(name)

    logger.info(f"[OrderContext] {len(orders)} orders scanned, {len(names)} unique product names found")
    return "\n".join(f"- {n}" for n in sorted(names))


def write_context() -> None:
    context = build_context()
    with open(CONTEXT_PATH, "w", encoding="utf-8") as f:
        f.write(context)
    push_blob(CONTEXT_PATH, _BLOB_NAME)
    logger.info(f"[OrderContext] Wrote {CONTEXT_PATH} and pushed to GCS as {_BLOB_NAME}")


if __name__ == "__main__":
    write_context()
