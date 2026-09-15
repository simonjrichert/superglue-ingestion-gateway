# Superglue Ingestion & Pre-Validation Gateway

On-premise edge sanitizer for [Superglue.ai](https://superglue.ai). **This example pipeline ingests a legacy ERP customer table and maps it into Salesforce CRM** (`Account` + `Contact`) before handover.

The gateway runs inside a customer VPC, reads dirty legacy SQL data, redacts PII, applies the Salesforce mapping in `mapping.yaml`, validates the resulting objects with Pydantic v2, isolates failures for implementation consultants, and forwards schema-valid Salesforce payloads to the Superglue Core Execution Engine.

Unstructured or corrupted legacy data degrades downstream LLM agent performance and inflates token cost. This container guarantees Salesforce schema validity before Superglue's AI schema agents load records into the target org.

## Architecture

```
Customer VPC
  PostgreSQL (legacy_erp)
           │
           ▼
  Pre-Processing Container (Python 3.11 / Pydantic v2)
    mask PAN → mapping.yaml → Salesforce Account + Contact
           │
     ┌─────┴──────────────────────────┐
     ▼                                ▼
  HTTP POST Salesforce objects   exceptions_for_consultant.json
     ▼
  Superglue Core Execution Engine  (target_system: salesforce_crm)
```

This repository is a Salesforce example. The same pattern applies to other ERPs (NetSuite, SAP, and so on): swap the target models and `mapping.yaml`, not the pipeline.

## Salesforce mapping

Legacy `legacy_customers` rows are **not** sent as cleaned SQL. They are transformed into Salesforce objects using `mapping.yaml`:

| Legacy column     | Salesforce object.field      | Rule |
|-------------------|------------------------------|------|
| `id`              | `Account.AccountExternalId__c`, `Contact.AccountExternalId__c` | Prefix `legacy_erp:` (idempotent external id) |
| `raw_name`        | `Account.Name`, `Contact.LastName` | Trim |
| —                 | `Account.Type`               | Constant `Customer` |
| `account_status`  | `Account.Status__c`          | `ACTIVE`→`Active`, `INACTIVE`→`Inactive`; anything else (including `PENDING`) is an exception |
| `email`           | `Contact.Email`              | RFC email |
| `created_at`      | `Account.CreatedDate`        | `YYYY-MM-DD` |
| `credit_card`     | *(not mapped)*               | Masked, then dropped — PAN never lands on Salesforce |

If Account mapping fails, the matching Contact is not forwarded. A source row is all-or-nothing.

## What it does

1. Connects to PostgreSQL with retry/backoff until the database is ready.
2. Masks credit-card numbers (`4111-****-****-4444`) before mapping, validation, or handover.
3. Applies `mapping.yaml` to build Salesforce `Account` and `Contact` payloads.
4. Validates those payloads against the Salesforce Pydantic models in `schemas.py`.
5. Writes each failed row plus mapping/validation errors to `exceptions_for_consultant.json`.
6. POSTs valid Salesforce objects to `SUPERGLUE_API_URL`. If the engine is unreachable, it prints the formatted payload (mock fallback) instead of crashing.

Seed data in `init_db.sql` is intentionally dirty so you can exercise both paths:

| Company        | Outcome |
|----------------|---------|
| Acme Corp      | Salesforce Account (`Active`) + Contact forwarded |
| Globex Inc     | Exception — invalid `Contact.Email` |
| Initech LLC    | Exception — `PENDING` has no Salesforce `Status__c` value |
| Stark Ind      | Exception — unmapped status and invalid `CreatedDate` |
| Umbrella Corp  | Salesforce Account (`Inactive`) + Contact forwarded |

## Prerequisites

- Docker Engine 24+
- Docker Compose v2

## Run

```bash
cp .env.example .env   # optional; compose already injects defaults
docker compose up --build
```

If Compose reports `permission denied ... docker.sock`, add your user to the `docker` group and open a new shell:

```bash
sudo usermod -aG docker "$USER"
newgrp docker
docker compose up --build
```

Alternatively run `sudo docker compose up --build`.

If containers start but the gateway times out talking to `db`, bridge traffic is being filtered. Allow inter-container communication:

```bash
sudo sysctl -w net.bridge.bridge-nf-call-iptables=0
```

The `gateway` service waits until Postgres is healthy, then runs `app.py` once. Exceptions land on the host at `./exceptions_for_consultant.json` because the container mounts the project directory.

Expected log shape:

```
📥 Ingesting legacy data from Postgres and mapping to salesforce_crm...
📋 Isolated 3 exception(s) into 'exceptions_for_consultant.json'
🚀 Forwarding 2 Salesforce Account(s) and 2 Contact(s) to Superglue Engine (...)
⚠️ Could not reach Superglue API Endpoint (...).
💡 Mock Mode Active: Data successfully mapped to Salesforce and payload formatted:
{
  "source_system": "legacy_erp_postgres",
  "target_system": "salesforce_crm",
  "data": {
    "Account": [ { "Name": "Acme Corp", "Status__c": "Active", "...": "..." } ],
    "Contact": [ { "LastName": "Acme Corp", "Email": "contact@acme.com", "...": "..." } ]
  }
}
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
| `MAPPING_FILE`       | `mapping.yaml` next to `app.py`              | Salesforce field mapping         |
| `SUPERGLUE_API_URL`  | `http://localhost:8080/api/v1/ingest`        | Core engine ingest endpoint      |
| `SUPERGLUE_API_KEY`  | `sg_live_mock_key_998877`                    | Bearer token for handover        |

Replace the mock API URL and key with your tenant's Superglue Core credentials in production. Do not commit live keys.

## Consultant exception log

Failed records look like:

```json
{
  "raw_record": { "id": 3, "raw_name": "Initech LLC", "account_status": "PENDING", "...": "..." },
  "errors": [
    {
      "type": "mapping_error",
      "code": "UNMAPPED_VALUE",
      "loc": ["Account", "Status__c"],
      "msg": "No Salesforce mapping for account_status 'PENDING'",
      "target_object": "Account",
      "target_field": "Status__c",
      "source_field": "account_status"
    }
  ],
  "timestamp": "2026-09-14T12:00:00+00:00",
  "status": "REQUIRES_CONSULTANT_ACTION"
}
```

Fix the source row (or extend `mapping.yaml` if the customer agrees `PENDING` should map), then re-run the gateway.

## Security notes

- Designed to stay inside the customer network boundary; only sanitized Salesforce JSON leaves the VPC.
- Credit-card values are masked before mapping and are not present on Account or Contact.
- Compose defaults (`secretpassword`, mock API key) are for local demonstration only.
