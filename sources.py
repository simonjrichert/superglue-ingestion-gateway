import csv
import os

import yaml


def load_sources(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not config or "sources" not in config:
        raise ValueError(f"Sources file '{path}' is missing a 'sources' map.")
    for name, spec in config["sources"].items():
        if not isinstance(spec, dict) or "type" not in spec:
            raise ValueError(f"Source '{name}' must define type.")
        if spec["type"] == "csv":
            if not spec.get("file"):
                raise ValueError(f"CSV source '{name}' is missing file.")
            if not spec.get("columns"):
                raise ValueError(
                    f"CSV source '{name}' is missing columns aliases."
                )
    config.setdefault("default", "postgres")
    return config


def alias_row(raw: dict, columns: dict) -> dict:
    """Map extractor headers onto the canonical names mapping.yaml expects."""
    normalized = {
        (key.strip() if isinstance(key, str) else key): value
        for key, value in raw.items()
    }
    aliased = {}
    for canonical, header in columns.items():
        if header in normalized:
            aliased[canonical] = normalized[header]
    return aliased


def fetch_csv_rows(spec: dict, base_dir: str) -> list[dict]:
    path = spec["file"]
    if not os.path.isabs(path):
        path = os.path.join(base_dir, path)
    encoding = spec.get("encoding", "utf-8-sig")
    with open(path, newline="", encoding=encoding) as handle:
        reader = csv.DictReader(handle)
        return [alias_row(row, spec["columns"]) for row in reader]
