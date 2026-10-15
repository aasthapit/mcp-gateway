import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ToolsetForm } from "../src/forms/ToolsetForm";
import type { CatalogTool, Toolset } from "../src/types";

const catalog: CatalogTool[] = [
  {
    namespaced_name: "sample-public_time_now",
    upstream: "sample-public",
    name: "time_now",
    description: "time",
  },
  {
    namespaced_name: "sample-public_echo",
    upstream: "sample-public",
    name: "echo",
    description: "echo",
  },
  {
    namespaced_name: "sample-private_secret_lookup",
    upstream: "sample-private",
    name: "secret_lookup",
  },
];

describe("ToolsetForm", () => {
  it("shows a live 'matches N catalog tools' hint for a glob and per entry", async () => {
    const user = userEvent.setup();
    render(<ToolsetForm initial={null} catalog={catalog} onSave={vi.fn()} />);
    const input = screen.getByLabelText("Allow pattern");
    await user.type(input, "sample-public_*");
    expect(screen.getByTestId("draft-hint")).toHaveTextContent("matches 2 catalog tools");
    await user.keyboard("{Enter}");
    expect(screen.getByTestId("matches-sample-public_*")).toHaveTextContent(
      "matches 2 catalog tools",
    );
    expect(screen.getByTestId("effective")).toHaveTextContent("Allows 2 of 3 catalog tools");
  });

  it("picks tools from the catalog, applies deny and saves the document", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn(async (doc: Toolset) => ({
      op: "create" as const,
      version: 42,
      document: doc,
    }));
    render(<ToolsetForm initial={null} catalog={catalog} onSave={onSave} />);
    await user.type(screen.getByLabelText("Id"), "eng");
    await user.type(screen.getByLabelText("Allow pattern"), "sample-*");
    await user.keyboard("{Enter}");
    await user.type(screen.getByLabelText("Deny pattern"), "secret");
    await user.click(screen.getByRole("button", { name: /sample-private_secret_lookup/ }));
    expect(screen.getByTestId("effective")).toHaveTextContent("Allows 2 of 3");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(onSave).toHaveBeenCalledWith({
      _id: "eng",
      enabled: true,
      allow: ["sample-*"],
      deny: ["sample-private_secret_lookup"],
    });
    expect(await screen.findByTestId("meta-version")).toHaveTextContent("42");
  });

  it("refuses characters the schema does not allow and shows API validation errors", async () => {
    const user = userEvent.setup();
    const { ApiError } = await import("../src/api");
    const onSave = vi.fn(async () => {
      throw new ApiError(400, {
        error: "invalid toolsets document",
        details: [{ path: "/allow/0", message: "bad" }],
      });
    });
    const initial: Toolset = { _id: "t", enabled: true, allow: ["x"], deny: [] };
    render(<ToolsetForm initial={initial} catalog={catalog} onSave={onSave} />);
    await user.type(screen.getByLabelText("Allow pattern"), "has space");
    expect(screen.getByText(/only letters, digits/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("/allow/0 bad");
  });
});
