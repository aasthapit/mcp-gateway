# sample-upstreams

Two tiny FastMCP 4.1 servers, served over **stateless streamable HTTP** at `/mcp`:

| Service          | Port | Tools                | Intended toolset |
| ---------------- | ---- | -------------------- | ---------------- |
| `sample-public`  | 8081 | `time_now`, `echo`   | guest            |
| `sample-private` | 8082 | `secret_lookup(key)` | developers only  |

They are started by `local/compose.yaml`. To run one on the host instead:

```bash
cd sample-upstreams && uv run sample-public
```
