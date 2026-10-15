"""Upstream MCP servers: configuration models and the registry that mounts them."""

from mcp_gateway.upstreams.models import CatalogTool, UpstreamConfig, UpstreamHealth
from mcp_gateway.upstreams.registry import UpstreamRegistry

__all__ = ["CatalogTool", "UpstreamConfig", "UpstreamHealth", "UpstreamRegistry"]
