import argparse
import os
import json
import re
import time
import requests
from pydantic import ValidationError
from datetime import datetime, timezone

from mapper import load_mapping, map_object
from schemas import OBJECT_MODELS
from sources import fetch_csv_rows, load_sources

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


def send_to_superglue(
    accounts: list[dict],
    contacts: list[dict],
    source_system: str,
):
    if not accounts:
        print("⚠️ No valid Salesforce records to send to Superglue.")
        return

    payload = {
        "source_system": source_system,
        "target_system": "salesforce_crm",
        "batch_id": f"batch_{int(time.time())}",
        "record_count": len(accounts),
        "data": {
            "Account": accounts,
            "Contact": contacts,
        },
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {SUPERGLUE_API_KEY}",
    }

    print(
        f"🚀 Forwarding {len(accounts)} Salesforce Account(s) and "
        f"{len(contacts)} Contact(s) to Superglue Engine ({SUPERGLUE_API_URL})..."
    )
    try:
        res = requests.post(SUPERGLUE_API_URL, json=payload, headers=headers, timeout=5)
        print(f"✅ Response [HTTP {res.status_code}]: {res.text}")
    except requests.exceptions.RequestException as e:
        print(f"⚠️ Could not reach Superglue API Endpoint ({e}).")
        print("💡 Mock Mode Active: Data successfully mapped to Salesforce and payload formatted:")
        print(json.dumps(payload, indent=2, default=str))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Map a legacy extract into Salesforce Account + Contact payloads."
    )
    parser.add_argument(
        "--source",
        default=os.getenv("SOURCE"),
        help="Extractor name from sources.yaml (default: postgres).",
    )
    return parser.parse_args(argv)


# --- Main Pipeline ---
def main(source_name: str | None = None):
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
    raw_rows = fetch_source_rows(selected, spec)

    accounts = []
    contacts = []
    exceptions = []

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

        accounts.append(mapped["Account"])
        contacts.append(mapped["Contact"])

    with open("exceptions_for_consultant.json", "w", encoding="utf-8") as f:
        json.dump(exceptions, f, indent=2, default=str)
    print(f"📋 Isolated {len(exceptions)} exception(s) into 'exceptions_for_consultant.json'")

    send_to_superglue(accounts, contacts, source_system)


if __name__ == "__main__":
    main(parse_args().source)
