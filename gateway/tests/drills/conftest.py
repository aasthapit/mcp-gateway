"""Failure drills (``make drills``): skipped unless ``GATEWAY_DRILLS=1``.

They need the local stack (``make up && make seed``) and a container runtime
(``CONTAINER``, default podman). They never restart the shared stack's containers: the
Mongo drill runs its own throwaway replica set, the upstream drill its own upstream and
the IdP drill cuts a TCP proxy in front of Keycloak.
"""

from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("GATEWAY_DRILLS") == "1":
        return
    skip = pytest.mark.skip(reason="failure drill: set GATEWAY_DRILLS=1 (make drills)")
    for item in items:
        if "drill" in item.keywords:
            item.add_marker(skip)
