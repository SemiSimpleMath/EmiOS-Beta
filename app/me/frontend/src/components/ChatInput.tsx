import { useState } from "react";

interface Props {
  onSubmit: (text: string) => void;
}

export function ChatInput({ onSubmit }: Props) {
  const [value, setValue] = useState("");

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!value.trim()) return;
    onSubmit(value);
    setValue("");
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="absolute bottom-0 left-0 right-0 p-4 bg-gradient-to-t from-white via-white to-transparent z-10"
    >
      <div className="mx-auto max-w-2xl">
        <input
          type="text"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="Ask anything… e.g. 'Annika's family' or 'between 2022 and 2024'"
          className="w-full px-4 py-3 text-sm border border-gray-200 rounded-full shadow-sm focus:outline-none focus:ring-2 focus:ring-accent focus:border-transparent bg-white"
          autoFocus
        />
      </div>
    </form>
  );
}
