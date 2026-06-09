import { useEffect, useId, useMemo, useRef, useState } from "react";

interface Props {
  value: string;
  symbols: string[];
  descriptions?: Record<string, string>; // symbol -> human-readable name
  favorites: string[]; // favorited symbols for the current asset class
  onChange: (symbol: string) => void;
  onToggleFavorite: (symbol: string) => void;
}

// Searchable symbol combobox with per-symbol favourites (★). Falls back to free entry so a
// symbol the broker offers but isn't in the list can still be typed.
export function SymbolPicker({
  value,
  symbols,
  descriptions = {},
  favorites,
  onChange,
  onToggleFavorite,
}: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const boxRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const listId = useId();

  // Close when clicking outside.
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  useEffect(() => {
    if (open) {
      setQuery("");
      // focus the search box once the dropdown mounts
      setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [open]);

  const favSet = useMemo(() => new Set(favorites), [favorites]);
  const q = query.trim().toUpperCase();
  // Match the ticker OR its description (so "apple" finds AAPLm, "oil" finds USOILm).
  const matches = useMemo(
    () =>
      symbols.filter(
        (s) => s.toUpperCase().includes(q) || (descriptions[s] ?? "").toUpperCase().includes(q),
      ),
    [symbols, q, descriptions],
  );
  const favMatches = matches.filter((s) => favSet.has(s));
  const otherMatches = matches.filter((s) => !favSet.has(s));

  const pick = (s: string) => {
    onChange(s);
    setOpen(false);
  };

  const onEnter = () => {
    if (matches.length) pick(matches[0]);
    else if (q) pick(q); // free entry
  };

  const Row = ({ s }: { s: string }) => (
    <div
      onClick={() => pick(s)}
      className={`flex cursor-pointer items-center gap-2 px-2 py-1.5 hover:bg-neutral-700 ${
        s === value ? "bg-neutral-700/60" : ""
      }`}
    >
      <button
        onClick={(e) => {
          e.stopPropagation();
          onToggleFavorite(s);
        }}
        title={favSet.has(s) ? "Remove from favourites" : "Add to favourites"}
        className={`text-sm leading-none ${favSet.has(s) ? "text-warn" : "text-neutral-600 hover:text-neutral-300"}`}
      >
        {favSet.has(s) ? "★" : "☆"}
      </button>
      <span className="text-sm font-medium">{s}</span>
      {descriptions[s] && (
        <span className="truncate text-xs text-neutral-500" title={descriptions[s]}>
          {descriptions[s]}
        </span>
      )}
    </div>
  );

  return (
    <div ref={boxRef} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        title={descriptions[value] || value}
        className="flex w-48 items-center justify-between rounded bg-neutral-800 px-2 py-1.5 text-left text-sm"
      >
        <span className="flex min-w-0 items-baseline gap-1.5">
          <span className="font-medium">{value || "Select…"}</span>
          {descriptions[value] && (
            <span className="truncate text-xs text-neutral-500">{descriptions[value]}</span>
          )}
        </span>
        <span className="ml-2 text-neutral-500">▾</span>
      </button>

      {open && (
        <div className="absolute z-20 mt-1 w-64 rounded border border-neutral-700 bg-neutral-900 shadow-lg">
          <div className="border-b border-neutral-800 p-2">
            <input
              ref={inputRef}
              name="symbol-search"
              autoComplete="off"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") onEnter();
                if (e.key === "Escape") setOpen(false);
              }}
              placeholder="Search symbol…"
              className="w-full rounded bg-neutral-800 px-2 py-1 text-sm uppercase"
            />
          </div>
          <div id={listId} className="max-h-72 overflow-auto py-1">
            {favMatches.length > 0 && (
              <>
                <div className="px-2 py-1 text-[10px] uppercase tracking-wide text-neutral-500">
                  Favourites
                </div>
                {favMatches.map((s) => (
                  <Row key={`f-${s}`} s={s} />
                ))}
                {otherMatches.length > 0 && <div className="my-1 border-t border-neutral-800" />}
              </>
            )}
            {otherMatches.map((s) => (
              <Row key={s} s={s} />
            ))}
            {matches.length === 0 && (
              <div className="px-2 py-2 text-xs text-neutral-500">
                {q ? `No match. Press Enter to use “${q}”.` : "No symbols."}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
