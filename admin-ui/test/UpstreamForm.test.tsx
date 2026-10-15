import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { toDocument, UpstreamForm } from "../src/forms/UpstreamForm";
import type { Upstream } from "../src/types";

describe("UpstreamForm", () => {
  it("builds an http upstream with a secretRef name, tags and timeouts", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn(async (doc: Upstream) => ({
      op: "create" as const,
      version: 3,
      document: doc,
    }));
    render(<UpstreamForm initial={null} onSave={onSave} />);
    await user.type(screen.getByLabelText(/^Id/), "github");
    await user.type(screen.getByLabelText("Display name"), "GitHub");
    await user.type(screen.getByLabelText("URL"), "http://gh:8080/mcp");
    await user.selectOptions(screen.getByLabelText("Type"), "static_bearer");
    expect(screen.getByText(/never the secret/)).toBeInTheDocument();
    await user.type(screen.getByLabelText("Secret name (secretRef)"), "upstream-github");
    await user.type(screen.getByLabelText("Tags (comma-separated)"), "external, scm");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(onSave).toHaveBeenCalledWith({
      _id: "github",
      displayName: "GitHub",
      enabled: true,
      transport: "http",
      url: "http://gh:8080/mcp",
      auth: { type: "static_bearer", secretRef: "upstream-github" },
      sessionMode: "per_request",
      protocolMode: "auto",
      timeoutsMs: { connect: 2000, call: 60000 },
      tags: ["external", "scm"],
    });
    expect(await screen.findByTestId("meta-version")).toHaveTextContent("3");
  });

  it("switching to stdio asks for a command and drops the url", async () => {
    const user = userEvent.setup();
    render(<UpstreamForm initial={null} onSave={vi.fn()} />);
    await user.selectOptions(screen.getByLabelText("Transport"), "stdio");
    expect(screen.queryByLabelText("URL")).not.toBeInTheDocument();
    expect(screen.getByLabelText(/Command/)).toBeInTheDocument();
  });

  it("toDocument drops fields that do not apply", () => {
    const doc = toDocument({
      _id: "x",
      displayName: "X",
      enabled: true,
      transport: "stdio",
      url: "http://ignored",
      command: "srv",
      args: [],
      auth: { type: "none", secretRef: "stale", headerName: "X" },
      sessionMode: "per_request",
      timeoutsMs: {},
      tags: [],
    });
    expect(doc).toEqual({
      _id: "x",
      displayName: "X",
      enabled: true,
      transport: "stdio",
      command: "srv",
      auth: { type: "none" },
      sessionMode: "per_request",
    });
  });
});
