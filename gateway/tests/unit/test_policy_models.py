"""The pydantic models agree with schemas/*.schema.json on the seed data and on rejects."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft4Validator
from pydantic import ValidationError

from mcp_gateway.policy.models import (
    COLLECTION_MODELS,
    ClaimSubject,
    GrantDoc,
    MetaDoc,
    ToolsetDoc,
    parse_document,
)

REPO = Path(__file__).resolve().parents[3]
SEED = REPO / "local" / "seed"
SCHEMAS = REPO / "schemas"

META_SAMPLE: dict[str, Any] = {
    "_id": "policy",
    "version": 7,
    "updatedAt": "2026-10-15T10:00:00Z",
    "updatedBy": "seed",
}

#: One invalid sample per collection; both the JSON Schema and pydantic must reject it.
INVALID_SAMPLES: dict[str, tuple[str, Any]] = {
    "upstreams": (
        "transport outside the enum",
        {
            "_id": "bad",
            "displayName": "x",
            "enabled": True,
            "transport": "grpc",
            "url": "http://x/mcp",
            "auth": {"type": "none"},
            "sessionMode": "shared",
        },
    ),
    "toolsets": (
        "glob with a space and a missing enabled flag",
        {"_id": "bad", "allow": ["sample public_*"]},
    ),
    "grants": (
        "claim subject without op",
        {
            "_id": "bad",
            "enabled": True,
            "subject": {"kind": "claim", "path": "roles"},
            "toolsets": ["public-basics"],
        },
    ),
    "meta": ("wrong singleton id", {"_id": "policies", "version": 1}),
}


def seed_docs(collection: str) -> list[dict[str, Any]]:
    if collection == "meta":
        return [META_SAMPLE]
    return json.loads((SEED / f"{collection}.json").read_text())


def validator(collection: str) -> Draft4Validator:
    schema = json.loads((SCHEMAS / f"{collection}.schema.json").read_text())
    return Draft4Validator(schema)


@pytest.mark.parametrize("collection", sorted(COLLECTION_MODELS))
def test_seed_documents_are_accepted_by_schema_and_models(collection: str) -> None:
    docs = seed_docs(collection)
    assert docs, f"no seed documents for {collection}"
    v = validator(collection)
    for raw in docs:
        v.validate(raw)
        model = parse_document(collection, raw)
        assert model.id == raw["_id"]


@pytest.mark.parametrize("collection", sorted(INVALID_SAMPLES))
def test_invalid_sample_is_rejected_by_schema_and_models(collection: str) -> None:
    why, raw = INVALID_SAMPLES[collection]
    assert not validator(collection).is_valid(raw), f"schema accepted: {why}"
    with pytest.raises(ValidationError):
        parse_document(collection, raw)


def test_models_forbid_unknown_fields_like_the_schema() -> None:
    raw = copy.deepcopy(seed_docs("toolsets")[0])
    raw["extra"] = 1
    assert not validator("toolsets").is_valid(raw)
    with pytest.raises(ValidationError):
        ToolsetDoc.model_validate(raw)


def test_models_accept_mongo_shaped_dates() -> None:
    from datetime import UTC, datetime

    raw = copy.deepcopy(seed_docs("grants")[0])
    raw["updatedAt"] = datetime(2026, 10, 15, tzinfo=UTC)  # BSON date, not a string
    g = GrantDoc.model_validate(raw)
    assert g.updated_at is not None and g.updated_at.year == 2026
    assert MetaDoc.model_validate(META_SAMPLE).version == 7


@pytest.mark.parametrize(
    ("op", "value", "ok"),
    [
        ("eq", "a", True),
        ("eq", ["a"], False),
        ("eq", None, False),
        ("in", ["a", 1, True], True),
        ("in", "a", False),
        ("contains", "a", True),
        ("contains", ["a"], False),
        ("exists", None, True),
        ("exists", "a", False),
    ],
)
def test_claim_subject_value_shape_follows_op(op: str, value: Any, ok: bool) -> None:
    raw = {"kind": "claim", "path": "roles", "op": op}
    if value is not None:
        raw["value"] = value
    if ok:
        assert ClaimSubject.model_validate(raw).op == op
    else:
        with pytest.raises(ValidationError):
            ClaimSubject.model_validate(raw)


def test_duplicates_rejected() -> None:
    with pytest.raises(ValidationError):
        ToolsetDoc.model_validate({"_id": "t", "enabled": True, "allow": ["a", "a"]})
    with pytest.raises(ValidationError):
        GrantDoc.model_validate(
            {"_id": "g", "enabled": True, "subject": {"kind": "anonymous"}, "toolsets": ["a", "a"]}
        )
