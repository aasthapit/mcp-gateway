import { ToolsetForm } from "../forms/ToolsetForm";
import { useApp } from "../context";
import { effective } from "../glob";
import type { Toolset } from "../types";
import { CollectionPage } from "./CollectionPage";

export function ToolsetsPage({
  selected,
  select,
}: {
  selected: string | null;
  select(id: string | null): void;
}) {
  const { catalog, catalogError } = useApp();
  const names = catalog.map((t) => t.namespaced_name);
  return (
    <>
      {catalogError && (
        <div className="warning">
          catalog unavailable ({catalogError}): the tool picker is empty
        </div>
      )}
      <CollectionPage<Toolset>
        collection="toolsets"
        title="Toolsets"
        selected={selected}
        select={select}
        columns={[
          { header: "Description", cell: (t) => t.description },
          {
            header: "Allows",
            cell: (t) => `${effective(t.allow, t.deny ?? [], names).length} tools`,
          },
        ]}
        editor={({ key, ...props }) => <ToolsetForm key={key} catalog={catalog} {...props} />}
      />
    </>
  );
}
