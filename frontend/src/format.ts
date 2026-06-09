// Shared display formatters so prices, dollars, and labels render consistently everywhere.

// Strip IEEE-754 representation noise (e.g. 51664.00000000001) without hard-coding decimals —
// keeps the right precision for JPY (3dp), indices (1dp), 5-digit FX, and sub-cent crypto.
export function fmtPrice(v: number | null | undefined): string {
  if (v == null) return "—";
  if (!Number.isFinite(v)) return String(v);
  return Number(v.toPrecision(12)).toString();
}

// USD amount, 2 decimals, thousands separators. `sign: true` prepends + for positives.
export function fmtUsd(v: number | null | undefined, opts: { sign?: boolean } = {}): string {
  if (v == null) return "—";
  if (!Number.isFinite(v)) return String(v);
  const s = `$${Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  if (v < 0) return `-${s}`;
  return opts.sign ? `+${s}` : s;
}

// Display a broker symbol consistently. Exness suffixes its instruments with a lowercase "m"
// (EURUSDm, US500m); the watchlist stores them with mixed casing depending on how they were
// added (EURUSDM vs USOILm). Normalize to base-uppercase + lowercase "m" so they all match.
export function displaySymbol(s: string): string {
  const u = (s || "").toUpperCase();
  return u.endsWith("M") ? `${u.slice(0, -1)}m` : u;
}

// Human label for an asset-class value. Capitalized; "index" -> "Indices".
const ASSET_LABELS: Record<string, string> = { index: "Indices" };
export function assetLabel(a: string): string {
  return ASSET_LABELS[a] ?? a.charAt(0).toUpperCase() + a.slice(1);
}
