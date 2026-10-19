import type { ReactNode } from "react";

export type NavIcon = "desk" | "monitor" | "scanner" | "risk" | "journal" | "backtest";

export interface NavItem {
  id: string;
  label: string;
  hint?: string;
  icon?: NavIcon;
  /** "view" swaps the page; "drawer" opens a pop-up over the desk (shown with a different active style). */
  kind: "view" | "drawer";
  active: boolean;
  onClick: () => void;
  /** Amber count for work that is WAITING ON YOU (pending approvals). Shown whenever > 0. */
  alert?: number;
  /** Red dot: something here has stopped trading (breaker paused / tripped). */
  danger?: boolean;
  /** Green dot: an automation that can open trades by itself is running here (Hybrid auto-pilot). */
  live?: boolean;
}

/**
 * The app's ONE navigation, living in the header bar.
 *
 * Two kinds of destination, deliberately styled apart:
 *  - VIEWS (Desk, Journal, Backtest) replace the page — underlined when active, like any page tab.
 *  - DRAWERS (Scanner, Risk) slide over the desk without leaving it — highlighted as "open" while up,
 *    and a second click closes them again.
 *
 * The alert badge is the part that earns its keep: a view you are not looking at can still tell you
 * it needs you, so "pending approval" no longer depends on you scrolling past it.
 */
export function NavTabs({ items }: { items: NavItem[] }) {
  return (
    <nav className="flex shrink-0 items-center gap-0.5" aria-label="Sections">
      {items.map((t) => {
        const viewOn = t.kind === "view" && t.active;
        const drawerOn = t.kind === "drawer" && t.active;
        return (
          <button
            key={t.id}
            onClick={t.onClick}
            title={t.hint}
            aria-current={viewOn ? "page" : undefined}
            aria-expanded={t.kind === "drawer" ? t.active : undefined}
            className={`relative flex shrink-0 items-center gap-1.5 rounded-md px-2 py-1.5 text-sm font-medium transition 2xl:px-2.5
              focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/50
              ${viewOn ? "text-white" : drawerOn ? "bg-brand-500/15 text-white ring-1 ring-inset ring-brand-500/50" : "text-neutral-400 hover:bg-neutral-800/70 hover:text-neutral-100"}`}
          >
            {t.icon && <Icon name={t.icon} />}
            <span>{t.label}</span>
            {t.alert ? (
              <span
                className="rounded bg-warn px-1.5 py-px text-[11px] font-bold leading-4 tabular-nums text-black"
                title={`${t.alert} waiting on you`}
              >
                {t.alert}
              </span>
            ) : null}
            {t.live && !t.danger && (
              <span className="relative flex h-2 w-2" aria-label="running">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-bull/60" />
                <span className="relative inline-flex h-2 w-2 rounded-full bg-bull" />
              </span>
            )}
            {t.danger && <span className="h-2 w-2 rounded-full bg-bear ring-2 ring-bear/30" aria-label="needs attention" />}
            {/* The page marker: a short accent under the label of the view you are on. */}
            <span
              className={`absolute inset-x-2.5 bottom-0 h-0.5 rounded-full transition ${viewOn ? "bg-brand-500" : "bg-transparent"}`}
              aria-hidden="true"
            />
          </button>
        );
      })}
    </nav>
  );
}

// Small stroke icons, drawn inline so the offline desktop build ships no icon font.
function Icon({ name }: { name: NavIcon }) {
  const paths: Record<NavIcon, ReactNode> = {
    desk: (
      <>
        <path d="M4 5v14M4 9h2v6H4" />
        <path d="M10 3v18M9 7h2v8H9z" />
        <path d="M16 6v12M15 9h2v5h-2z" />
        <path d="M21 20H3" />
      </>
    ),
    // A grid of rows with a trend arrow - many pairs, one look.
    monitor: (
      <>
        <rect x="3" y="4" width="18" height="16" rx="2" />
        <path d="M3 9.5h18M3 15h18M9 4v16" />
        <path d="M13 13l2-2 2 1.5 2-2.5" />
      </>
    ),
    scanner: (
      <>
        <circle cx="11" cy="11" r="6.5" />
        <path d="M20.5 20.5 16 16" />
        <path d="M8 12.5l2-2.5 2 1.5 2.5-3" />
      </>
    ),
    risk: <path d="M12 3l7 3v5.5c0 4.5-3 7.8-7 9.5-4-1.7-7-5-7-9.5V6l7-3z" />,
    journal: (
      <>
        <path d="M5 4h11a2 2 0 0 1 2 2v14H7a2 2 0 0 1-2-2V4z" />
        <path d="M5 18a2 2 0 0 1 2-2h11" />
        <path d="M9 8h5" />
      </>
    ),
    backtest: (
      <>
        <path d="M3 12a9 9 0 1 0 3-6.7" />
        <path d="M3 4v4h4" />
        <path d="M12 8v4l3 2" />
      </>
    ),
  };
  return (
    <svg
      viewBox="0 0 24 24"
      className="h-4 w-4 shrink-0"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {paths[name]}
    </svg>
  );
}
