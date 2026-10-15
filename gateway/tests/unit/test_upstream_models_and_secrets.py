from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from mcp_gateway.upstreams.models import UpstreamConfig
from mcp_gateway.upstreams.secrets import SecretMissing, env_var_for, resolve_secret

BASE = {
    "_id": "github",
    "displayName": "GitHub",
    "enabled": True,
    "transport": "http",
    "url": "http://mcp-github.svc:8080/mcp",
    "auth": {"type": "static_bearer", "secretRef": "upstream-github"},
    "sessionMode": "shared",
    "timeoutsMs": {"connect": 1500, "call": 45000},
    "cacheTtlSec": 120,
    "tags": ["external"],
    "updatedAt": "2026-10-15T10:00:00Z",
    "updatedBy": "seed",
}


def test_config_from_mongo_document() -> None:
    cfg = UpstreamConfig.model_validate(BASE)
    assert cfg.id == "github" and cfg.namespaced("search") == "github_search"
    assert cfg.auth.secret_ref == "upstream-github"
    assert cfg.timeouts_ms.connect_s == 1.5 and cfg.timeouts_ms.call_s == 45.0
    assert cfg.protocol_mode == "auto"  # not in the schema -> default
    assert cfg.tags == ("external",)


def test_fingerprint_ignores_audit_fields_but_not_mount_fields() -> None:
    a = UpstreamConfig.model_validate(BASE)
    b = UpstreamConfig.model_validate(
        {**BASE, "updatedAt": "2026-10-16T00:00:00Z", "updatedBy": "x"}
    )
    c = UpstreamConfig.model_validate({**BASE, "cacheTtlSec": 5})
    assert a.same_mount_as(b)
    assert not a.same_mount_as(c)


@pytest.mark.parametrize(
    "patch, message",
    [
        ({"url": None}, "requires url"),
        ({"transport": "stdio", "url": None}, "requires command"),
        ({"auth": {"type": "static_bearer"}}, "requires auth.secretRef"),
        ({"auth": {"type": "static_header", "secretRef": "x"}}, "requires auth.headerName"),
        ({"_id": "Bad_Id"}, "pattern"),
        ({"url": "ftp://x"}, "pattern"),
    ],
)
def test_config_rejects_inconsistent_documents(patch: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        UpstreamConfig.model_validate({**BASE, **patch})


def test_stdio_config() -> None:
    cfg = UpstreamConfig.model_validate(
        {
            **BASE,
            "transport": "stdio",
            "url": None,
            "command": "uvx",
            "args": ["mcp-x"],
            "auth": {"type": "none"},
        }
    )
    assert cfg.command == "uvx" and cfg.args == ("mcp-x",)


# -- secrets ------------------------------------------------------------------------------


def test_secret_from_file_is_trimmed(tmp_path: Path) -> None:
    (tmp_path / "upstream-github").write_text("  tok3n\n")
    assert resolve_secret("upstream-github", tmp_path) == "tok3n"


def test_secret_env_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert env_var_for("upstream-github.v2") == "GATEWAY_UPSTREAM_SECRET_UPSTREAM_GITHUB_V2"
    monkeypatch.setenv("GATEWAY_UPSTREAM_SECRET_UPSTREAM_GITHUB", " from-env ")
    assert resolve_secret("upstream-github", tmp_path) == "from-env"
    (tmp_path / "upstream-github").write_text("from-file")
    assert resolve_secret("upstream-github", tmp_path) == "from-file"  # file wins


def test_secret_missing_lists_where_it_looked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GATEWAY_UPSTREAM_SECRET_NOPE", raising=False)
    (tmp_path / "empty").write_text("\n")
    with pytest.raises(SecretMissing) as exc:
        resolve_secret("nope", tmp_path)
    assert str(tmp_path / "nope") in str(exc.value) and "GATEWAY_UPSTREAM_SECRET_NOPE" in str(
        exc.value
    )
    with pytest.raises(SecretMissing):
        resolve_secret("empty", tmp_path)
    with pytest.raises(SecretMissing):
        resolve_secret("../etc/passwd", tmp_path)
