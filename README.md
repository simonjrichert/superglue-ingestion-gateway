# Superglue Ingestion & Pre-Validation Gateway

On-premise edge sanitizer for [Superglue.ai](https://superglue.ai). The gateway runs inside a customer VPC, reads dirty legacy SQL data, redacts PII, validates records with Pydantic v2, isolates failures for implementation consultants, and forwards schema-valid payloads to the Superglue Core Execution Engine.

Unstructured or corrupted legacy data degrades downstream LLM agent performance and inflates token cost. This container guarantees schema validity before Superglue's AI schema agents map records into enterprise ERPs such as Salesforce or NetSuite.

## Architecture

```
Customer VPC
  PostgreSQL (legacy_erp) ──► Pre-Processing Container (Python 3.11 / Pydantic v2)
                                      │
                    ┌─────────────────┴─────────────────┐
                    ▼                                   ▼
           Valid records (HTTP POST)          exceptions_for_consultant.json
                    ▼
           Superglue Core Execution Engine
```

## What it does

1. Connects to PostgreSQL with retry/backoff until the database is ready.
2. Masks credit-card numbers (`4111-****-****-4444`) before any validation or handover.
3. Enforces enterprise constraints:
   - RFC-compliant email (`EmailStr`)
   - `account_status` in `ACTIVE`, `INACTIVE`, `PENDING` (normalized to uppercase)
   - `created_at` as `YYYY-MM-DD`
4. Writes each failed row plus structured Pydantic errors to `exceptions_for_consultant.json`.
5. POSTs valid records to `SUPERGLUE_API_URL`. If the engine is unreachable, it prints the formatted payload (mock fallback) instead of crashing.

Seed data in `init_db.sql` is intentionally dirty so you can exercise both paths:

| Company        | Outcome                                      |
|----------------|----------------------------------------------|
| Acme Corp      | Valid                                        |
| Globex Inc     | Exception — invalid email                    |
| Initech LLC    | Valid                                        |
| Stark Ind      | Exception — unknown status and invalid date  |
| Umbrella Corp  | Valid                                        |

## Prerequisites

- Docker Engine 24+
- Docker Compose v2

## Run

```bash
cp .env.example .env   # optional; compose already injects defaults
docker compose up --build
```

The `gateway` service waits until Postgres is healthy, then runs `app.py` once. Exceptions land on the host at `./exceptions_for_consultant.json` because the container mounts the project directory.

Expected log shape:

```
📥 Ingesting legacy data from Postgres...
📋 Isolated 2 exception(s) into 'exceptions_for_consultant.json'
🚀 Forwarding 3 clean records to Superglue Engine (...)
⚠️ Could not reach Superglue API Endpoint (...).
💡 Mock Mode Active: Data successfully validated and payload formatted:
{ ... }
```

Tear down:

```bash
docker compose down
```

Postgres init scripts only run on first volume create. To re-seed from `init_db.sql`:

```bash
docker compose down -v
docker compose up --build
```

## Local Python run (without Compose)

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# start Postgres separately, then:
export DB_HOST=localhost
python app.py
```

## Configuration

| Variable             | Default                                      | Purpose                          |
|----------------------|----------------------------------------------|----------------------------------|
| `DB_HOST`            | `db` in Compose / `localhost` locally        | Legacy Postgres host             |
| `DB_PORT`            | `5432`                                       | Postgres port                    |
| `DB_NAME`            | `legacy_erp`                                 | Database name                    |
| `DB_USER`            | `admin`                                      | Database user                    |
| `DB_PASSWORD`        | `secretpassword`                             | Database password                |
| `SUPERGLUE_API_URL`  | `http://localhost:8080/api/v1/ingest`        | Core engine ingest endpoint      |
| `SUPERGLUE_API_KEY`  | `sg_live_mock_key_998877`                    | Bearer token for handover        |

Replace the mock API URL and key with your tenant's Superglue Core credentials in production. Do not commit live keys.

## Consultant exception log

Failed records look like:

```json
{
  "raw_record": { "id": 2, "raw_name": "Globex Inc", "email": "invalid-email-format", "...": "..." },
  "validation_errors": [ { "type": "value_error", "loc": ["email"], "msg": "..." } ],
  "timestamp": "2026-09-14T12:00:00+00:00",
  "status": "REQUIRES_CONSULTANT_ACTION"
}
```

Fix the source row in the legacy system, then re-run the gateway.

## Security notes

- Designed to stay inside the customer network boundary; only sanitized JSON leaves the VPC.
- Credit-card values are masked before validation and never appear in the outbound payload.
- Compose defaults (`secretpassword`, mock API key) are for local demonstration only.
