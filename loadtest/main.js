// `make loadtest` entry point: all scenarios in parallel against the local stack.
//   warm gateway (GATEWAY_URL, default :18080): production config, upstream lists cached
//   cold gateway (GATEWAY_COLD_URL, default :18081): upstream list cache off
// Rates are per scenario (constant arrival rate, so latency does not throttle the load).
// SLO thresholds (docs/perf.md "SLOs") make k6 exit non-zero when they are missed.
export { guestMix } from "./guest.js";
export { dev1Mix } from "./dev1.js";
export { listenHolder } from "./listen.js";
export { coldList } from "./cold.js";

const DURATION = __ENV.LOADTEST_DURATION || "60s";
const SCALE = Number(__ENV.LOADTEST_SCALE || 1);

function rate(name, perSec, maxVUs) {
  return {
    executor: "constant-arrival-rate",
    exec: name,
    rate: Math.max(1, Math.round(perSec * SCALE)),
    timeUnit: "1s",
    duration: DURATION,
    preAllocatedVUs: Math.ceil(maxVUs / 2),
    maxVUs,
  };
}

export const options = {
  scenarios: {
    guest: rate("guestMix", 40, 60),
    dev1: rate("dev1Mix", 10, 30),
    cold: rate("coldList", 5, 10),
    listeners: {
      executor: "constant-vus",
      exec: "listenHolder",
      vus: Number(__ENV.LISTEN_VUS || 50),
      duration: DURATION,
      gracefulStop: "30s",
    },
  },
  thresholds: {
    mcp_list_cached: ["p(95)<150"],
    mcp_list_dev1: ["p(95)<150"],
    mcp_list_cold: ["p(95)<600"],
    mcp_call_guest: ["p(95)<300"],
    mcp_call_dev1: ["p(95)<300"],
    mcp_errors: ["rate<0.01"],
    // HTTP 5xx anywhere but on the listen streams (those end by client timeout).
    "http_req_failed{op:list}": ["rate<0.01"],
    "http_req_failed{op:call}": ["rate<0.01"],
    "http_req_failed{op:list_cold}": ["rate<0.01"],
  },
  summaryTrendStats: ["min", "med", "p(90)", "p(95)", "p(99)", "max", "count"],
  discardResponseBodies: false,
};

const TRENDS = [
  ["tools/list cached (guest)", "mcp_list_cached"],
  ["tools/list cached (dev1)", "mcp_list_dev1"],
  ["tools/list cold (guest)", "mcp_list_cold"],
  ["tools/call guest (sample-public_echo)", "mcp_call_guest"],
  ["tools/call dev1 (sample-private_secret_lookup)", "mcp_call_dev1"],
];

function fmt(v) {
  return v === undefined ? "-" : v.toFixed(1);
}

export function handleSummary(data) {
  const rows = TRENDS.filter(([, m]) => data.metrics[m]).map(([label, m]) => {
    const v = data.metrics[m].values;
    return `| ${label} | ${v.count} | ${fmt(v.med)} | ${fmt(v["p(95)"])} | ${fmt(v["p(99)"])} | ${fmt(v.max)} |`;
  });
  const errors = data.metrics.mcp_errors ? data.metrics.mcp_errors.values.rate : 0;
  const failed = Object.entries(data.metrics)
    .filter(([, m]) => m.thresholds && Object.values(m.thresholds).some((t) => !t.ok))
    .map(([name]) => name);
  const md = [
    "| Operation | Requests | p50 ms | p95 ms | p99 ms | max ms |",
    "| --- | ---: | ---: | ---: | ---: | ---: |",
    ...rows,
    "",
    `MCP-level error rate (list/call): ${(errors * 100).toFixed(2)} %. ` +
      `Thresholds: ${failed.length ? "FAILED " + failed.join(", ") : "all passed"}.`,
  ].join("\n");
  const out = __ENV.LOADTEST_OUT || "results";
  return {
    [`${out}/summary.json`]: JSON.stringify(data, null, 2),
    [`${out}/summary.md`]: md + "\n",
    stdout: "\n" + md + "\n",
  };
}
