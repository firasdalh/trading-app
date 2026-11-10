/**
 * The move between the desk and full screen, animated (asked 2026-11-08: "not direct open" - the trader should
 * feel the chart go from one mode into the other). The browser's own switch is instant, so the animation runs on
 * top of it, from `fullscreenchange`, before the new layout's first frame is painted:
 *
 * - IN: the chart block opens out of its card's rectangle until it fills the screen. The opening is a growing
 *   clip, not a scale: the block is laid out full size from the first frame, so no text is ever stretched.
 * - OUT (the button or Esc): a panel the size of the screen shrinks into the card, then fades, so the eye
 *   lands on the chart where it now sits.
 *
 * Skipped when the system asks for reduced motion.
 */

export interface MorphFrom {
  rect: DOMRect;   // the card, in the desk's viewport, the moment full screen was asked for
  vw: number;      // that viewport's size
  vh: number;
}

const IN_MS = 480;
const OUT_MS = 540;
// Fast out of the start, soft landing - the card "lifts" into the screen rather than snapping.
const EASE_IN = "cubic-bezier(0.2, 0, 0, 1)";
const EASE_OUT = "cubic-bezier(0.3, 0, 0, 1)";

let opening: Animation | null = null;
let ghost: HTMLDivElement | null = null;

const reduced = () => !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

function dropGhost() {
  ghost?.remove();
  ghost = null;
}

/** Where the card is now - call it just before asking for full screen. */
export function rememberFrom(card: HTMLElement): MorphFrom {
  return { rect: card.getBoundingClientRect(), vw: window.innerWidth, vh: window.innerHeight };
}

/** Just entered full screen: open `el` (the full-screen element) out of the card's old rectangle. */
export function growIn(el: HTMLElement, from: MorphFrom | null): void {
  dropGhost();
  if (!from || reduced() || typeof el.animate !== "function") return;
  const W = window.innerWidth;
  const H = window.innerHeight;
  // Where the desk's viewport sat on the screen: under the browser's bars, above the taskbar - for a maximized
  // window, the usual case. Anywhere else the start is only near the card, which a half-second opening hides.
  const taskbar = screen.height > 0 ? Math.max(0, (screen.height - screen.availHeight) * (H / screen.height)) : 0;
  const top0 = Math.max(0, H - from.vh - taskbar);
  const left0 = Math.max(0, (W - from.vw) / 2);
  const r = from.rect;
  const t = Math.max(0, top0 + r.top);
  const l = Math.max(0, left0 + r.left);
  const right = Math.max(0, W - l - r.width);
  const bottom = Math.max(0, H - t - r.height);
  opening?.cancel();
  opening = el.animate(
    [
      { clipPath: `inset(${t}px ${right}px ${bottom}px ${l}px round 12px)`, opacity: 0.55 },
      { clipPath: "inset(0px 0px 0px 0px round 0px)", opacity: 1 },
    ],
    { duration: IN_MS, easing: EASE_IN },
  );
  opening.onfinish = () => { opening = null; };
}

/** Just left full screen: a screen-sized panel, dressed like `card`, shrinks into it and fades. */
export function shrinkBack(card: HTMLElement): void {
  // An opening still running would go on clipping the block with full-screen coordinates, inside the card.
  opening?.cancel();
  opening = null;
  dropGhost();
  if (reduced() || typeof card.animate !== "function") return;
  const r = card.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) return;
  const W = window.innerWidth;
  const H = window.innerHeight;
  const cs = getComputedStyle(card);
  const g = document.createElement("div");
  g.setAttribute("aria-hidden", "true");
  Object.assign(g.style, {
    position: "fixed", left: "0px", top: "0px", width: `${W}px`, height: `${H}px`, zIndex: "2147483000",
    pointerEvents: "none", boxSizing: "border-box", background: "#0a0a0a",
    border: `1px solid ${cs.borderTopColor || "rgba(38,38,38,0.8)"}`,
    // The card's ring and glow (scalp mode's amber) - the panel lands looking like the card it becomes.
    boxShadow: cs.boxShadow && cs.boxShadow !== "none" ? cs.boxShadow : "0 0 0 1px rgba(64,64,64,0.6)",
  });
  document.body.appendChild(g);
  ghost = g;
  const radius = cs.borderTopLeftRadius || "12px";
  const at = { left: `${r.left}px`, top: `${r.top}px`, width: `${r.width}px`, height: `${r.height}px`, borderRadius: radius };
  const a = g.animate(
    [
      { left: "0px", top: "0px", width: `${W}px`, height: `${H}px`, borderRadius: "0px", opacity: 1 },
      { ...at, opacity: 1, offset: 0.7 },
      { ...at, opacity: 0 },
    ],
    { duration: OUT_MS, easing: EASE_OUT, fill: "forwards" },
  );
  const done = () => {
    if (ghost === g) ghost = null;
    g.remove();
  };
  a.onfinish = done;
  a.oncancel = done;
  window.setTimeout(done, OUT_MS + 800);   // never leave a panel over the desk, whatever happens to the animation
}
