"""
Syncs local state files (watchlist history, orders cache, dashboard metrics)
with a GCS bucket so they survive restarts of the ephemeral price-check VM
and stay consistent with the always-on bot VM.

No-op unless GCS_STATE_BUCKET is set — this dev machine won't have it set,
so local behavior is unchanged; the deployed VMs set it via instance metadata.
"""
import logging
import os

from config import WATCHLIST_PATH

logger = logging.getLogger(__name__)

_BUCKET = os.environ.get("GCS_STATE_BUCKET")


def _bucket():
    from google.cloud import storage
    return storage.Client().bucket(_BUCKET)


def pull_blob(local_path: str, blob_name: str) -> None:
    """Download `blob_name` from GCS, overwriting the local copy."""
    if not _BUCKET:
        return
    try:
        blob = _bucket().blob(blob_name)
        if blob.exists():
            blob.download_to_filename(local_path)
            logger.info(f"[GCS] Pulled {blob_name} from gs://{_BUCKET}")
        else:
            logger.info(f"[GCS] No {blob_name} in gs://{_BUCKET} yet — starting fresh.")
    except Exception as e:
        logger.error(f"[GCS] Pull failed ({blob_name}): {e}")


def push_blob(local_path: str, blob_name: str) -> None:
    """Upload the local file to GCS as `blob_name`."""
    if not _BUCKET:
        return
    try:
        _bucket().blob(blob_name).upload_from_filename(local_path)
        logger.info(f"[GCS] Pushed {blob_name} to gs://{_BUCKET}")
    except Exception as e:
        logger.error(f"[GCS] Push failed ({blob_name}): {e}")


def pull_state() -> None:
    """Download the latest history.json from GCS, overwriting the local copy."""
    pull_blob(WATCHLIST_PATH, "history.json")


def push_state() -> None:
    """Upload the local history.json to GCS."""
    push_blob(WATCHLIST_PATH, "history.json")
