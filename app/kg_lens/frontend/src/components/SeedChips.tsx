import { LensNode } from "../types";

interface Props {
  seeds: string[];
  seedNodeMap: Map<string, LensNode>;
  onRemove: (id: string) => void;
}

export function SeedChips({ seeds, seedNodeMap, onRemove }: Props) {
  if (seeds.length === 0) return null;
  return (
    <div className="absolute top-4 left-4 z-10 flex flex-wrap gap-2 max-w-[60%]">
      {seeds.map((id) => {
        const node = seedNodeMap.get(id);
        const label = node?.label ?? id.slice(0, 8);
        return (
          <button
            key={id}
            onClick={() => onRemove(id)}
            className="bg-accent text-accent-fg text-xs px-2.5 py-1 rounded-full hover:bg-blue-700 transition flex items-center gap-1.5 shadow-sm"
            title="remove from focus"
          >
            <span className="font-medium">{label}</span>
            <span className="opacity-70">×</span>
          </button>
        );
      })}
    </div>
  );
}
