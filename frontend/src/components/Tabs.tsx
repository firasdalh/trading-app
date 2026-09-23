import type { ReactNode } from "react";

export interface TabDef<T extends string> {
  id: T;
  label: string;
  /** Neutral count shown beside the label (open positions, watched pairs…). Hidden when 0/undefined. */
  count?: number;
  /** Amber count for work that is WAITING ON YOU. Always shown when > 0, even next to a count. */
  alert?: number;
  hint?: string;
}

interface Props<T extends string> {
  tabs: TabDef<T>[];
  active: T;
  onChange: (id: T) => void;
  /** Rendered at the right end of the bar (status text, a global action…). */
  trailing?: ReactNode;
}

/**
 * The desk's ONE primary navigation.
 *
 * The dashboard used to be a single scroll of fourteen equal-weight cards, which meant the two
 * things a desk must never hide — an open position and a decision waiting on you — sat below three
 * screens of scanners. Splitting the same panels across tabs costs nothing (they are unchanged) and
 * buys a real hierarchy: one place to work, one place to look for candidates, one place for the
 * book, one for configuration.
 *
 * The alert badge is the part that earns its keep: a tab you are not looking at can still tell you
 * it needs you, so "pending approval" no longer depends on you scrolling past it.
 */
export function Tabs<T extends string>({ tabs, active, onChange, trailing }: Props<T>) {
  return (
    <div className="border-b border-neutral-800 bg-neutral-950/95 backdrop-blur">
      <div className="mx-auto flex max-w-7xl items-center justify-between gap-3 px-4">
        <nav className="flex items-center gap-0.5 overflow-x-auto" role="tablist" aria-label="Sections">
          {tabs.map((t) => {
            const on = t.id === active;
            return (
              <button
                key={t.id}
                role="tab"
                aria-selected={on}
                title={t.hint}
                onClick={() => onChange(t.id)}
                className={`relative flex shrink-0 items-center gap-1.5 px-3 py-2.5 text-sm font-medium transition
                  ${on ? "text-neutral-100" : "text-neutral-500 hover:text-neutral-200"}`}
              >
                {t.label}
                {t.count ? (
                  <span className="rounded bg-neutral-800 px-1.5 py-px text-[11px] tabular-nums text-neutral-300">
                    {t.count}
                  </span>
                ) : null}
                {t.alert ? (
                  <span
                    className="rounded bg-warn px-1.5 py-px text-[11px] font-bold tabular-nums text-black"
                    title={`${t.alert} waiting on you`}
                  >
                    {t.alert}
                  </span>
                ) : null}
                {/* The active marker sits on the container's bottom border, so the bar reads as one
                    surface rather than a row of buttons. */}
                <span
                  className={`absolute inset-x-1 -bottom-px h-0.5 rounded-full transition ${
                    on ? "bg-brand-500" : "bg-transparent"
                  }`}
                />
              </button>
            );
          })}
        </nav>
        {trailing ? <div className="shrink-0">{trailing}</div> : null}
      </div>
    </div>
  );
}
