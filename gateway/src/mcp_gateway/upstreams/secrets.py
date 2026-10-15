"""Resolve ``auth.secretRef`` to a credential value.

Secrets never live in Mongo. In a pod, ``secretRef`` names a key of a Kubernetes Secret
mounted at ``GATEWAY_UPSTREAM_SECRETS_DIR`` (one file per key, ESO/Vault populated). For
local development the same reference can come from an environment variable
``GATEWAY_UPSTREAM_SECRET_<REF>`` where ``<REF>`` is the reference upper-cased with every
non-alphanumeric character replaced by ``_`` (``upstream-github`` -> ``UPSTREAM_GITHUB``).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ENV_PREFIX = "GATEWAY_UPSTREAM_SECRET_"


class SecretMissing(LookupError):
    """The reference resolves to nothing; the upstream must not be mounted."""

    def __init__(self, ref: str, looked_in: list[str]) -> None:
        self.ref = ref
        self.looked_in = looked_in
        super().__init__(f"secret {ref!r} not found (looked in {', '.join(looked_in)})")


def env_var_for(ref: str) -> str:
    return ENV_PREFIX + re.sub(r"[^A-Za-z0-9]", "_", ref).upper()


def resolve_secret(ref: str, secrets_dir: str | os.PathLike[str]) -> str:
    """Return the secret value for ``ref`` or raise :class:`SecretMissing`.

    File contents are stripped of surrounding whitespace (Kubernetes Secret files often end
    with a newline). An empty file or empty variable counts as missing. Path traversal in
    ``ref`` is rejected by the schema pattern, and double-checked here.
    """
    if "/" in ref or ref in (".", "..") or not ref:
        raise SecretMissing(ref, ["<invalid reference>"])
    path = Path(secrets_dir) / ref
    looked_in = [str(path)]
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    if value:
        return value
    var = env_var_for(ref)
    looked_in.append(f"${var}")
    value = os.environ.get(var, "").strip()
    if value:
        return value
    raise SecretMissing(ref, looked_in)
