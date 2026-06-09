import { useEffect, useState } from "react";
import { api } from "../api/client";
import { fmtPrice, fmtUsd } from "../format";
import type { AnalyzeResponse, TimeframeRead, TradeEconomics, TradeProposal } from "../types";

const TF_RANK: Record<string, number> = { "1m": 1, "5m": 2, "15m": 3, "30m": 4, "1h": 5, "4h": 6, "1d": 7 };

function trendTone(trend?: string): string {
  if (trend === "up") return "text-bull";
  if (trend === "down") return "text-bear";
  return "text-neutral-300";
}

// Extract just the LLM reviewer's note from a rationale (the chips cover the rest).
function reviewNote(rationale: string): string {
  const i = rationale.indexOf("AI review");
  return i >= 0 ? rationale.slice(i) : "";
}

interface Props {
  result: AnalyzeResponse | null;
  status: string | null;
  // Whether the executed setup is still open at the broker (live truth). null = unknown/loading.
  // The proposal's stored status stays "executed" forever, so this is what tells the panel the
  // position has since closed.
  positionOpen?: boolean | null;
  busy: boolean;
  equity?: number | null;
  onApprove: (lots?: number | null) => void;
  onReject: () => void;
}

// Shows the current proposal: direction, levels, confidence, the risk-adjusted size, the
// risk-manager verdict, the cost/leverage + an adjustable (2%-capped) size, and each agent's
// reasoning (expandable). Approve/Reject in Mode A.
export function ProposalPanel({ result, status, positionOpen, busy, equity, onApprove, onReject }: Props) {
  const proposalId = result?.proposal_id ?? null;
  const actionable = !!result && result.proposal.direction !== "no_trade";
  const pending = status === "pending_approval";

  const [lots, setLots] = useState<string>("");
  const [econ, setEcon] = useState<TradeEconomics | null>(null);
  const [capped, setCapped] = useState(false);
  const [maxLots, setMaxLots] = useState<number | null>(null);
  const [previewBusy, setPreviewBusy] = useState(false);

  // Load the AI's default size + cost/leverage whenever the proposal changes.
  useEffect(() => {
    if (!proposalId || !actionable) {
      setEcon(null);
      setLots("");
      return;
    }
    let cancelled = false;
    setPreviewBusy(true);
    api
      .sizePreview(proposalId, null)
      .then((r) => {
        if (cancelled) return;
        setEcon(r.economics);
        setCapped(r.capped);
        setMaxLots(r.max_lots);
        if (r.economics?.lots != null) setLots(String(r.economics.lots));
      })
      .catch(() => {})
      .finally(() => !cancelled && setPreviewBusy(false));
    return () => {
      cancelled = true;
    };
  }, [proposalId, actionable]);

  // Re-price at a user-entered lot size (the backend clamps to the 2% per-trade ceiling).
  const reprice = async (val: string) => {
    if (!proposalId) return;
    const n = Number(val);
    setPreviewBusy(true);
    try {
      const r = await api.sizePreview(proposalId, Number.isFinite(n) && n > 0 ? n : null);
      setEcon(r.economics);
      setCapped(r.capped);
      setMaxLots(r.max_lots);
      if (r.economics?.lots != null) setLots(String(r.economics.lots));
    } catch {
      /* ignore */
    } finally {
      setPreviewBusy(false);
    }
  };

  if (!result) {
    return (
      <div className="card text-sm text-neutral-400">
        No proposal yet. Pick a symbol and run analysis.
      </div>
    );
  }

  const { proposal, risk } = result;
  const noTrade = proposal.direction === "no_trade";
  const approveLots = lots ? Number(lots) : null;

  return (
    <div className="card space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-base font-semibold">{proposal.symbol}</span>
          {noTrade && proposal.watch ? (
            <span className="rounded bg-warn/20 px-2 py-0.5 text-xs font-bold uppercase text-warn">
              watching
            </span>
          ) : (
            <DirectionBadge direction={proposal.direction} />
          )}
          <span className="text-xs text-neutral-400">{proposal.timeframe}</span>
        </div>
        <div className="flex items-center gap-2">
          <ReviewBadge decision={proposal.review_decision} />
          <StatusBadge status={status} positionOpen={positionOpen} />
        </div>
      </div>

      {!noTrade && (
        <div className="grid grid-cols-3 gap-2 text-sm">
          <Stat label="Entry" value={fmtPrice(proposal.entry)} />
          <Stat label="Stop" value={fmtPrice(proposal.stop_loss)} valueClass="text-bear" />
          <Stat label="Target" value={fmtPrice(proposal.take_profit)} valueClass="text-bull" />
        </div>
      )}

      <div className="flex items-center gap-3 text-sm">
        <span className="text-neutral-400">Confidence</span>
        <div className="h-2 flex-1 rounded bg-neutral-800">
          <div
            className="h-2 rounded bg-blue-500"
            style={{ width: `${Math.round(proposal.confidence * 100)}%` }}
          />
        </div>
        <span className="tabular-nums">{Math.round(proposal.confidence * 100)}%</span>
      </div>

      {/* Risk Manager verdict — deterministic, final. */}
      <div
        className={`rounded-md border p-2 text-sm ${
          risk.approved ? "border-bull/40 bg-bull/10" : "border-bear/40 bg-bear/10"
        }`}
      >
        <div className="font-medium">
          Risk Manager: {risk.decision.toUpperCase()}
          {risk.approved && (
            <span className="ml-2 text-neutral-300">
              size {risk.approved_qty} · risk ${risk.risk_amount} (
              {(
                (risk.risk_pct_of_equity > 0
                  ? risk.risk_pct_of_equity
                  : equity
                    ? risk.risk_amount / equity
                    : 0) * 100
              ).toFixed(2)}
              % of equity)
            </span>
          )}
        </div>
        <div className="text-neutral-400">{risk.reason}</div>
      </div>

      {/* Cost, leverage, and an adjustable (2%-capped) size — what you'll spend before approving. */}
      {!noTrade && (
        <div className="rounded-md border border-neutral-800 p-3 text-sm">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-xs font-semibold uppercase tracking-wide text-neutral-400">
              Cost &amp; size {previewBusy && <span className="text-neutral-500">· …</span>}
            </span>
            {capped && (
              <span
                className="rounded bg-warn/20 px-2 py-0.5 text-xs font-medium text-warn"
                title="Your size was reduced to fit the 2% per-trade risk cap (RISK.md) or the exposure budget."
              >
                capped at 2%
              </span>
            )}
          </div>

          <div className="grid grid-cols-3 gap-2">
            <Stat label="Spend (margin)" value={fmtUsd(econ?.margin_usd)} />
            <Stat label="Leverage" value={econ?.leverage != null ? `${econ.leverage.toFixed(0)}×` : "—"} />
            <Stat
              label="Exposure"
              value={
                econ?.notional_usd != null
                  ? `$${econ.notional_usd.toLocaleString(undefined, { maximumFractionDigits: 0 })}`
                  : "—"
              }
            />
          </div>

          {pending && (
            <div className="mt-3 flex items-end gap-3">
              <label className="text-xs text-neutral-400">
                <div className="mb-1">Size (lots)</div>
                <input
                  name="proposal-lots"
                  autoComplete="off"
                  value={lots}
                  onChange={(e) => setLots(e.target.value)}
                  onBlur={(e) => reprice(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && reprice((e.target as HTMLInputElement).value)}
                  inputMode="decimal"
                  className="w-24 rounded bg-neutral-800 px-2 py-1.5 text-sm tabular-nums text-neutral-100"
                />
              </label>
              {maxLots != null && (
                <button
                  type="button"
                  onClick={() => reprice(String(maxLots))}
                  className="mb-0.5 text-xs text-neutral-400 hover:text-neutral-200"
                  title="Set the maximum size allowed by the 2% per-trade risk cap"
                >
                  max {maxLots} lots (2% cap)
                </button>
              )}
            </div>
          )}
          {econ?.margin_usd == null && (
            <div className="mt-2 text-xs text-neutral-500">
              Cost/leverage are only computed for MT5 (Exness) positions.
            </div>
          )}
        </div>
      )}

      {!noTrade && <SetupSignals proposal={proposal} />}

      {/* For no-trade, the rationale is the sit-out reason. For an actionable setup the chips
          above already cover it, so only surface the LLM reviewer's note if present. */}
      {noTrade ? (
        <p className="text-sm text-neutral-300">{proposal.rationale}</p>
      ) : (
        reviewNote(proposal.rationale) && (
          <p className="text-xs leading-relaxed text-neutral-400">{reviewNote(proposal.rationale)}</p>
        )
      )}

      <Reasoning result={result} />

      {pending ? (
        <div className="flex gap-2">
          <button
            onClick={() => onApprove(approveLots)}
            disabled={busy || previewBusy}
            className="btn flex-1 bg-bull text-white hover:bg-green-700"
          >
            Approve
          </button>
          <button
            onClick={onReject}
            disabled={busy}
            className="btn flex-1 bg-neutral-700 text-white hover:bg-neutral-600"
          >
            Reject
          </button>
        </div>
      ) : (
        <div className="text-xs text-neutral-500">
          {noTrade
            ? proposal.watch
              ? "Watching — setup forming; waiting for the trigger. Nothing to approve yet."
              : "Orchestrator declined — nothing to approve."
            : status === "executed"
              ? positionOpen === false
                ? "Position closed — no longer open at the broker. Run analysis for a fresh setup."
                : "Position open — manage it in the Positions table below."
              : status === "approved"
                ? "Approved — awaiting execution."
                : status === "rejected"
                  ? "Rejected."
                  : "No action available in this state."}
        </div>
      )}
    </div>
  );
}

function SetupSignals({ proposal }: { proposal: TradeProposal }) {
  const tech = proposal.technical;
  if (!tech || !tech.timeframes.length) return null;

  const entryTf = tech.timeframes[0];
  const ind = entryTf?.indicators ?? {};
  const macro = [...tech.timeframes].sort(
    (a, b) => (TF_RANK[b.timeframe] ?? 0) - (TF_RANK[a.timeframe] ?? 0),
  )[0];

  const adx = ind["adx"];
  const macdH = ind["macd_hist"];
  const rsi = ind["rsi14"];
  const e = proposal.entry;
  const s = proposal.stop_loss;
  const t = proposal.take_profit;
  const rr = e != null && s != null && t != null && e !== s ? Math.abs(t - e) / Math.abs(e - s) : null;

  const adxLabel =
    adx == null ? "—" : `${adx.toFixed(0)} ${adx >= 25 ? "(strong)" : adx < 20 ? "(weak)" : "(moderate)"}`;
  const macdLabel =
    macdH == null ? "—" : `${macdH > 0 ? "bullish" : "bearish"} (${macdH.toFixed(3)})`;

  const rows: { label: string; value: string; tone?: string }[] = [
    { label: "Entry-TF trend", value: (entryTf?.trend ?? "—").toUpperCase(), tone: trendTone(entryTf?.trend) },
    { label: `Higher-TF (${macro?.timeframe ?? "—"})`, value: (macro?.trend ?? "—").toUpperCase(), tone: trendTone(macro?.trend) },
    { label: "Trend strength (ADX)", value: adxLabel },
    { label: "Momentum (MACD)", value: macdLabel, tone: macdH == null ? undefined : macdH > 0 ? "text-bull" : "text-bear" },
    { label: "RSI (14)", value: rsi == null ? "—" : rsi.toFixed(1) },
    { label: "Fundamental bias", value: (proposal.fundamental?.bias ?? "—").toUpperCase() },
    { label: "Reward : Risk", value: rr == null ? "—" : `${rr.toFixed(2)} : 1`, tone: rr != null && rr >= 2 ? "text-bull" : undefined },
  ];

  return (
    <div className="rounded-md border border-neutral-800 p-3">
      <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-neutral-400">
        Why this setup
      </div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-sm">
        {rows.map((r) => (
          <div key={r.label} className="flex items-center justify-between gap-2">
            <span className="text-neutral-400">{r.label}</span>
            <span className={`tabular-nums ${r.tone ?? "text-neutral-200"}`}>{r.value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function Reasoning({ result }: { result: AnalyzeResponse }) {
  const [open, setOpen] = useState(false);
  const tech = result.proposal.technical;
  const fund = result.proposal.fundamental;
  return (
    <div className="rounded-md border border-neutral-800">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between px-3 py-2 text-sm text-neutral-300"
      >
        <span>Agent reasoning</span>
        <span>{open ? "▲" : "▼"}</span>
      </button>
      {open && (
        <div className="space-y-4 border-t border-neutral-800 p-3 text-xs">
          {tech && (
            <div className="space-y-3">
              <div className="font-semibold text-neutral-200">
                Technical · overall trend{" "}
                <span className={trendTone(tech.overall_trend)}>{tech.overall_trend.toUpperCase()}</span>{" "}
                · conf {Math.round(tech.confidence * 100)}%
              </div>
              {tech.timeframes.map((tf) => (
                <TimeframeBlock key={tf.timeframe} tf={tf} />
              ))}
            </div>
          )}
          {fund && (
            <div className="space-y-1">
              <div className="font-semibold text-neutral-200">
                Fundamental · bias{" "}
                <span className={biasTone(fund.bias)}>{fund.bias.toUpperCase()}</span> · conf{" "}
                {Math.round(fund.confidence * 100)}%
              </div>
              {fund.surprise_assessment && (
                <div className="text-neutral-400">{fund.surprise_assessment}</div>
              )}
              {fund.key_drivers.length > 0 && (
                <ul className="ml-4 list-disc text-neutral-400">
                  {fund.key_drivers.slice(0, 4).map((d, i) => (
                    <li key={i}>{d}</li>
                  ))}
                </ul>
              )}
              {fund.stand_aside_windows.length > 0 && (
                <div className="text-warn">
                  Stand aside around:{" "}
                  {fund.stand_aside_windows
                    .map((w) => `${w.label} (${fmtWindow(w.start, w.end)})`)
                    .join(", ")}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function nf(v: number | undefined): string {
  return v == null ? "—" : v.toLocaleString(undefined, { maximumFractionDigits: 5 });
}

// Format a stand-aside window with its DATE (relative day when close) + a "passed" marker, so
// it's clear whether the event is still ahead. Times/dates render in the user's local zone.
function fmtWindow(startIso: string, endIso: string): string {
  const start = new Date(startIso);
  const end = new Date(endIso);
  const now = new Date();
  const time = (d: Date) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  const startDay = new Date(start.getFullYear(), start.getMonth(), start.getDate());
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const dayDiff = Math.round((startDay.getTime() - today.getTime()) / 86_400_000);
  const day =
    dayDiff === 0 ? "Today"
    : dayDiff === 1 ? "Tomorrow"
    : dayDiff === -1 ? "Yesterday"
    : start.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });

  const passed = end.getTime() < now.getTime() ? " · passed" : "";
  return `${day} ${time(start)}–${time(end)}${passed}`;
}

function TimeframeBlock({ tf }: { tf: TimeframeRead }) {
  const i = tf.indicators;
  const groups: { title: string; items: [string, number | undefined][] }[] = [
    { title: "Trend", items: [["EMA20", i["ema20"]], ["EMA50", i["ema50"]], ["EMA200", i["ema200"]], ["Last", i["last_close"]]] },
    { title: "Momentum", items: [["RSI", i["rsi14"]], ["MACD", i["macd"]], ["Signal", i["macd_signal"]], ["Hist", i["macd_hist"]]] },
    { title: "Strength", items: [["ADX", i["adx"]], ["+DI", i["plus_di"]], ["−DI", i["minus_di"]]] },
    { title: "Volatility", items: [["ATR", i["atr14"]], ["BB↑", i["bb_upper"]], ["BB·", i["bb_mid"]], ["BB↓", i["bb_lower"]], ["Width", i["bb_width"]]] },
    { title: "Context", items: [["Vol×avg", i["vol_ratio"]], ["Support", tf.support_levels[0]], ["Resist", tf.resistance_levels[0]]] },
  ];
  return (
    <div className="rounded border border-neutral-800/70 p-2">
      <div className="mb-1.5 flex items-center gap-2">
        <span className="rounded bg-neutral-800 px-1.5 py-0.5 font-medium text-neutral-200">{tf.timeframe}</span>
        <span className={`font-medium ${trendTone(tf.trend)}`}>{tf.trend.toUpperCase()}</span>
      </div>
      <div className="space-y-1">
        {groups.map((g) => (
          <div key={g.title} className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
            <span className="w-16 shrink-0 text-neutral-500">{g.title}</span>
            {g.items.map(([label, val]) => (
              <span key={label} className="text-neutral-400">
                {label} <span className="tabular-nums text-neutral-200">{nf(val)}</span>
              </span>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

function biasTone(bias: string): string {
  if (bias === "bullish") return "text-bull";
  if (bias === "bearish") return "text-bear";
  return "text-neutral-300";
}

function DirectionBadge({ direction }: { direction: string }) {
  const map: Record<string, string> = {
    long: "bg-bull text-white",
    short: "bg-bear text-white",
    no_trade: "bg-neutral-700 text-neutral-200",
  };
  return (
    <span className={`rounded px-2 py-0.5 text-xs font-bold uppercase ${map[direction] ?? ""}`}>
      {direction.replace("_", " ")}
    </span>
  );
}

function ReviewBadge({ decision }: { decision: string | null }) {
  if (!decision) return null;
  const confirmed = decision === "confirm";
  return (
    <span
      className={`rounded px-2 py-0.5 text-xs font-medium ${
        confirmed ? "bg-bull/20 text-bull" : "bg-bear/20 text-bear"
      }`}
      title="LLM reviewer's verdict on the deterministic setup (it can only confirm or veto)"
    >
      AI review: {confirmed ? "confirmed" : "vetoed"}
    </span>
  );
}

function StatusBadge({ status, positionOpen }: { status: string | null; positionOpen?: boolean | null }) {
  if (!status) return null;
  // An executed proposal whose position has since closed should read "closed", not "executed".
  const label = status === "executed" && positionOpen === false ? "closed" : status;
  return (
    <span className="rounded bg-neutral-800 px-2 py-0.5 text-xs text-neutral-300">{label}</span>
  );
}

function Stat({ label, value, valueClass }: { label: string; value: string; valueClass?: string }) {
  return (
    <div className="rounded bg-neutral-800/60 p-2">
      <div className="text-xs text-neutral-400">{label}</div>
      <div className={`tabular-nums ${valueClass ?? ""}`}>{value}</div>
    </div>
  );
}

