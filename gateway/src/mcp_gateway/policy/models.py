"""Pydantic models for the policy collections (``toolsets``, ``grants``, ``meta``).

They mirror ``schemas/*.schema.json`` (the source of truth, also compiled into the Mongo
``$jsonSchema`` validators); ``tests/unit/test_policy_models.py`` keeps the two in step by
checking every ``local/seed`` document against both. Upstream documents use
:class:`mcp_gateway.upstreams.models.UpstreamConfig`, re-exported here as
:data:`UpstreamDoc` so the policy layer has one import.

Field aliases are the Mongo/JSON spellings (``_id``, ``updatedAt``); attribute names are
snake_case. All models are frozen: a snapshot built from them is immutable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mcp_gateway.upstreams.models import UpstreamConfig

UpstreamDoc = UpstreamConfig

ID_PATTERN = r"^[a-z][a-z0-9-]{0,62}$"
GLOB_PATTERN = r"^[A-Za-z0-9_*?\[\]!.-]{1,200}$"
CLAIM_PATH_PATTERN = r"^[A-Za-z0-9_$:/.-]{1,200}$"

ClaimOp = Literal["eq", "in", "contains", "exists"]
Scalar = str | int | float | bool


class _Doc(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)


class AnonymousSubject(_Doc):
    """Matches every caller, authenticated or not: the guest floor."""

    kind: Literal["anonymous"]


class AuthenticatedSubject(_Doc):
    """Matches any caller that presented a valid token."""

    kind: Literal["authenticated"]


class ClaimSubject(_Doc):
    """Matches a dotted JSON path into the verified claims.

    ``eq``: the claim equals ``value`` (scalar). ``in``: the claim (or any element of a
    list claim) is one of ``value`` (list of scalars). ``contains``: ``value`` (scalar) is
    an element of the list claim, or a word of a space-separated string claim such as
    ``scope``. ``exists``: the path resolves to a non-null value; ``value`` must be absent.
    """

    kind: Literal["claim"]
    path: str = Field(pattern=CLAIM_PATH_PATTERN)
    op: ClaimOp
    value: Scalar | list[Scalar] | None = None

    @model_validator(mode="after")
    def _value_matches_op(self) -> ClaimSubject:
        is_list = isinstance(self.value, list)
        match self.op:
            case "exists":
                if self.value is not None:
                    raise ValueError("op=exists takes no value")
            case "in":
                if not is_list:
                    raise ValueError("op=in needs a list of accepted scalars")
            case "eq" | "contains":
                if self.value is None or is_list:
                    raise ValueError(f"op={self.op} needs a scalar value")
        return self


class ClientIdSubject(_Doc):
    """Matches the OAuth client the token was issued to (``azp`` / ``client_id``)."""

    kind: Literal["client_id"]
    value: str = Field(min_length=1, max_length=200)


Subject = Annotated[
    AnonymousSubject | AuthenticatedSubject | ClaimSubject | ClientIdSubject,
    Field(discriminator="kind"),
]


class ToolsetDoc(_Doc):
    """A named allow-list of namespaced tool names; ``deny`` wins over ``allow``."""

    id: str = Field(alias="_id", pattern=ID_PATTERN)
    description: str | None = Field(default=None, max_length=1000)
    enabled: bool
    allow: tuple[Annotated[str, Field(pattern=GLOB_PATTERN)], ...]
    deny: tuple[Annotated[str, Field(pattern=GLOB_PATTERN)], ...] = ()
    updated_at: datetime | None = Field(default=None, alias="updatedAt")
    updated_by: str | None = Field(default=None, alias="updatedBy", max_length=200)

    @model_validator(mode="after")
    def _unique_items(self) -> ToolsetDoc:
        for name in ("allow", "deny"):
            items = getattr(self, name)
            if len(set(items)) != len(items):
                raise ValueError(f"{name} contains duplicates")
        return self


class GrantDoc(_Doc):
    """Who gets which toolsets."""

    id: str = Field(alias="_id", pattern=ID_PATTERN)
    description: str | None = Field(default=None, max_length=1000)
    enabled: bool
    priority: int = Field(default=0, ge=0, le=100_000)
    """Display/audit ordering only; does not change set semantics."""
    subject: Subject
    toolsets: tuple[Annotated[str, Field(pattern=ID_PATTERN)], ...] = Field(min_length=1)
    updated_at: datetime | None = Field(default=None, alias="updatedAt")
    updated_by: str | None = Field(default=None, alias="updatedBy", max_length=200)

    @model_validator(mode="after")
    def _unique_toolsets(self) -> GrantDoc:
        if len(set(self.toolsets)) != len(self.toolsets):
            raise ValueError("toolsets contains duplicates")
        return self


class MetaDoc(_Doc):
    """Singleton ``{_id: "policy", version}`` bumped by every admin write."""

    id: Literal["policy"] = Field(alias="_id")
    version: int = Field(ge=0)
    updated_at: datetime | None = Field(default=None, alias="updatedAt")
    updated_by: str | None = Field(default=None, alias="updatedBy", max_length=200)


COLLECTION_MODELS: dict[str, type[BaseModel]] = {
    "upstreams": UpstreamDoc,
    "toolsets": ToolsetDoc,
    "grants": GrantDoc,
    "meta": MetaDoc,
}


def parse_document(collection: str, raw: dict[str, Any]) -> BaseModel:
    """Validate one raw Mongo/JSON document for ``collection``."""
    return COLLECTION_MODELS[collection].model_validate(raw)
