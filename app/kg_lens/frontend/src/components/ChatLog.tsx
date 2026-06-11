import { useEffect, useRef, useState } from "react";

// One query/reply pair. reply === null while the parse-query request is
// in flight (the backend runs an LLM manager — several seconds).
export interface ChatExchange {
  id: number;
  query: string;
  reply: string | null;
  isError: boolean;
}

interface Props {
  exchanges: ChatExchange[];
}

function ChevronIcon({ collapsed }: { collapsed: boolean }) {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={collapsed ? "" : "rotate-180"}
    >
      <polyline points="6 9 12 15 18 9" />
    </svg>
  );
}

// Animated "thinking…" indicator shown while a request is in flight.
function ThinkingDots() {
  return (
    <span className="inline-flex items-baseline gap-1 text-gray-400">
      thinking
      <span className="inline-flex gap-0.5">
        <span className="h-1 w-1 rounded-full bg-gray-400 animate-bounce" />
        <span className="h-1 w-1 rounded-full bg-gray-400 animate-bounce [animation-delay:150ms]" />
        <span className="h-1 w-1 rounded-full bg-gray-400 animate-bounce [animation-delay:300ms]" />
      </span>
    </span>
  );
}

function ReplyText({ exchange }: { exchange: ChatExchange }) {
  if (exchange.reply === null) return <ThinkingDots />;
  return (
    <span className={exchange.isError ? "text-red-600" : "text-gray-600"}>
      {exchange.reply}
    </span>
  );
}

// Compact chat log above the ChatInput: each submitted query plus the
// agent's reply (the parse-query `message`). Collapsible; collapsed shows
// just the latest agent reply line.
export function ChatLog({ exchanges }: Props) {
  const [collapsed, setCollapsed] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Auto-scroll to the newest row whenever the log changes or reopens.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [exchanges, collapsed]);

  if (exchanges.length === 0) return null;
  const latest = exchanges[exchanges.length - 1];

  return (
    <div className="absolute bottom-20 left-4 z-10 w-80 bg-white/90 border border-gray-200 rounded-lg shadow-sm text-xs">
      <button
        type="button"
        onClick={() => setCollapsed((c) => !c)}
        className="flex w-full items-center justify-between px-3 py-1.5 text-gray-500 hover:text-gray-900"
        aria-label={collapsed ? "expand chat log" : "collapse chat log"}
      >
        <span className="font-medium">chat</span>
        <ChevronIcon collapsed={collapsed} />
      </button>
      {collapsed ? (
        <div className="px-3 pb-2 truncate">
          <ReplyText exchange={latest} />
        </div>
      ) : (
        <div
          ref={scrollRef}
          className="max-h-[200px] overflow-y-auto px-3 pb-2 flex flex-col gap-1.5"
        >
          {exchanges.map((ex) => (
            <div key={ex.id} className="flex flex-col gap-1">
              <div className="self-end max-w-[90%] bg-gray-100 text-gray-800 rounded-md px-2 py-1">
                {ex.query}
              </div>
              <div className="self-start max-w-[90%]">
                <ReplyText exchange={ex} />
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
