import { useEffect, useRef } from "react";

// Sound alert for "something needs your approval".
//
// The tone is SYNTHESISED with the Web Audio API rather than loaded from a file. The desktop app is
// served from a local backend with a strict CSP and runs offline, so a bundled asset is one more
// thing to ship, cache and get wrong — two oscillator notes cost nothing and always work.
//
// Deliberately a rising two-note chime (E5 -> A5), short and quiet. This fires while you are doing
// something else; it needs to be noticeable without being an alarm, and pleasant enough that you
// don't reach to disable it after the third trade.

const NOTES = [
  { hz: 659.25, at: 0.0, len: 0.14 },   // E5
  { hz: 880.0, at: 0.13, len: 0.22 },   // A5 — rising, so it reads as "attention", not "error"
];
const PEAK = 0.14;                       // gentle; this plays unprompted

let ctx: AudioContext | null = null;

function play(): void {
  try {
    type WithWebkit = typeof globalThis & { webkitAudioContext?: typeof AudioContext };
    const Ctor = window.AudioContext ?? (globalThis as WithWebkit).webkitAudioContext;
    if (!Ctor) return;
    ctx ??= new Ctor();
    // Browsers suspend audio until the page has been interacted with. By the time a proposal is
    // waiting the user has clicked something, so this normally resolves; if it doesn't, we simply
    // stay silent rather than throwing.
    void ctx.resume().catch(() => {});
    const now = ctx.currentTime;
    for (const n of NOTES) {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = n.hz;
      // Ramp both ends — a square-edged envelope clicks audibly.
      gain.gain.setValueAtTime(0.0001, now + n.at);
      gain.gain.exponentialRampToValueAtTime(PEAK, now + n.at + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + n.at + n.len);
      osc.connect(gain).connect(ctx.destination);
      osc.start(now + n.at);
      osc.stop(now + n.at + n.len + 0.02);
    }
  } catch {
    /* no audio device / blocked — a missing chime must never break the dashboard */
  }
}

/**
 * Chime when a NEW item appears in `ids`.
 *
 * Tracks which ids have already been seen, so it fires once per genuinely new item and never
 * re-fires on a poll that simply returns the same list again.
 *
 * The first load is silent on purpose: opening the dashboard to five pending proposals should not
 * play five chimes for things that were already waiting. Only items that arrive while you are
 * watching are announced.
 */
export function useApprovalChime(ids: number[] | undefined, enabled: boolean): void {
  const seen = useRef<Set<number> | null>(null);

  useEffect(() => {
    if (!ids) return;                       // still loading — nothing known yet
    if (seen.current === null) {            // first real payload: adopt it silently
      seen.current = new Set(ids);
      return;
    }
    const fresh = ids.filter((id) => !seen.current!.has(id));
    // Keep the set to the CURRENT ids so approving and re-receiving an id can chime again, and the
    // set can't grow without bound over a long session.
    seen.current = new Set(ids);
    if (fresh.length && enabled) play();
  }, [ids, enabled]);
}
