"""
Syncs the local watchlist state file (history.json) with a GCS bucket so it
survives restarts of the ephemeral price-check VM and stays consistent with
the always-on bot VM.

No-op unless GCS_STATE_BUCKET is set — this dev machine won't have it set,
so local behavior is unchanged; the deployed VMs set it via instance metadata.
"""
import logging
import os

from config import WATCHLIST_PATH

logger = logging.getLogger(__name__)

_BUCKET    = os.environ.get("GCS_STATE_BUCKET")
_BLOB_NAME = "history.json"


def _bucket():
    from google.cloud import storage
    return storage.Client().bucket(_BUCKET)


def pull_state() -> None:
    """Download the latest history.json from GCS, overwriting the local copy."""
    if not _BUCKET:
        return
    try:
        blob = _bucket().blob(_BLOB_NAME)
        if blob.exists():
            blob.download_to_filename(WATCHLIST_PATH)
            logger.info(f"[GCS] Pulled {_BLOB_NAME} from gs://{_BUCKET}")
        else:
            logger.info(f"[GCS] No {_BLOB_NAME} in gs://{_BUCKET} yet — starting fresh.")
    except Exception as e:
        logger.error(f"[GCS] Pull failed: {e}")


def push_state() -> None:
    """Upload the local history.json to GCS."""
    if not _BUCKET:
        return
    try:
        _bucket().blob(_BLOB_NAME).upload_from_filename(WATCHLIST_PATH)
        logger.info(f"[GCS] Pushed {_BLOB_NAME} to gs://{_BUCKET}")
    except Exception as e:
        logger.error(f"[GCS] Push failed: {e}")
