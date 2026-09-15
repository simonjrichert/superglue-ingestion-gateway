from datetime import datetime

import yaml

from schemas import OBJECT_MODELS


class MappingError(Exception):
    def __init__(
        self,
        code: str,
        msg: str,
        *,
        target_object: str,
        target_field: str,
        source_field: str | None = None,
    ):
        super().__init__(msg)
        self.code = code
        self.msg = msg
        self.target_object = target_object
        self.target_field = target_field
        self.source_field = source_field

    def to_dict(self) -> dict:
        loc = [self.target_object, self.target_field]
        error = {
            "type": "mapping_error",
            "code": self.code,
            "loc": loc,
            "msg": self.msg,
            "target_object": self.target_object,
            "target_field": self.target_field,
        }
        if self.source_field is not None:
            error["source_field"] = self.source_field
        return error


def load_mapping(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        mapping = yaml.safe_load(handle)
    if not mapping or "objects" not in mapping:
        raise ValueError(f"Mapping file '{path}' is missing an 'objects' list.")
    for obj in mapping["objects"]:
        name = obj.get("name")
        if name not in OBJECT_MODELS:
            raise ValueError(f"Mapping references unknown Salesforce object '{name}'.")
    return mapping


def _is_empty(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _transforms(spec: dict) -> list[str]:
    raw = spec.get("transform")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    return list(raw)


def _apply_transform(
    value,
    name: str,
    spec: dict,
    *,
    target_object: str,
    target_field: str,
    source_field: str | None,
):
    if name == "trim":
        return value.strip() if isinstance(value, str) else str(value).strip()
    if name == "uppercase":
        return value.upper() if isinstance(value, str) else str(value).upper()
    if name == "prefix":
        prefix = spec.get("prefix", "")
        return f"{prefix}{value}"
    if name == "parse_date":
        text = str(value).strip()
        formats = spec.get("formats") or ["%Y-%m-%d"]
        for fmt in formats:
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        raise MappingError(
            "INVALID_DATE",
            f"Date '{value}' does not match expected format(s) {formats}",
            target_object=target_object,
            target_field=target_field,
            source_field=source_field,
        )
    raise MappingError(
        "UNKNOWN_TRANSFORM",
        f"Unknown transform '{name}'",
        target_object=target_object,
        target_field=target_field,
        source_field=source_field,
    )


def resolve_field(row: dict, spec: dict, target_object: str, target_field: str):
    if "const" in spec:
        return spec["const"]

    source_field = spec.get("source")
    if not source_field:
        raise MappingError(
            "MISSING_SOURCE",
            f"Mapping for {target_object}.{target_field} has no source or const",
            target_object=target_object,
            target_field=target_field,
        )

    if source_field not in row:
        raise MappingError(
            "MISSING_SOURCE",
            f"Source column '{source_field}' is not present on the legacy row",
            target_object=target_object,
            target_field=target_field,
            source_field=source_field,
        )

    value = row[source_field]
    required = spec.get("required", True)
    if required and _is_empty(value):
        raise MappingError(
            "EMPTY_VALUE",
            f"Source column '{source_field}' is empty",
            target_object=target_object,
            target_field=target_field,
            source_field=source_field,
        )
    if _is_empty(value):
        return None

    for transform_name in _transforms(spec):
        value = _apply_transform(
            value,
            transform_name,
            spec,
            target_object=target_object,
            target_field=target_field,
            source_field=source_field,
        )

    value_map = spec.get("map")
    if value_map is not None:
        if value not in value_map:
            raise MappingError(
                "UNMAPPED_VALUE",
                f"No Salesforce mapping for {source_field} '{value}' "
                f"(allowed legacy values: {list(value_map.keys())})",
                target_object=target_object,
                target_field=target_field,
                source_field=source_field,
            )
        value = value_map[value]

    return value


def map_object(row: dict, object_spec: dict) -> tuple[dict | None, list[dict]]:
    target_object = object_spec["name"]
    payload = {}
    errors = []
    for target_field, spec in object_spec["fields"].items():
        try:
            payload[target_field] = resolve_field(row, spec, target_object, target_field)
        except MappingError as err:
            errors.append(err.to_dict())
    if errors:
        return None, errors
    return payload, []
