"""
orders_sync.py — caches Zepto order history directly via MCP (no LLM).

Past orders don't change, so this only fetches full details for order IDs
not already cached — cheap even with a long order history. Feeds the
dashboard's order count / total spent / total savings / discount % metrics
from Zepto's own ground-truth data (billSummary.totalSaved etc.), rather
than trying to reconstruct them from our own price-check history.
"""
import asyncio
import json
import logging
import os

from config import WATCHLIST_PATH
from zepto_auth import get_valid_token as get_cached_token
from direct_zepto import _call_tool_async, _extract_text
from gcs_sync import pull_blob, push_blob

logger = logging.getLogger(__name__)

ORDERS_PATH = os.path.join(os.path.dirname(os.path.abspath(WATCHLIST_PATH)), "orders.json")
_BLOB_NAME  = "orders.json"


def _parse_json_result(result):
    """Prefer structuredContent (reliable JSON) over text-block parsing."""
    structured = getattr(result, "structuredContent", None)
    if structured:
        return structured
    return json.loads(_extract_text(result))


def _load_cache() -> dict:
    pull_blob(ORDERS_PATH, _BLOB_NAME)
    if not os.path.exists(ORDERS_PATH):
        return {}
    with open(ORDERS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(ORDERS_PATH), exist_ok=True)
    tmp_path = ORDERS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, ORDERS_PATH)
    push_blob(ORDERS_PATH, _BLOB_NAME)


async def _call_with_retry(tool_name: str, arguments: dict, token: str, max_retries: int = 3):
    delay = 3
    for attempt in range(max_retries + 1):
        result = await _call_tool_async(tool_name, arguments, token)
        raw = _extract_text(result)
        if "Too Many Requests" in raw and not getattr(result, "structuredContent", None):
            if attempt < max_retries:
                logger.warning(f"[Orders] Rate-limited on {tool_name} (attempt {attempt + 1}/{max_retries}), retrying in {delay}s...")
                await asyncio.sleep(delay)
                delay *= 2
                continue
            logger.error(f"[Orders] Still rate-limited on {tool_name} after all retries")
            return None
        return result
    return None


async def _sync_async(max_new_details: int = 10) -> dict:
    token = get_cached_token()
    cache = _load_cache()

    list_result = await _call_with_retry("list_order_history", {"limit": 50, "pageNumber": 1}, token)
    if list_result is None:
        return cache
    summaries = _parse_json_result(list_result).get("orders", [])

    fetched_new = 0
    for summary in summaries:
        order_id = summary["id"]
        if order_id in cache:
            continue
        if fetched_new >= max_new_details:
            break  # backfill the rest on the next run — avoid a burst of API calls

        detail_result = await _call_with_retry("get_order_detail", {"orderId": order_id}, token)
        if detail_result is None:
            continue
        try:
            detail = _parse_json_result(detail_result)
        except (json.JSONDecodeError, TypeError):
            logger.error(f"[Orders] Could not parse order detail for {order_id}")
            continue

        bill = detail.get("billSummary") or {}
        cache[order_id] = {
            "id":                       order_id,
            "code":                     summary.get("code"),
            "status":                   summary.get("formattedStatus"),
            "placed_at":                detail.get("placedTime"),
            "grand_total":              (summary.get("grandTotalAmount") or 0) / 100,
            "grand_total_pre_discount": (detail.get("grandTotalAmountPreDiscount") or 0) / 100,
            "total_saved":              (bill.get("totalSaved") or 0) / 100,
        }
        fetched_new += 1
        logger.info(f"[Orders] Cached order {summary.get('code', order_id)}")

    if fetched_new:
        _save_cache(cache)
        logger.info(f"[Orders] Synced {fetched_new} new order(s), {len(cache)} total cached")

    return cache


def sync_order_history(max_new_details: int = 10) -> list:
    """
    Sync + return the full cached order history as a list, newest first.
    Only backfills up to `max_new_details` previously-unseen orders per
    call to stay rate-limit friendly; remaining backfill (e.g. on first
    run with a long history) continues on subsequent calls.
    """
    cache = asyncio.run(_sync_async(max_new_details))
    return sorted(cache.values(), key=lambda o: o.get("placed_at") or "", reverse=True)
