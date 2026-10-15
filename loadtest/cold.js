// Cold tools/list: list caching off (cache_ttl_sec=0), so every list fans out to both
// upstreams before policy filtering.
//   coldList          :18081  shared upstream sessions (seed default): one round trip each
//   coldListNegotiate :18082  per_request sessions, protocol auto: every list also opens a
//                              connection and negotiates (server/discover) per upstream,
//                              the worst case for a legacy/unknown upstream
import { check } from "k6";
import { Trend } from "k6/metrics";

import { errors } from "./guest.js";
import { rpc, toolNames } from "./lib/mcp.js";

export const listCold = new Trend("mcp_list_cold", true);
export const listColdNegotiate = new Trend("mcp_list_cold_negotiate", true);

const COLD = __ENV.GATEWAY_COLD_URL || "http://127.0.0.1:18081";
const COLD_NEGOTIATE = __ENV.GATEWAY_COLD_NEGOTIATE_URL || "http://127.0.0.1:18082";

export function coldList() {
  const res = rpc(COLD, "tools/list", {}, { tags: { op: "list_cold", who: "guest" } });
  const good = check(res, {
    "cold list 200": (r) => r.status === 200,
    "cold list has the floor": () => toolNames(res).includes("sample-public_echo"),
  });
  listCold.add(res.timings.duration);
  errors.add(!good);
}

export function coldListNegotiate() {
  const res = rpc(COLD_NEGOTIATE, "tools/list", {}, { tags: { op: "list_cold", who: "guest" } });
  const good = check(res, {
    "cold+negotiate list 200": (r) => r.status === 200,
    "cold+negotiate list has the floor": () => toolNames(res).includes("sample-public_echo"),
  });
  listColdNegotiate.add(res.timings.duration);
  errors.add(!good);
}
