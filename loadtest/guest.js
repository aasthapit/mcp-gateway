// Guest (anonymous) traffic: 70 % tools/list, 30 % tools/call sample-public_echo.
// The guest floor (local/seed: public-basics) is exactly time_now + echo.
import { check } from "k6";
import { Rate, Trend } from "k6/metrics";

import { ok, rpc, toolNames } from "./lib/mcp.js";

export const listCached = new Trend("mcp_list_cached", true);
export const callGuest = new Trend("mcp_call_guest", true);
export const errors = new Rate("mcp_errors");

const BASE = __ENV.GATEWAY_URL || "http://127.0.0.1:18080";

export function guestMix() {
  if (Math.random() < 0.7) {
    const res = rpc(BASE, "tools/list", {}, { tags: { op: "list", who: "guest" } });
    const names = toolNames(res);
    const good = check(res, {
      "guest list 200": (r) => r.status === 200,
      "guest sees the floor only": () =>
        names.includes("sample-public_echo") && !names.includes("sample-private_secret_lookup"),
    });
    listCached.add(res.timings.duration);
    errors.add(!good);
  } else {
    const res = rpc(
      BASE,
      "tools/call",
      { name: "sample-public_echo", arguments: { text: "k6" } },
      { tags: { op: "call", who: "guest" } },
    );
    const good = check(res, { "guest call ok": (r) => ok(r) });
    callGuest.add(res.timings.duration);
    errors.add(!good);
  }
}
