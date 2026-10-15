// dev1 (role mcp-developer, bearer from `make token`): tools/list + a private tool call.
import { check } from "k6";
import { Trend } from "k6/metrics";

import { errors } from "./guest.js";
import { ok, rpc, toolNames } from "./lib/mcp.js";

export const listDev1 = new Trend("mcp_list_dev1", true);
export const callDev1 = new Trend("mcp_call_dev1", true);

const BASE = __ENV.GATEWAY_URL || "http://127.0.0.1:18080";
const TOKEN = __ENV.LOADTEST_DEV1_TOKEN;

export function dev1Mix() {
  const list = rpc(BASE, "tools/list", {}, { token: TOKEN, tags: { op: "list", who: "dev1" } });
  const names = toolNames(list);
  let good = check(list, {
    "dev1 list 200": (r) => r.status === 200,
    "dev1 sees the private tool": () => names.includes("sample-private_secret_lookup"),
  });
  listDev1.add(list.timings.duration);
  errors.add(!good);

  const call = rpc(
    BASE,
    "tools/call",
    { name: "sample-private_secret_lookup", arguments: { key: "k6" } },
    { token: TOKEN, tags: { op: "call", who: "dev1" } },
  );
  good = check(call, { "dev1 call ok": (r) => ok(r) });
  callDev1.add(call.timings.duration);
  errors.add(!good);
}
