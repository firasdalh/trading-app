import { useEffect, useId, useRef, useState, type ReactNode } from "react";

/** The pop-ups that slide over the desk without leaving it. */
export type DrawerId = "scanner" | "risk" | "advisor";
export const DRAWER_IDS: DrawerId[] = ["scanner", "risk", "advisor"];

interface Props {
  open: boolean;
  title: string;
  /** One line under the title saying what this pop-up is for. */
  subtitle?: ReactNode;
  onClose: () => void;
  /** Extra controls in the drawer header, left of the close button. */
  actions?: ReactNode;
  /** Tailwind width class for the panel. Wide enough for the scanner grids and the positions table. */
  widthClass?: string;
  children: ReactNode;
}

/**
 * Right-side pop-up panel for everything you CONSULT rather than work in: the scanners, the risk
 * settings, the position advisor.
 *
 * Why a drawer and not a page or a tab: these used to be tabs, and every glance at one cost you the
 * chart and the pair you were studying. A drawer slides OVER the desk, so the desk stays mounted —
 * chart, analysis and polling are untouched — and closing it (Esc, ✕, or a click on the dimmed desk)
 * puts you back exactly where you were.
 *
 * It opens BELOW the sticky header on purpose (`--hdr-h`, measured by App): the header carries the
 * account numbers and the kill-switch, and neither should ever be hidden behind a pop-up.
 *
 * Rendered only while open, so a closed drawer's panels do not poll in the background — the same
 * behaviour they had as tabs.
 *
 * On the desk at lg+ it also stops at the sidebar's left edge (`--book-w`, set by Dashboard while
 * the sidebar sits beside the chart): open positions, their P&L and what is waiting for you are the
 * one thing the desk promises to keep in view, and a drawer slid over them hid exactly that. The
 * sidebar stays live and clickable beside it. Off the desk (Journal, Backtest) or on a narrow
 * window the variable is unset and the drawer runs to the window edge as before.
 */
export function Drawer({ open, title, subtitle, onClose, actions, widthClass = "max-w-[48rem]", children }: Props) {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  // Parents pass inline closures; reading through a ref keeps the effect below from re-running (and
  // re-stealing focus from an input inside the drawer) on every parent render.
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  // Slide in on the frame after mount; starting already in place would skip the transition.
  const [shown, setShown] = useState(false);

  useEffect(() => {
    if (!open) {
      setShown(false);
      return;
    }
    const prevFocus = document.activeElement as HTMLElement | null;
    const raf = requestAnimationFrame(() => setShown(true));
    panelRef.current?.focus({ preventScroll: true });
    // An Esc already used by a layer ABOVE the drawer (the Settings modal takes it in the capture
    // phase and marks it handled) is not ours: that press closes the modal only, the next one this.
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      e.preventDefault();
      closeRef.current();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("keydown", onKey);
      // Hand focus back to whatever opened the drawer, so keyboard users are not dropped at the top.
      prevFocus?.focus?.({ preventScroll: true });
    };
  }, [open]);

  if (!open) return null;

  return (
    <div
      // overflow-hidden: the slide-in starts off this box's right edge, so it must not be painted
      // over the sidebar on its way in.
      className="fixed bottom-0 left-0 z-[25] overflow-hidden"
      style={{ top: "var(--hdr-h, 0px)", right: "var(--book-w, 0px)" }}
    >
      {/* The dimmed desk behind is the click-outside target. */}
      <div
        className={`absolute inset-0 bg-black/50 transition-opacity duration-200 ${shown ? "opacity-100" : "opacity-0"}`}
        onClick={() => closeRef.current()}
        aria-hidden="true"
      />
      <div
        ref={panelRef}
        role="dialog"
        // Not aria-modal: the header above stays usable on purpose (kill-switch, nav).
        aria-modal="false"
        aria-labelledby={titleId}
        tabIndex={-1}
        className={`absolute inset-y-0 right-0 flex w-full ${widthClass} flex-col border-x border-neutral-800 bg-neutral-950 shadow-pop outline-none
          transition-transform duration-200 ease-out ${shown ? "translate-x-0" : "translate-x-full"}`}
      >
        <div className="flex shrink-0 items-start gap-3 border-b border-neutral-800 bg-neutral-900/60 px-4 py-3">
          <div className="min-w-0 flex-1">
            <h2 id={titleId} className="text-base font-semibold text-neutral-100">{title}</h2>
            {subtitle && <div className="mt-0.5 text-xs text-neutral-500">{subtitle}</div>}
          </div>
          {actions}
          <button
            onClick={() => closeRef.current()}
            className="btn btn-ghost -mr-1 px-2"
            aria-label={`Close ${title}`}
            title="Close (Esc)"
          >
            ✕
          </button>
        </div>
        {/* overscroll-contain: reaching the end of the drawer must not start scrolling the desk. */}
        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto overscroll-contain p-4">{children}</div>
      </div>
    </div>
  );
}
