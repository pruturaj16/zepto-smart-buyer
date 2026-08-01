"""
Zepto MCP token management.

Mints/refreshes a Zepto MCP access token from a stored refresh_token —
no browser/OAuth-consent step needed at run time.

Two backing stores, tried in this order:
  1. GCP Secret Manager (used when GCP_PROJECT is set — i.e. on the deployed VMs)
  2. Claude Code's local credential store (~/.claude/.credentials.json) — used
     for local development on this machine, where the Zepto MCP connector was
     originally authorized.

Either store holds {access_token, refresh_token, client_id, expires_at}.
When the access token is near expiry, this calls Zepto's token endpoint
directly (grant_type=refresh_token) and persists the new token back to
whichever store it came from. Zepto's OAuth server may rotate the refresh
token on use, so the new refresh_token (if returned) is saved too.
"""
import json
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

TOKEN_ENDPOINT = "https://auth.zepto.co.in/token"

_LOCAL_CREDS_PATH = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")

_GCP_PROJECT     = os.environ.get("GCP_PROJECT")
_GCP_SECRET_NAME = os.environ.get("ZEPTO_TOKEN_SECRET", "zepto-mcp-token")

_EXPIRY_SKEW_SECONDS = 300  # refresh this many seconds before actual expiry


def _normalize_expires_at_ms(expires_at) -> int:
    """expires_at may be stored in seconds or milliseconds — normalize to ms."""
    expires_at = float(expires_at)
    return int(expires_at * 1000) if expires_at < 10**12 else int(expires_at)


# ── Local store (Claude Code's credentials.json) ─────────────────────────────

def _load_local_state() -> dict | None:
    if not os.path.exists(_LOCAL_CREDS_PATH):
        return None
    with open(_LOCAL_CREDS_PATH, "r", encoding="utf-8") as f:
        creds = json.load(f)
    for key, entry in creds.get("mcpOAuth", {}).items():
        if entry.get("serverName") == "zepto":
            return {
                "access_token":  entry["accessToken"],
                "refresh_token": entry["refreshToken"],
                "client_id":     entry["clientId"],
                "expires_at":    _normalize_expires_at_ms(entry["expiresAt"]),
                "_source":       "local",
                "_key":          key,
            }
    return None


def _save_local_state(state: dict) -> None:
    with open(_LOCAL_CREDS_PATH, "r", encoding="utf-8") as f:
        creds = json.load(f)
    entry = creds["mcpOAuth"][state["_key"]]
    entry["accessToken"]  = state["access_token"]
    entry["refreshToken"] = state["refresh_token"]
    entry["expiresAt"]    = state["expires_at"]
    tmp = _LOCAL_CREDS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(creds, f, indent=2)
    os.replace(tmp, _LOCAL_CREDS_PATH)


# ── GCP Secret Manager store ──────────────────────────────────────────────────

def _load_secret_manager_state() -> dict | None:
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{_GCP_PROJECT}/secrets/{_GCP_SECRET_NAME}/versions/latest"
    try:
        resp = client.access_secret_version(name=name)
    except Exception as e:
        logger.warning(f"[ZeptoAuth] Could not read secret {_GCP_SECRET_NAME}: {e}")
        return None
    payload = json.loads(resp.payload.data.decode("utf-8"))
    payload["expires_at"] = _normalize_expires_at_ms(payload["expires_at"])
    payload["_source"] = "gcp"
    return payload


def _save_secret_manager_state(state: dict) -> None:
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    parent = f"projects/{_GCP_PROJECT}/secrets/{_GCP_SECRET_NAME}"
    payload = json.dumps({
        "access_token":  state["access_token"],
        "refresh_token": state["refresh_token"],
        "client_id":     state["client_id"],
        "expires_at":    state["expires_at"],
    }).encode("utf-8")
    client.add_secret_version(parent=parent, payload={"data": payload})


# ── Dispatch ───────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if _GCP_PROJECT:
        state = _load_secret_manager_state()
        if state:
            return state
    state = _load_local_state()
    if state:
        return state
    raise RuntimeError(
        "No Zepto refresh token found in GCP Secret Manager or "
        f"{_LOCAL_CREDS_PATH}. Re-authorize the Zepto MCP connector "
        "(claude mcp / open Claude Desktop) to seed one."
    )


def _save_state(state: dict) -> None:
    if state["_source"] == "gcp":
        _save_secret_manager_state(state)
    else:
        _save_local_state(state)


def _refresh(state: dict) -> dict:
    logger.info("[ZeptoAuth] Access token near expiry — refreshing...")
    resp = requests.post(
        TOKEN_ENDPOINT,
        data={
            "grant_type":    "refresh_token",
            "refresh_token": state["refresh_token"],
            "client_id":     state["client_id"],
        },
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()

    state["access_token"]  = body["access_token"]
    state["refresh_token"] = body.get("refresh_token", state["refresh_token"])
    state["expires_at"]    = int((time.time() + body.get("expires_in", 3600)) * 1000)
    logger.info("[ZeptoAuth] Refreshed successfully.")
    return state


def get_valid_token() -> str:
    """Returns a valid Zepto MCP access token, refreshing it first if needed."""
    state  = _load_state()
    now_ms = time.time() * 1000

    if state["expires_at"] - now_ms < _EXPIRY_SKEW_SECONDS * 1000:
        state = _refresh(state)
        _save_state(state)

    return state["access_token"]
