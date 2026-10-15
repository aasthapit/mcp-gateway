"""sample-public: tools meant for the guest toolset."""

from __future__ import annotations

from datetime import UTC, datetime

from fastmcp import FastMCP

from sample_upstreams._serve import serve

mcp = FastMCP(
    "sample-public",
    instructions="Public sample upstream. Tools: time_now, echo.",
)


@mcp.tool
def time_now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


@mcp.tool
def echo(text: str) -> str:
    """Echo the given text back unchanged."""
    return text


def main() -> None:
    serve(mcp, default_port=8081)


if __name__ == "__main__":
    main()
