"""sample-private: a tool that must only be reachable through an elevated toolset."""

from __future__ import annotations

from fastmcp import FastMCP

from sample_upstreams._serve import serve

mcp = FastMCP(
    "sample-private",
    instructions="Private sample upstream. Tools: secret_lookup.",
)

_SECRETS = {
    "launch-code": "0000-0000-0000",
    "coffee-machine": "the password is the brand name",
}


@mcp.tool
def secret_lookup(key: str) -> str:
    """Look up a (fake) secret by key. Only developers should see this tool."""
    try:
        return _SECRETS[key]
    except KeyError:
        return f"no secret stored under {key!r}; known keys: {sorted(_SECRETS)}"


def main() -> None:
    serve(mcp, default_port=8082)


if __name__ == "__main__":
    main()
