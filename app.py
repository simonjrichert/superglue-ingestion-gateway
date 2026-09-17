import argparse
import os
import json
import re
import time
from pydantic import ValidationError
from datetime import datetime, timezone

from mapper import load_mapping, map_object
from schemas import OBJECT_MODELS
from sources import fetch_csv_rows, load_sources
from handover import (
    FORWARDED_FILE,
    deliver_payload,
    flush_outbox,
    outbox_files,
    skip_ids,
)

# --- Configuration ---
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "legacy_erp")
DB_USER = os.getenv("DB_USER", "admin")
DB_PASSWORD = os.getenv("DB_PASSWORD", "secretpassword")

SUPERGLUE_API_URL = os.getenv("SUPERGLUE_API_URL", "http://localhost:8080/api/v1/ingest")
SUPERGLUE_API_KEY = os.getenv("SUPERGLUE_API_KEY", "sg_live_mock_key_998877")
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
MAPPING_FILE = os.getenv(
    "MAPPING_FILE",
    os.path.join(_APP_DIR, "mapping.yaml"),
)
SOURCES_FILE = os.getenv(
    "SOURCES_FILE",
    os.path.join(_APP_DIR, "sources.yaml"),
)
EXCEPTIONS_FILE = "exceptions_for_consultant.json"
DRY_RUN_PAYLOAD_FILE = "dry_run_payload.json"


# --- Helpers ---
def mask_credit_card(card_str: str) -> str:
    if not card_str:
        return "****"
    # Keep first 4 and last 4 digits
    digits = re.sub(r"\D", "", card_str)
    if len(digits) >= 8:
        return f"{digits[:4]}-****-****-{digits[-4:]}"
    return "****-****-****-****"


_CARD_IN_TEXT = re.compile(r"(?:\d[ -]?){12,19}\d")


def mask_pan_in_text(text: str | None) -> str | None:
    """Redact card-shaped substrings in free text (e.g. notes) before mapping."""
    if not text:
        return text
    return _CARD_IN_TEXT.sub(lambda match: mask_credit_card(match.group(0)), text)


def _json_safe_errors(errors: list[dict]) -> list[dict]:
    """Pydantic ctx may contain Exception instances that json.dump cannot encode."""
    safe = []
    for error in errors:
        item = dict(error)
        ctx = item.get("ctx")
        if isinstance(ctx, dict):
            item["ctx"] = {key: str(value) for key, value in ctx.items()}
        safe.append(item)
    return safe


def _pydantic_errors(object_name: str, err: ValidationError) -> list[dict]:
    prefixed = []
    for error in _json_safe_errors(err.errors(include_url=False)):
        loc = error.get("loc", ())
        error["target_object"] = object_name
        error["loc"] = [object_name, *loc]
        prefixed.append(error)
    return prefixed


def fetch_legacy_data():
    import psycopg2
    from psycopg2.extras import RealDictCursor

    conn = None
    last_error = None
    for attempt in range(5):
        try:
            conn = psycopg2.connect(
                host=DB_HOST,
                port=DB_PORT,
                dbname=DB_NAME,
                user=DB_USER,
                password=DB_PASSWORD,
                connect_timeout=5,
            )
            break
        except psycopg2.OperationalError as err:
            last_error = err
            backoff = 2 ** attempt
            print(f"Database not ready (attempt {attempt + 1}/5): {err}. Retrying in {backoff}s...")
            time.sleep(backoff)

    if not conn:
        raise Exception(f"Could not connect to PostgreSQL database. Last error: {last_error}")

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id, raw_name, email, account_status, credit_card, created_at, notes FROM legacy_customers;"
        )
        rows = cur.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def fetch_source_rows(source_name: str, spec: dict) -> list[dict]:
    source_type = spec["type"]
    if source_type == "postgres":
        return fetch_legacy_data()
    if source_type == "csv":
        return fetch_csv_rows(spec, _APP_DIR)
    raise ValueError(f"Unknown source type '{source_type}' for '{source_name}'.")


def map_row_to_salesforce(row: dict, mapping: dict) -> tuple[dict | None, list[dict]]:
    """Transform one legacy row into Salesforce Account + Contact, or collect errors."""
    errors = []
    mapped = {}

    for object_spec in mapping["objects"]:
        object_name = object_spec["name"]
        payload, mapping_errors = map_object(row, object_spec)
        if mapping_errors:
            errors.extend(mapping_errors)
            continue
        try:
            validated = OBJECT_MODELS[object_name].model_validate(payload)
            mapped[object_name] = validated.model_dump(mode="json")
        except ValidationError as err:
            errors.extend(_pydantic_errors(object_name, err))

    if errors or set(mapped) != {obj["name"] for obj in mapping["objects"]}:
        return None, errors
    return mapped, []


def build_handover_payload(
    accounts: list[dict],
    contacts: list[dict],
    source_system: str,
) -> dict:
    return {
        "source_system": source_system,
        "target_system": "salesforce_crm",
        "batch_id": f"batch_{int(time.time())}",
        "record_count": len(accounts),
        "data": {
            "Account": accounts,
            "Contact": contacts,
        },
    }


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def send_to_superglue(payload: dict):
    accounts = payload["data"]["Account"]
    if not accounts:
        print("⚠️ No valid Salesforce records to send to Superglue.")
        return
    deliver_payload(payload, SUPERGLUE_API_URL, SUPERGLUE_API_KEY)


def write_dry_run_payload(payload: dict, ingested: int, forwarded: int, isolated: int):
    with open(DRY_RUN_PAYLOAD_FILE, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=str)
    print(
        f"🧪 Dry-run: ingested {ingested}, would forward {forwarded} Account(s) and "
        f"{forwarded} Contact(s), isolated {isolated} exception(s). No POST."
    )
    print(
        f"   Inspect '{DRY_RUN_PAYLOAD_FILE}' and '{EXCEPTIONS_FILE}' "
        "before running without --dry-run."
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Map a legacy extract into Salesforce Account + Contact payloads."
    )
    parser.add_argument(
        "--source",
        default=os.getenv("SOURCE"),
        help="Extractor name from sources.yaml (default: postgres).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Map and write exceptions, but do not POST to the engine.",
    )
    parser.add_argument(
        "--flush-outbox",
        action="store_true",
        help="POST pending outbox files only; do not extract or map again.",
    )
    return parser.parse_args(argv)


# --- Main Pipeline ---
def main(
    source_name: str | None = None,
    dry_run: bool = False,
    flush_only: bool = False,
):
    if flush_only:
        print("📤 Flushing outbox (no extract/map).")
        flush_outbox(SUPERGLUE_API_URL, SUPERGLUE_API_KEY)
        return

    mapping = load_mapping(MAPPING_FILE)
    sources_config = load_sources(SOURCES_FILE)
    selected = source_name or sources_config.get("default", "postgres")
    available = sources_config["sources"]
    if selected not in available:
        names = ", ".join(sorted(available))
        raise ValueError(f"Unknown source '{selected}'. Defined sources: {names}.")

    spec = available[selected]
    source_system = spec.get("source_system", selected)
    print(
        f"📥 Ingesting from '{selected}' ({spec['type']}) and mapping to "
        f"{mapping.get('target_system', 'salesforce_crm')}..."
    )
    if dry_run:
        print("🧪 Dry-run enabled: map and isolate exceptions, do not POST.")
    elif outbox_files():
        flush_outbox(SUPERGLUE_API_URL, SUPERGLUE_API_KEY)

    already = skip_ids()
    raw_rows = fetch_source_rows(selected, spec)

    accounts = []
    contacts = []
    exceptions = []
    skipped = 0

    for row in raw_rows:
        # Mask PAN after aliasing so CSV Card Number / Comments use the same keys.
        working_row = dict(row)
        working_row["credit_card"] = mask_credit_card(row.get("credit_card"))
        working_row["notes"] = mask_pan_in_text(row.get("notes"))

        mapped, errors = map_row_to_salesforce(working_row, mapping)
        if mapped is None:
            exceptions.append(
                {
                    "source": selected,
                    "raw_record": working_row,
                    "errors": errors,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "status": "REQUIRES_CONSULTANT_ACTION",
                }
            )
            continue

        external_id = mapped["Account"]["AccountExternalId__c"]
        if external_id in already:
            skipped += 1
            continue

        accounts.append(mapped["Account"])
        contacts.append(mapped["Contact"])

    with open(EXCEPTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(exceptions, f, indent=2, default=str)
    print(f"📋 Isolated {len(exceptions)} exception(s) into '{EXCEPTIONS_FILE}'")
    if skipped:
        print(
            f"⏭️ Replay: skipped {skipped} mapped row(s) already in "
            f"'{FORWARDED_FILE}' or outbox."
        )

    payload = build_handover_payload(accounts, contacts, source_system)
    if dry_run:
        write_dry_run_payload(payload, len(raw_rows), len(accounts), len(exceptions))
        return

    send_to_superglue(payload)


if __name__ == "__main__":
    args = parse_args()
    main(
        source_name=args.source,
        dry_run=args.dry_run or _env_flag("DRY_RUN"),
        flush_only=args.flush_outbox or _env_flag("FLUSH_OUTBOX"),
    )
