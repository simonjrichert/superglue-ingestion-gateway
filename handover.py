"""Persist failed handovers (outbox) and skip already-sent external ids (replay)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

OUTBOX_DIR = os.getenv("OUTBOX_DIR", "outbox")
FORWARDED_FILE = os.getenv("FORWARDED_FILE", os.path.join("state", "forwarded.json"))


def _ensure_dirs():
    Path(OUTBOX_DIR).mkdir(parents=True, exist_ok=True)
    Path(FORWARDED_FILE).parent.mkdir(parents=True, exist_ok=True)


def payload_inputs(payload: dict) -> dict:
    """Tool-run body uses inputs; older outbox files used data."""
    if "inputs" in payload:
        return payload.get("inputs") or {}
    return payload.get("data") or {}


def payload_external_ids(payload: dict) -> list[str]:
    return [
        account["AccountExternalId__c"]
        for account in payload_inputs(payload).get("Account", [])
    ]


def load_forwarded_ids() -> set[str]:
    path = Path(FORWARDED_FILE)
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    return set(data.get("external_ids", []))


def save_forwarded_ids(ids: set[str]):
    _ensure_dirs()
    payload = {
        "external_ids": sorted(ids),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    Path(FORWARDED_FILE).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def mark_forwarded(ids: list[str] | set[str]):
    current = load_forwarded_ids()
    current.update(ids)
    save_forwarded_ids(current)


def outbox_files() -> list[Path]:
    folder = Path(OUTBOX_DIR)
    if not folder.is_dir():
        return []
    return sorted(folder.glob("*.json"))


def load_outbox_ids() -> set[str]:
    pending = set()
    for path in outbox_files():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        pending.update(payload_external_ids(payload))
    return pending


def skip_ids() -> set[str]:
    """Already POSTed successfully, or sitting in outbox waiting to POST."""
    return load_forwarded_ids() | load_outbox_ids()


def write_outbox(payload: dict) -> Path:
    _ensure_dirs()
    batch_id = f"batch_{int(datetime.now(timezone.utc).timestamp())}"
    path = Path(OUTBOX_DIR) / f"{batch_id}.json"
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def post_payload(payload: dict, url: str, api_key: str) -> tuple[bool, str]:
    """POST one handover body. Returns (success, log line)."""
    records = payload_inputs(payload)
    accounts = records.get("Account", [])
    contacts = records.get("Contact", [])
    if not accounts:
        return True, "⚠️ No valid Salesforce records to send to Superglue."

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    print(
        f"🚀 Forwarding {len(accounts)} Salesforce Account(s) and "
        f"{len(contacts)} Contact(s) to Superglue Engine ({url})..."
    )
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=5)
    except requests.exceptions.RequestException as err:
        return False, f"⚠️ Could not reach Superglue API Endpoint ({err})."

    if 200 <= response.status_code < 300:
        return True, f"✅ Response [HTTP {response.status_code}]: {response.text}"

    return False, (
        f"⚠️ Engine returned HTTP {response.status_code}: {response.text}. "
        "Treating as failed handover."
    )


def deliver_payload(payload: dict, url: str, api_key: str) -> bool:
    """POST; on success record ids. On failure write outbox and keep ids unsent."""
    ids = payload_external_ids(payload)
    ok, message = post_payload(payload, url, api_key)
    print(message)
    if ok:
        mark_forwarded(ids)
        print(f"📒 Recorded {len(ids)} external id(s) in '{FORWARDED_FILE}' (replay).")
        return True

    path = write_outbox(payload)
    print(
        f"📦 Payload saved to '{path}'. It is not marked forwarded. "
        "Re-run or pass --flush-outbox to POST without remapping."
    )
    print(json.dumps(payload, indent=2, default=str))
    return False


def flush_outbox(url: str, api_key: str) -> int:
    """POST each pending outbox file. Delete and mark forwarded only on success."""
    files = outbox_files()
    if not files:
        print("📭 Outbox is empty.")
        return 0

    delivered = 0
    for path in files:
        print(f"📤 Flushing outbox file '{path}'...")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as err:
            print(f"⚠️ Skipping unreadable outbox file {path}: {err}")
            continue
        ok, message = post_payload(payload, url, api_key)
        print(message)
        if not ok:
            print(f"📦 Left '{path}' in outbox.")
            print(json.dumps(payload, indent=2, default=str))
            continue
        mark_forwarded(payload_external_ids(payload))
        path.unlink(missing_ok=True)
        delivered += 1
        print(f"🗑️ Removed '{path}' after successful POST.")
    return delivered
