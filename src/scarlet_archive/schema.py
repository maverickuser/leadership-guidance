"""JSON Schema for data/posts/{slug}.json (schema_version 1)."""

from __future__ import annotations

from jsonschema import Draft202012Validator

from .errors import ExtractError

SCHEMA_VERSION = 1

POST_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Scarlet Ink archived post",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version", "id", "url", "title", "subtitle", "author", "published_date",
        "categories", "word_count", "summary", "sections", "text", "links", "content_hash",
        "fetched_at", "status",
    ],
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]*$"},
        "url": {"type": "string", "pattern": "^https?://"},
        "title": {"type": "string", "minLength": 1},
        "subtitle": {"type": ["string", "null"]},
        "author": {"type": "string", "minLength": 1},
        "published_date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
        "categories": {"type": "array", "items": {"type": "string"}},
        "word_count": {"type": "integer", "minimum": 0},
        "summary": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["heading", "level", "text"],
                "properties": {
                    "heading": {"type": ["string", "null"]},
                    "level": {"type": "integer", "minimum": 0, "maximum": 6},
                    "text": {"type": "string", "minLength": 1},
                },
            },
        },
        "text": {"type": "string"},
        "links": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "url"],
                "properties": {"text": {"type": "string"}, "url": {"type": "string", "pattern": "^https?://"}},
            },
        },
        "content_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "fetched_at": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"},
        "status": {"enum": ["ok", "partial"]},
    },
}

_validator = Draft202012Validator(POST_SCHEMA)


def validate_post(record: dict) -> None:
    errors = sorted(_validator.iter_errors(record), key=lambda e: list(e.path))
    if errors:
        e = errors[0]
        where = "/".join(str(p) for p in e.path) or "(root)"
        raise ExtractError(f"schema: {where}: {e.message}")
