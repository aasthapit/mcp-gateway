/** "Effective tools for…": computed by the gateway (/internal/effective-tools) via the API. */
import { useState } from "react";
import type { PreviewInput } from "../api";
import { ErrorView } from "../components/ErrorView";
import { useApp } from "../context";
import type { PreviewResult, RuntimeConfig } from "../types";

type Mode = "anonymous" | "claims" | "token";

const CLAIM_TEMPLATES: Record<string, object> = {
  "mcp-developer": { sub: "preview-dev", realm_access: { roles: ["mcp-developer"] } },
  "ci-runner": { sub: "service-account-ci-runner", azp: "ci-runner" },
  "no roles": { sub: "preview-user" },
};

export function PreviewPage({ config }: { config: RuntimeConfig }) {
  const { api } = useApp();
  const [mode, setMode] = useState<Mode>("anonymous");
  const [claimsText, setClaimsText] = useState(
    JSON.stringify(CLAIM_TEMPLATES["mcp-developer"], null, 2),
  );
  const [token, setToken] = useState("");
  const [result, setResult] = useState<PreviewResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      let input: PreviewInput;
      if (mode === "anonymous") input = { kind: "anonymous" };
      else if (mode === "token") input = { token: token.trim() };
      else {
        const claims = JSON.parse(claimsText) as unknown;
        if (!claims || typeof claims !== "object" || Array.isArray(claims))
          throw new Error("claims must be a JSON object");
        input = { claims: claims as Record<string, unknown> };
      }
      setResult(await api.preview(input));
    } catch (err) {
      setResult(null);
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page">
      <h1>Effective tools for…</h1>
      <div className="tabs" role="tablist">
        {(["anonymous", "claims", "token"] as Mode[]).map((m) => (
          <button
            key={m}
            role="tab"
            aria-selected={mode === m}
            className={mode === m ? "tab active" : "tab"}
            onClick={() => {
              setMode(m);
              setResult(null);
              setError(null);
            }}
          >
            {{ anonymous: "Guest (no token)", claims: "Claims", token: "Paste a token" }[m]}
          </button>
        ))}
      </div>
      {mode === "anonymous" && (
        <p className="muted">What an MCP client without an Authorization header sees.</p>
      )}
      {mode === "claims" && (
        <>
          <div className="row">
            <span className="muted">templates:</span>
            {Object.entries(CLAIM_TEMPLATES).map(([name, c]) => (
              <button
                key={name}
                type="button"
                className="link"
                onClick={() => setClaimsText(JSON.stringify(c, null, 2))}
              >
                {name}
              </button>
            ))}
          </div>
          <textarea
            aria-label="claims JSON"
            rows={8}
            value={claimsText}
            onChange={(e) => setClaimsText(e.target.value)}
            spellCheck={false}
          />
        </>
      )}
      {mode === "token" && (
        <>
          <div className="warning">
            The token is decoded <strong>without verification</strong> (no signature, issuer,
            audience or expiry check) only to read its claims for this preview. The gateway verifies
            real tokens.
          </div>
          <textarea
            aria-label="JWT"
            rows={5}
            value={token}
            placeholder="eyJhbGciOi…"
            onChange={(e) => setToken(e.target.value)}
            spellCheck={false}
          />
        </>
      )}
      <div className="actions">
        <button
          className="primary"
          onClick={() => void run()}
          disabled={busy || (mode === "token" && !token.trim())}
        >
          {busy ? "Asking the gateway…" : "Preview"}
        </button>
      </div>
      <ErrorView error={error} />
      {result && (
        <div className="result">
          <p>
            Policy version <strong>{result.policy_version}</strong>
            {result.input.mode === "token" && (
              <span className="badge badge-degraded">unverified token</span>
            )}
          </p>
          <div className="cols">
            <div>
              <h3>Tools ({result.tools.length})</h3>
              <ul data-testid="preview-tools">
                {result.tools.map((t) => (
                  <li key={t}>
                    <code>{t}</code>
                  </li>
                ))}
              </ul>
            </div>
            <div>
              <h3>Matching grants</h3>
              <ul>
                {(result.grants ?? []).map((g) => (
                  <li key={g}>
                    <a href={`#/grants/${encodeURIComponent(g)}`}>
                      <code>{g}</code>
                    </a>
                  </li>
                ))}
              </ul>
              <h3>Principal</h3>
              <pre className="small">{JSON.stringify(result.principal, null, 2)}</pre>
            </div>
          </div>
        </div>
      )}
      <p className="muted">
        Check with a real MCP client: point it at <code>{config.gatewayMcpUrl}</code> (no token =
        guest).
      </p>
    </div>
  );
}
