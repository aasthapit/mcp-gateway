// Cold tools/list: the second gateway runs with upstream list caching off
// (cache_ttl_sec=0), so every list fans out to both upstreams before policy filtering.
import { check } from "k6";
import { Trend } from "k6/metrics";

import { errors } from "./guest.js";
import { rpc, toolNames } from "./lib/mcp.js";

export const listCold = new Trend("mcp_list_cold", true);

const COLD = __ENV.GATEWAY_COLD_URL || "http://127.0.0.1:18081";

export function coldList() {
  const res = rpc(COLD, "tools/list", {}, { tags: { op: "list_cold", who: "guest" } });
  const good = check(res, {
    "cold list 200": (r) => r.status === 200,
    "cold list has the floor": () => toolNames(res).includes("sample-public_echo"),
  });
  listCold.add(res.timings.duration);
  errors.add(!good);
}
