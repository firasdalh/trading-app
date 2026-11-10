// Celebration sounds, made once with the user's ElevenLabs account (backend/scripts/
// make_celebration_sounds.py) and served from public/sounds/celebrate with a manifest. A winning close
// plays one clip of its profit level at random - never the one that level played last, so it isn't the
// same every time. Without clips (not generated yet, or a load error) the caller plays its own notes -
// for a win, the jingle of its show (winJingle below).

import { play, type Note } from "./hooks/useApprovalChime";

/** The celebration shows (WinCelebration.tsx); each has a little jingle of its own for when no clip plays. */
export type Jingle = "sparkle" | "coins" | "confetti" | "fireworks";

const JINGLES: Record<Jingle, { notes: Note[]; peak: number }> = {
  // A quick high twinkle.
  sparkle: { peak: 0.1, notes: [
    { hz: 1318.5, at: 0, len: 0.07 }, { hz: 1567.98, at: 0.05, len: 0.07 },
    { hz: 1975.53, at: 0.1, len: 0.07 }, { hz: 2637.02, at: 0.15, len: 0.22 },
  ] },
  // Three coin pickups, each a step higher.
  coins: { peak: 0.11, notes: [
    { hz: 987.77, at: 0, len: 0.06 }, { hz: 1318.5, at: 0.06, len: 0.16 },
    { hz: 1046.5, at: 0.2, len: 0.06 }, { hz: 1396.91, at: 0.26, len: 0.16 },
    { hz: 1174.66, at: 0.4, len: 0.06 }, { hz: 1567.98, at: 0.46, len: 0.3 },
  ] },
  // The C-major fanfare the target card always had.
  confetti: { peak: 0.14, notes: [
    { hz: 523.25, at: 0, len: 0.12 }, { hz: 659.25, at: 0.1, len: 0.12 },
    { hz: 783.99, at: 0.2, len: 0.12 }, { hz: 1046.5, at: 0.3, len: 0.4 },
  ] },
  // A rising whistle, then a bright chord.
  fireworks: { peak: 0.08, notes: [
    { hz: 392, at: 0, len: 0.07 }, { hz: 493.88, at: 0.06, len: 0.07 }, { hz: 587.33, at: 0.12, len: 0.07 },
    { hz: 783.99, at: 0.18, len: 0.08 }, { hz: 1046.5, at: 0.36, len: 0.55 }, { hz: 1318.5, at: 0.36, len: 0.55 },
    { hz: 1567.98, at: 0.36, len: 0.55 },
  ] },
};
// The ticket and the win card both see a close on the charted pair: one jingle, not two.
const JINGLE_GAP_MS = 2500;
let lastJingleAt = 0;

/** A win's jingle, at most once per 2.5 s across the app. Returns whether it played. */
export function winJingle(kind: Jingle): boolean {
  if (Date.now() - lastJingleAt < JINGLE_GAP_MS) return false;
  lastJingleAt = Date.now();
  play(JINGLES[kind].notes, JINGLES[kind].peak);
  return true;
}

type Tier = { key: string; min: number; files: string[] };
type Manifest = { tiers: Tier[] };

const BASE = "/sounds/celebrate/";
const DEDUPE_MS = 3000;   // a target hit is seen by the ticket AND the confetti card: one clip, not two

let manifest: Promise<Manifest | null> | null = null;
const lastFile: Record<string, string> = {};
let lastAt = 0;

function load(): Promise<Manifest | null> {
  manifest ??= fetch(`${BASE}manifest.json`, { cache: "no-cache" })
    .then((r) => (r.ok ? (r.json() as Promise<Manifest>) : null))
    .catch(() => null);
  return manifest;
}

/**
 * Celebrate a winning close of `pnl` dollars. Resolves true when it was handled (a clip played, or one
 * just did for the same win), false when the caller should play its own sound instead. A target hit
 * celebrates at least one level up from its amount.
 */
export async function playCelebration(pnl: number, opts: { target?: boolean; volume?: number } = {}): Promise<boolean> {
  if (!(pnl > 0)) return false;
  if (Date.now() - lastAt < DEDUPE_MS) return true;
  const m = await load();
  const tiers = (m?.tiers ?? []).filter((t) => t.files?.length).sort((a, b) => a.min - b.min);
  if (!tiers.length) return false;
  let i = tiers.reduce((at, t, j) => (pnl >= t.min ? j : at), 0);
  if (opts.target) i = Math.min(tiers.length - 1, i + 1);
  const tier = tiers[i];
  const pool = tier.files.length > 1 ? tier.files.filter((f) => f !== lastFile[tier.key]) : tier.files;
  const file = pool[Math.floor(Math.random() * pool.length)];
  lastAt = Date.now();
  try {
    const audio = new Audio(BASE + file);
    audio.volume = opts.volume ?? 0.7;
    await audio.play();
    lastFile[tier.key] = file;
    return true;
  } catch {
    lastAt = 0;           // blocked or missing: let the caller's own sound play
    return false;
  }
}
