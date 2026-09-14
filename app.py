import os
import json
import re
import time
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel, EmailStr, field_validator, ValidationError
from datetime import datetime, timezone

# --- Configuration ---
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "legacy_erp")
DB_USER = os.getenv("DB_USER", "admin")
DB_PASSWORD = os.getenv("DB_PASSWORD", "secretpassword")

SUPERGLUE_API_URL = os.getenv("SUPERGLUE_API_URL", "http://localhost:8080/api/v1/ingest")
SUPERGLUE_API_KEY = os.getenv("SUPERGLUE_API_KEY", "sg_live_mock_key_998877")


# --- Pydantic Schema ---
class SanitizedCustomerRecord(BaseModel):
    customer_id: int
    company_name: str
    email: EmailStr
    account_status: str
    credit_card_masked: str
    created_at: str

    @field_validator("account_status")
    @classmethod
    def validate_status(cls, v):
        allowed = ["ACTIVE", "INACTIVE", "PENDING"]
        upper_val = v.upper()
        if upper_val not in allowed:
            raise ValueError(f"Status '{v}' is not a valid enterprise status {allowed}")
        return upper_val

    @field_validator("created_at")
    @classmethod
    def validate_date(cls, v):
        try:
            datetime.strptime(v, "%Y-%m-%d")
            return v
        except ValueError:
            raise ValueError(f"Date '{v}' does not match required format YYYY-MM-DD")


# --- Helpers ---
def mask_credit_card(card_str: str) -> str:
    if not card_str:
        return "****"
    # Keep first 4 and last 4 digits
    digits = re.sub(r"\D", "", card_str)
    if len(digits) >= 8:
        return f"{digits[:4]}-****-****-{digits[-4:]}"
    return "****-****-****-****"


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


def fetch_legacy_data():
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
            "SELECT id, raw_name, email, account_status, credit_card, created_at FROM legacy_customers;"
        )
        rows = cur.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def send_to_superglue(clean_records: list[dict]):
    if not clean_records:
        print("⚠️ No valid records to send to Superglue.")
        return

    payload = {
        "source_system": "legacy_erp_postgres",
        "target_system": "salesforce_crm",
        "batch_id": f"batch_{int(time.time())}",
        "record_count": len(clean_records),
        "data": clean_records,
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {SUPERGLUE_API_KEY}",
    }

    print(f"🚀 Forwarding {len(clean_records)} clean records to Superglue Engine ({SUPERGLUE_API_URL})...")
    try:
        res = requests.post(SUPERGLUE_API_URL, json=payload, headers=headers, timeout=5)
        print(f"✅ Response [HTTP {res.status_code}]: {res.text}")
    except requests.exceptions.RequestException as e:
        print(f"⚠️ Could not reach Superglue API Endpoint ({e}).")
        print("💡 Mock Mode Active: Data successfully validated and payload formatted:")
        print(json.dumps(payload, indent=2))


# --- Main Pipeline ---
def main():
    print("📥 Ingesting legacy data from Postgres...")
    raw_rows = fetch_legacy_data()

    valid_records = []
    exceptions = []

    for row in raw_rows:
        # Pre-process PII
        masked_card = mask_credit_card(row.get("credit_card"))

        candidate_payload = {
            "customer_id": row["id"],
            "company_name": row["raw_name"],
            "email": row["email"],
            "account_status": row["account_status"],
            "credit_card_masked": masked_card,
            "created_at": row["created_at"],
        }

        try:
            validated = SanitizedCustomerRecord(**candidate_payload)
            valid_records.append(validated.model_dump())
        except ValidationError as err:
            exceptions.append(
                {
                    "raw_record": row,
                    "validation_errors": _json_safe_errors(err.errors(include_url=False)),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "status": "REQUIRES_CONSULTANT_ACTION",
                }
            )

    # Persist Exceptions for Implementation Consultant
    with open("exceptions_for_consultant.json", "w", encoding="utf-8") as f:
        json.dump(exceptions, f, indent=2)
    print(f"📋 Isolated {len(exceptions)} exception(s) into 'exceptions_for_consultant.json'")

    # Send Valid Records to Superglue Core
    send_to_superglue(valid_records)


if __name__ == "__main__":
    main()
