// Celebration sounds, made once with the user's ElevenLabs account (backend/scripts/
// make_celebration_sounds.py) and served from public/sounds/celebrate with a manifest. A winning close
// plays one clip of its profit level at random - never the one that level played last, so it isn't the
// same every time. Without clips (not generated yet, or a load error) the caller plays its own notes.

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
