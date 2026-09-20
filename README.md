# Superglue Ingestion & Pre-Validation Gateway

On-premise edge sanitizer for [Superglue.ai](https://superglue.ai). **This example pipeline ingests a legacy ERP customer table and maps it into Salesforce CRM** (`Account` + `Contact`) before handover.

The gateway runs inside a customer VPC, reads dirty legacy SQL data, redacts PII, applies the Salesforce mapping in `mapping.yaml`, validates the resulting objects with Pydantic v2, isolates failures for implementation consultants, and forwards schema-valid Salesforce payloads to the Superglue Core Execution Engine.

Unstructured or corrupted legacy data degrades downstream LLM agent performance and inflates token cost. This container guarantees Salesforce schema validity before Superglue's AI schema agents load records into the target org.

## Architecture

```
Customer VPC
  PostgreSQL (legacy_erp)     CSV export (client column names)
           │                         │
           └──────────┬──────────────┘
                      ▼
  Pre-Processing Container (Python 3.11 / Pydantic v2)
    alias headers → mask PAN → mapping.yaml → Salesforce Account + Contact
           │
     ┌─────┴──────────────────────────┐
     ▼                                ▼
  HTTP POST  or  --dry-run          exceptions_for_consultant.json
  (2xx → state/forwarded.json)      dry_run_payload.json
  (fail → outbox/*.json)
     ▼
  POST /v1/tools/{toolId}/run  (inputs: Account + Contact)
```

This repository is a Salesforce example. The same pattern applies to other ERPs (NetSuite, SAP, and so on): swap the target models and `mapping.yaml`, not the pipeline. Postgres is the default extract; CSV is an alternate extract of a different dump with different headers. Both alias onto the same canonical fields. They are **not** merged in one run.

## Why I built it this way

I treated this as an on-prem customer POC, not a hosted demo. The Compose stack is a stand-in for a VPC: the gateway container reads the customer's Postgres (`legacy_erp.legacy_customers`) and only then opens an outbound HTTP call. Nothing in the happy path assumes a SaaS connector already has clean REST objects.

I also refused to "fix" the SQL in place. Cleaning `account_status` or `created_at` and posting a slightly nicer `legacy_customers` row is not an implementation. Salesforce does not store `raw_name` or `PENDING`. I mapped each source row into `Account` and `Contact` (`schemas.py`) so the payload is something the target org could actually load. The handover is a tool run (`POST /v1/tools/{toolId}/run`): `inputs.Account` / `inputs.Contact` match that Salesforce contract. The saved tool upserts into the org; this gateway does not call the Salesforce API.

The field decisions live in `mapping.yaml` on purpose. That file is the written result of sitting with the customer: `id` becomes `AccountExternalId__c` (`legacy_erp:{id}`) so reruns do not duplicate Accounts; `ACTIVE`/`INACTIVE` become `Active`/`Inactive`; `PENDING` has no picklist value so it is parked, not coerced; `credit_card` is not a Salesforce field at all. I did not bury those rules in `if` statements in `app.py`. If the customer later agrees `PENDING` should map, the change is the YAML (and the expected-outcome table below), not a hidden code path.

Clients often do not give you the Postgres table. `sources.yaml` is the extract side of that workshop: Postgres already uses the canonical names; the CSV export uses `Company Name`, `E-Mail`, `Status`. Those headers are aliased onto the same `raw_name` / `email` / `account_status` fields `mapping.yaml` already knows. The Salesforce contract does not move when the dump format does.

Failures are scoped to the row, not the batch. Unmapped statuses, bad emails, and unparseable dates go to `exceptions_for_consultant.json` with the Salesforce object, field, source column, and a stable error code (`UNMAPPED_VALUE`, `INVALID_DATE`, …). Valid Accounts/Contacts still forward. I would rather an implementation consultant fix three source rows than push illegal picklist values into Salesforce or abort the whole load because Globex has a broken email.

Safety here is about egress, not a scanner bolted on at the end. I mask PAN on a working copy of the row before mapping, drop `credit_card` from the target schema, and keep the exception log on that same masked copy. The only JSON meant to leave the network is Salesforce-shaped records that never contained a full card number.

`--dry-run` is the rehearsal before that egress. Same extract, mask, mapping, and exception file; no HTTP. You inspect `dry_run_payload.json` with the consultant, then run again without the flag. That is different from the **outbox**: dry-run is "I did not intend to POST yet." Outbox is "I intended to POST and the engine failed; the payload is on disk." Successful POSTs record `AccountExternalId__c` in `state/forwarded.json` so a later run does not send Acme (`legacy_erp:1`) again.

## Salesforce mapping

Legacy `legacy_customers` rows are **not** sent as cleaned SQL. They are transformed into Salesforce objects using `mapping.yaml`:

| Legacy column     | Salesforce object.field      | Rule |
|-------------------|------------------------------|------|
| `id`              | `Account.AccountExternalId__c`, `Contact.AccountExternalId__c` | Prefix `legacy_erp:` (idempotent external id) |
| `raw_name`        | `Account.Name`, `Contact.LastName` | Trim |
| —                 | `Account.Type`               | Constant `Customer` |
| `account_status`  | `Account.Status__c`          | `ACTIVE`→`Active`, `INACTIVE`→`Inactive`; anything else (including `PENDING`) is an exception |
| `email`           | `Contact.Email`              | RFC email |
| `created_at`      | `Account.CreatedDate`        | `YYYY-MM-DD` or `DD/MM/YYYY` |
| `credit_card`     | *(not mapped)*               | Masked, then dropped — PAN never lands on Salesforce |
| `notes`           | *(not mapped)*               | Free text; card-shaped substrings are masked, then dropped |

If Account mapping fails, the matching Contact is not forwarded. A source row is all-or-nothing.

## What it does

1. Chooses an extractor from `sources.yaml` (`postgres` by default, or `csv`).
2. Connects to PostgreSQL with retry/backoff, **or** reads `fixtures/customers_export.csv` and aliases client headers onto canonical field names.
3. Masks credit-card numbers (`4111-****-****-4444`) in `credit_card` and in `notes` before mapping, validation, or handover.
4. Applies `mapping.yaml` to build Salesforce `Account` and `Contact` payloads.
5. Validates those payloads against the Salesforce Pydantic models in `schemas.py`.
6. Writes each failed row plus mapping/validation errors to `exceptions_for_consultant.json`.
7. If `--dry-run` / `DRY_RUN=1`: writes the would-be handover to `dry_run_payload.json` and **does not POST** (and does not write the outbox).
8. Otherwise POSTs `{ inputs: { Account, Contact }, options: { async: true } }` to `SUPERGLUE_API_URL/tools/{SUPERGLUE_TOOL_ID}/run`. HTTP 2xx (including 202) records those `AccountExternalId__c` values in `state/forwarded.json`. If the engine is unreachable or returns a non-2xx, the payload is written to `outbox/<batch_id>.json` and those ids are **not** marked forwarded.
9. A later run skips ids already forwarded or already sitting in the outbox (replay). `--flush-outbox` POSTs pending files without extracting again.

Seed data in `init_db.sql` is intentionally dirty so you can exercise both paths. Placeholders such as `N/A` / `null` are treated as empty. Duplicate `Name` values are **not** merged — identity is `AccountExternalId__c`.

| Company | Dirt | Outcome |
|---------|------|---------|
| Acme Corp | Happy path | Account (`Active`) + Contact forwarded (`legacy_erp:1`) |
| Globex Inc | Invalid email | Exception — invalid `Contact.Email` |
| Initech LLC | `PENDING` | Exception — no Salesforce `Status__c` value |
| Stark Ind | Unknown status + invalid date | Exception — unmapped status and invalid `CreatedDate` |
| Umbrella Corp | `INACTIVE` | Account (`Inactive`) + Contact forwarded |
| Soylent Corp | Leading/trailing spaces in `raw_name` | Forwarded; `Name` / `LastName` trimmed to `Soylent Corp` |
| Hooli | `created_at = 15/03/2026` | Forwarded; `CreatedDate=2026-03-15` |
| Massive Dynamic | `email = N/A` | Exception — `EMPTY_VALUE` on `Contact.Email` |
| Tyrell Corp | PAN only in `notes` | Forwarded; notes never on Salesforce; card in notes is masked on the working row |
| (Wayne) | Blank `raw_name` | Exception — `EMPTY_VALUE` on `Account.Name` and `Contact.LastName` |
| Oscorp | Same `Name` as Acme, different email | Forwarded as a **second** Account (`legacy_erp:11`); no silent dedupe |

Expected split: **6 forwarded**, **5 exceptions**.

## Second source: CSV export

Postgres is the default Compose path. The CSV path is a second extract: the same Salesforce mapping, different column names, different companies, different ids (`101`–`106` so `legacy_erp:101` cannot collide with `legacy_erp:1`).

`fixtures/customers_export.csv` looks like a file a client emailed (`Company Name`, `E-Mail`, `Status`, extra `Region` column that is dropped). `sources.yaml` aliases those headers onto `raw_name` / `email` / `account_status` / … then `mapping.yaml` runs unchanged. PAN is masked **after** aliasing so `Card Number` and `Comments` hit the same mask as SQL `credit_card` / `notes`. Encoding is `utf-8-sig` so an Excel BOM does not become part of the first header.

Do not run both sources in one batch. Pick one:

```bash
python app.py --source csv
```

Or with Compose (skip Postgres; the gateway does not need it for this path):

```bash
docker compose run --no-deps --rm -e SOURCE=csv gateway
```

`--source` overrides `SOURCE`. Both default to `postgres`.

| Company | Dirt | Outcome |
|---------|------|---------|
| Wonka Industries | Happy path (`Customer ID=101`) | Account (`Active`) + Contact forwarded (`legacy_erp:101`) |
| Aperture Science | `Status=inactive`, `Created=20/06/2026` | Forwarded; `Status__c=Inactive`, `CreatedDate=2026-06-20` |
| Cyberdyne Systems | `PENDING` | Exception — no Salesforce `Status__c` value (`UNMAPPED_VALUE`) |
| (blank name) | Empty `Company Name` | Exception — `EMPTY_VALUE` on `Account.Name` and `Contact.LastName` |
| Vandelay Industries | Invalid email | Exception — invalid `Contact.Email` |
| Gekko & Co | PAN only in `Comments` | Forwarded; comments never on Salesforce; card in comments is masked on the working row |

Expected split: **3 forwarded**, **3 exceptions**. Extract is `customer_csv_export`; the tool-run body is still `inputs.Account` / `inputs.Contact`.

## Dry-run

`--dry-run` is a rehearsal of the load, not a copy of customer infra. Mapping and exceptions still run; nothing is posted to the engine.

Use it to answer "how many would forward, which rows park, what would Acme/Wonka look like as Salesforce objects?" before a real handover. Inspect:

- `exceptions_for_consultant.json` — parked rows and error codes
- `dry_run_payload.json` — the exact JSON that a non-dry run would POST

```bash
python app.py --dry-run
python app.py --source csv --dry-run
```

Compose (CSV does not need Postgres):

```bash
docker compose run --no-deps --rm -e SOURCE=csv -e DRY_RUN=1 gateway
```

Postgres dry-run still needs a healthy `db` (or a local Python run with Postgres up):

```bash
docker compose run --rm -e DRY_RUN=1 gateway
```

`--dry-run` overrides nothing about the mapping. `DRY_RUN=1` (or `true` / `yes` / `on`) is the same flag for Compose. A run without either still POSTs. Failed POSTs go to the outbox instead of a throwaway mock print.

## Outbox and replay

Identity is `AccountExternalId__c` (`legacy_erp:{id}`). Mapping already stamps that key; outbox/replay is the **workflow** around it.

**Outbox** (`outbox/<batch_id>.json`): if a real run cannot deliver (connection error or non-2xx), the Salesforce payload is saved on disk. Those ids are not written to `state/forwarded.json`. Re-run the gateway (it tries to flush existing outbox files first) or:

```bash
python app.py --flush-outbox
docker compose run --no-deps --rm -e FLUSH_OUTBOX=1 gateway
```

Dry-run never writes the outbox.

**Replay** (`state/forwarded.json`): after HTTP 2xx, external ids are recorded. The next extract still maps every row (exceptions stay visible) but **skips** Accounts already forwarded or already pending in outbox. Acme (`legacy_erp:1`) is not POSTed twice. If a consultant later fixes Globex, that new id was never forwarded, so it goes out on the next live run.

To rehearse from scratch, delete `state/forwarded.json` and `outbox/*.json` (both are gitignored). Compose mounts the project directory, so those files land on the host.

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

The `gateway` service waits until Postgres is healthy, then runs `app.py` once. Exceptions land on the host at `./exceptions_for_consultant.json` because the container mounts the project directory. A dry-run also writes `./dry_run_payload.json` there. A failed live POST writes `./outbox/*.json`; a successful POST updates `./state/forwarded.json`.

Expected log shape (first live run, engine down):

```
📥 Ingesting from 'postgres' (postgres) and mapping to salesforce_crm...
📋 Isolated 5 exception(s) into 'exceptions_for_consultant.json'
🚀 Forwarding 6 Salesforce Account(s) and 6 Contact(s) to Superglue Engine (...)
⚠️ Could not reach Superglue API Endpoint (...).
📦 Payload saved to 'outbox/batch_....json'. It is not marked forwarded.
```

Second live run, still down:

```
⏭️ Replay: skipped 6 mapped row(s) already in 'state/forwarded.json' or outbox.
⚠️ No valid Salesforce records to send to Superglue.
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
# CSV path — no database:
python app.py --source csv
python app.py --source csv --dry-run
# Postgres path — start Postgres separately, then:
export DB_HOST=localhost
python app.py
python app.py --dry-run
python app.py --flush-outbox
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
| `SOURCES_FILE`       | `sources.yaml` next to `app.py`              | Extractors and CSV column aliases |
| `SOURCE`             | `postgres`                                   | Extractor name (`postgres` or `csv`) |
| `DRY_RUN`            | unset / `0`                                  | `1` maps and writes files, skips POST |
| `FLUSH_OUTBOX`       | unset / `0`                                  | `1` POSTs `outbox/*.json` only       |
| `OUTBOX_DIR`         | `outbox`                                     | Failed-handover JSON                 |
| `FORWARDED_FILE`     | `state/forwarded.json`                       | Replay ledger of sent external ids   |
| `SUPERGLUE_API_URL`  | `https://api.superglue.ai/v1`                 | API root (or a full `.../run` URL) |
| `SUPERGLUE_TOOL_ID`  | `upsert-salesforce-customers`                | Saved Salesforce upsert tool       |
| `SUPERGLUE_API_KEY`  | `sg_live_mock_key_998877`                    | Bearer token for handover          |

Replace the mock tool id and key with your tenant's saved tool and API key. Do not commit live keys. The gateway never POSTs to Salesforce directly.

## Consultant exception log

Failed records look like:

```json
{
  "source": "postgres",
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
- Credit-card values are masked in `credit_card` and in `notes` before mapping and are not present on Account or Contact.
- Compose defaults (`secretpassword`, mock API key) are for local demonstration only.
