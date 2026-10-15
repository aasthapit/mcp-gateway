import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { AuthProvider, createUserManager } from "./auth";
import "./styles.css";
import type { RuntimeConfig } from "./types";

async function start() {
  const root = createRoot(document.getElementById("root") as HTMLElement);
  try {
    const res = await fetch("/config.json", { cache: "no-store" });
    if (!res.ok) throw new Error(`/config.json: HTTP ${res.status}`);
    const config = (await res.json()) as RuntimeConfig;
    const manager = createUserManager(config);
    root.render(
      <StrictMode>
        <AuthProvider manager={manager}>
          <App config={config} />
        </AuthProvider>
      </StrictMode>,
    );
  } catch (err) {
    root.render(<div className="center error">failed to start: {(err as Error).message}</div>);
  }
}

void start();
