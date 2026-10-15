"""Upstream MCP servers: configuration models, secrets, proxies and the registry."""

from mcp_gateway.upstreams.models import CatalogTool, UpstreamConfig, UpstreamHealth
from mcp_gateway.upstreams.registry import MountedUpstream, UpstreamRegistry

__all__ = ["CatalogTool", "MountedUpstream", "UpstreamConfig", "UpstreamHealth", "UpstreamRegistry"]
