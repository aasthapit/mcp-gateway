import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { GrantForm } from "../src/forms/GrantForm";
import type { Grant, Toolset } from "../src/types";

const toolsets: Toolset[] = [
  { _id: "public-basics", enabled: true, allow: [] },
  { _id: "engineering", enabled: true, allow: [] },
];
const saved = (doc: Grant) => ({ op: "update" as const, version: 7, document: doc });

async function setup(initial: Grant | null = null) {
  const user = userEvent.setup();
  const onSave = vi.fn(async (doc: Grant) => saved(doc));
  render(<GrantForm initial={initial} toolsets={toolsets} onSave={onSave} />);
  return { user, onSave };
}

describe("GrantForm subject builder", () => {
  it("builds a claim/contains subject with toolsets", async () => {
    const { user, onSave } = await setup();
    await user.type(screen.getByLabelText("Id"), "developers");
    await user.click(screen.getByRole("radio", { name: "Claim" }));
    await user.clear(screen.getByLabelText("claim value"));
    await user.type(screen.getByLabelText("claim value"), "mcp-developer");
    await user.click(screen.getByRole("checkbox", { name: /engineering/ }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({
        _id: "developers",
        subject: {
          kind: "claim",
          path: "realm_access.roles",
          op: "contains",
          value: "mcp-developer",
        },
        toolsets: ["engineering"],
      }),
    );
    expect(await screen.findByTestId("meta-version")).toHaveTextContent("7");
  });

  it("'in' produces an array, 'exists' drops the value, numbers are typed", async () => {
    const initial: Grant = {
      _id: "g",
      enabled: true,
      subject: { kind: "claim", path: "tier", op: "eq", value: "" },
      toolsets: ["public-basics"],
    };
    const { user, onSave } = await setup(initial);
    await user.selectOptions(screen.getByLabelText("Operator"), "in");
    await user.type(screen.getByLabelText("claim value"), "gold, silver,");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(onSave.mock.lastCall?.[0].subject).toEqual({
      kind: "claim",
      path: "tier",
      op: "in",
      value: ["gold", "silver"],
    });

    await user.selectOptions(screen.getByLabelText("value type"), "number");
    await user.clear(screen.getByLabelText("claim value"));
    await user.type(screen.getByLabelText("claim value"), "1, 2");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(onSave.mock.lastCall?.[0].subject).toMatchObject({ op: "in", value: [1, 2] });

    await user.selectOptions(screen.getByLabelText("Operator"), "exists");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(onSave.mock.lastCall?.[0].subject).toEqual({
      kind: "claim",
      path: "tier",
      op: "exists",
    });
  });

  it("client_id and anonymous subjects", async () => {
    const { user, onSave } = await setup();
    await user.type(screen.getByLabelText("Id"), "ci");
    await user.click(screen.getByRole("radio", { name: "Client id" }));
    await user.type(screen.getByRole("textbox", { name: "Client id" }), "ci-runner");
    await user.click(screen.getByRole("checkbox", { name: /public-basics/ }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(onSave.mock.lastCall?.[0].subject).toEqual({ kind: "client_id", value: "ci-runner" });
    await user.click(screen.getByRole("radio", { name: "Anonymous" }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(onSave.mock.lastCall?.[0].subject).toEqual({ kind: "anonymous" });
  });

  it("cannot save without a toolset", async () => {
    const { user } = await setup();
    await user.type(screen.getByLabelText("Id"), "x");
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });
});
