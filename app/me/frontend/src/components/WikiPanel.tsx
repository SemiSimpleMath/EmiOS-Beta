import { useQuery } from "@tanstack/react-query";
import { fetchNodeDetail } from "../api";
import { LensNode } from "../types";

interface Props {
  node: LensNode;
  onClose: () => void;
  onAddToFocus: (id: string) => void;
}

export function WikiPanel({ node, onClose, onAddToFocus }: Props) {
  const { data: detail } = useQuery({
    queryKey: ["node-detail", node.id],
    queryFn: () => fetchNodeDetail(node.id),
  });

  return (
    <aside
      className="absolute top-0 right-0 h-full w-full md:w-[40%] bg-white shadow-2xl border-l border-gray-200 z-20 flex flex-col"
      role="dialog"
      aria-label={`Details for ${node.label}`}
    >
      <header className="flex items-center justify-between px-5 py-4 border-b border-gray-200">
        <div className="min-w-0">
          <h2 className="text-lg font-semibold truncate">{node.label}</h2>
          <p className="text-xs text-gray-500 uppercase tracking-wide">
            {node.node_type}
            {node.category ? ` · ${node.category}` : ""}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => onAddToFocus(node.id)}
            className="text-xs px-2.5 py-1 rounded border border-gray-300 hover:border-gray-400 hover:bg-gray-50"
          >
            Add to focus
          </button>
          <button
            onClick={onClose}
            className="text-gray-500 hover:text-gray-900 text-xl leading-none"
            aria-label="close"
          >
            ×
          </button>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto p-5">
        {detail?.description && (
          <section className="mb-4">
            <h3 className="text-xs uppercase tracking-wide text-gray-500 mb-1">
              About
            </h3>
            <p className="text-sm text-gray-800 leading-relaxed">
              {detail.description}
            </p>
          </section>
        )}

        {detail && (detail.start_date || detail.end_date) && (
          <section className="mb-4">
            <h3 className="text-xs uppercase tracking-wide text-gray-500 mb-1">
              When
            </h3>
            <p className="text-sm text-gray-800">
              {detail.start_date ?? "?"} → {detail.end_date ?? "ongoing"}
            </p>
          </section>
        )}

        {detail && detail.aliases.length > 0 && (
          <section className="mb-4">
            <h3 className="text-xs uppercase tracking-wide text-gray-500 mb-1">
              Also known as
            </h3>
            <p className="text-sm text-gray-800">{detail.aliases.join(", ")}</p>
          </section>
        )}

        {detail?.wiki_url && (
          <section className="mt-6">
            <a
              href={detail.wiki_url}
              target="_blank"
              rel="noreferrer"
              className="inline-block text-sm px-4 py-2 rounded bg-accent text-accent-fg hover:bg-blue-700"
            >
              Open full wiki page →
            </a>
          </section>
        )}
      </div>
    </aside>
  );
}
