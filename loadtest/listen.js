// Listen-stream holders: each VU keeps one `subscriptions/listen` stream open for
// LISTEN_HOLD_SEC, then re-listens (as clients do after a graceful close). k6 only sees the
// response when it ends, so the stream ends by client timeout; those requests are tagged
// op=listen and excluded from the error/latency thresholds. While they are open the
// gateway's gateway_open_listen_streams gauge shows them (sampled by run.sh).
import { rpc } from "./lib/mcp.js";

const BASE = __ENV.GATEWAY_URL || "http://127.0.0.1:18080";
const HOLD = Number(__ENV.LISTEN_HOLD_SEC || 20);

export function listenHolder() {
  rpc(
    BASE,
    "subscriptions/listen",
    { notifications: { toolsListChanged: true } },
    { tags: { op: "listen" }, timeout: `${HOLD}s` },
  );
}
