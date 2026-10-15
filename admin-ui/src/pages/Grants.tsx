import { useEffect, useState } from "react";
import { useApp } from "../context";
import { GrantForm } from "../forms/GrantForm";
import type { Grant, Subject, Toolset } from "../types";
import { CollectionPage } from "./CollectionPage";

export function subjectLabel(s: Subject): string {
  switch (s.kind) {
    case "anonymous":
      return "everyone";
    case "authenticated":
      return "any valid token";
    case "client_id":
      return `client ${s.value}`;
    case "claim":
      return s.op === "exists"
        ? `${s.path} exists`
        : `${s.path} ${s.op} ${JSON.stringify(s.value)}`;
  }
}

export function GrantsPage({
  selected,
  select,
}: {
  selected: string | null;
  select(id: string | null): void;
}) {
  const { api } = useApp();
  const [toolsets, setToolsets] = useState<Toolset[]>([]);
  useEffect(() => {
    api
      .list<Toolset>("toolsets")
      .then((r) => setToolsets(r.items))
      .catch(() => setToolsets([]));
  }, [api]);
  return (
    <CollectionPage<Grant>
      collection="grants"
      title="Grants"
      selected={selected}
      select={select}
      columns={[
        { header: "Subject", cell: (g) => subjectLabel(g.subject) },
        { header: "Toolsets", cell: (g) => g.toolsets.join(", ") },
        { header: "Priority", cell: (g) => g.priority ?? "" },
      ]}
      editor={({ key, ...props }) => <GrantForm key={key} toolsets={toolsets} {...props} />}
    />
  );
}
