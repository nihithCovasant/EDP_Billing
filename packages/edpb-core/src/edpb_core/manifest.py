"""The batch manifest contract: the packaged JSON Schema plus load/validate
helpers. The schema file bundled here (edpb_core/manifest.schema.json) is THE
schema — the copies the repos used to carry in docs/ defer to this one.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

import jsonschema

MANIFEST_NAME = "manifest.json"


class ManifestValidationError(Exception):
    """The manifest is unreadable or violates the schema."""


@lru_cache
def manifest_schema() -> dict[str, Any]:
    """The packaged JSON Schema (draft 2020-12)."""
    text = resources.files("edpb_core").joinpath("manifest.schema.json").read_text("utf-8")
    return json.loads(text)


def validate_manifest(data: dict[str, Any]) -> None:
    """Raise ManifestValidationError if `data` violates the schema."""
    try:
        jsonschema.validate(data, manifest_schema())
    except jsonschema.ValidationError as exc:
        raise ManifestValidationError(exc.message) from exc


def load_manifest_file(path: Path | str) -> dict[str, Any]:
    """Read + validate a manifest.json from disk."""
    p = Path(path)
    if not p.is_file():
        raise ManifestValidationError(f"manifest not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestValidationError(f"manifest unreadable: {exc}") from exc
    validate_manifest(data)
    return data
