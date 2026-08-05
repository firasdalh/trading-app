import { useEffect, useState } from "react";
import { api } from "../api/client";
import { fmtPrice, fmtUsd } from "../format";
import { ArmSetupButton } from "./ArmSetupButton";
import { ago, localTime } from "./advisorFormat";
import { RegimeBadge } from "./RegimeBadge";
import { AiDecisionCard } from "./AiDecisionCard";
import { ReviewExplanation } from "./ReviewExplanation";
import { ScenarioCard } from "./ScenarioCard";
import type {
  AiScenarioRead,
  AnalyzeResponse,
  ConditionalSetupView,
  PositionAdvice,
  PositionView,
  TimeframeRead,
  TradeEconomics,
  TradeProposal,
} from "../types";

const TF_RANK: Record<string, number> = { "1m": 1, "5m": 2, "15m": 3, "30m": 4, "1h": 5, "4h": 6, "1d": 7 };

function trendTone(trend?: string): string {
  if (trend === "up") return "text-bull";
  if (trend === "down") return "text-bear";
  return "text-neutral-300";
}

// The IMMEDIATE higher timeframe loaded above the entry TF (15m→1h, 1h→4h, 4h→1d) — the one the
// engine's trend gate now compares against. Falls back to the highest loaded TF if none is above.
function immediateHigher(tfs: TimeframeRead[], entryTf: string): TimeframeRead | undefined {
  const er = TF_RANK[entryTf] ?? 0;
  const above = tfs
    .filter((t) => (TF_RANK[t.timeframe] ?? 0) > er)
    .sort((a, b) => (TF_RANK[a.timeframe] ?? 0) - (TF_RANK[b.timeframe] ?? 0));
  return above[0] ?? [...tfs].sort((a, b) => (TF_RANK[b.timeframe] ?? 0) - (TF_RANK[a.timeframe] ?? 0))[0];
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
  // The live position on this pair, when there is one. Lets the panel compare the CURRENT read
  // against the trade you're actually in ("does the engine still agree with me?").
  openPosition?: PositionView | null;
  armedSetup?: ConditionalSetupView | null;   // an armed 'wait for the break' setup for this symbol
  busy: boolean;
  equity?: number | null;
  onApprove: (lots?: number | null) => void;
  onReject: () => void;
  onRunAnalysis?: () => void;   // re-run analysis for the charted symbol (same as the top button)
  analyzing?: boolean;
  scenario?: AiScenarioRead | null;   // Step 4: AI two-scenario read (info only)
  scenarioBusy?: boolean;
  onLoadScenarios?: () => void;        // opt-in fetch (info-only read costs AI tokens, so it's on demand)
  // Toggle the AI scenario's cited S/R lines on the chart (shared with the chart's own scenario card).
  scenLevelsShown?: boolean;
  onToggleScenLevels?: (levels: { support: number | null; resistance: number | null; target: number | null; invalidation: number | null } | null) => void;
}

// Shows the current proposal: direction, levels, confidence, the risk-adjusted size, the
// risk-manager verdict, the cost/leverage + an adjustable (3%-capped) size, and each agent's
// reasoning (expandable). Approve/Reject in Mode A.
export function ProposalPanel({ result, status, positionOpen, openPosition, armedSetup, busy, equity, onApprove, onReject, onRunAnalysis, analyzing, scenario, scenarioBusy, onLoadScenarios, scenLevelsShown, onToggleScenLevels }: Props) {
  const proposalId = result?.proposal_id ?? null;
  const actionable = !!result && result.proposal.direction !== "no_trade";
  const pending = status === "pending_approval";

  const [lots, setLots] = useState<string>("");
  const [econ, setEcon] = useState<TradeEconomics | null>(null);
  const [riskUsd, setRiskUsd] = useState<number | null>(null);
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
        setRiskUsd(r.risk?.risk_amount ?? null);
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

  // Re-price at a user-entered lot size (the backend clamps to the 3% per-trade ceiling).
  const reprice = async (val: string) => {
    if (!proposalId) return;
    const n = Number(val);
    setPreviewBusy(true);
    try {
      const r = await api.sizePreview(proposalId, Number.isFinite(n) && n > 0 ? n : null);
      setEcon(r.economics);
      setRiskUsd(r.risk?.risk_amount ?? null);
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
      <div className="card space-y-2 text-sm text-neutral-400">
        <div>No proposal yet. Pick a symbol and run analysis.</div>
        {onRunAnalysis && (
          <button
            onClick={onRunAnalysis}
            disabled={analyzing}
            className="btn btn-primary"
          >
            {analyzing ? "Analyzing…" : "Run analysis"}
          </button>
        )}
      </div>
    );
  }

  const { proposal, risk } = result;
  const noTrade = proposal.direction === "no_trade";
  // Structured AI decision (when 🤖 AI decides). For an ARM the market proposal is NO_TRADE by design
  // (it's a pending order), so the raw "VETOED / NO_TRADE / 0%" is misleading — we reframe below.
  const ai = proposal.ai_decision ?? null;
  const aiArm = ai?.kind === "arm";
  const aiNonOpen = !!ai && ai.kind !== "open";
  const approveLots = lots ? Number(lots) : null;
  // Potential reward in $ (reward scales with risk by the same lot×point factor -> reward = risk×R).
  const rrMult =
    proposal.entry != null && proposal.stop_loss != null && proposal.take_profit != null
      && proposal.entry !== proposal.stop_loss
      ? Math.abs(proposal.take_profit - proposal.entry) / Math.abs(proposal.entry - proposal.stop_loss)
      : null;
  const rewardUsd = riskUsd != null && rrMult != null ? riskUsd * rrMult : null;

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
          {result?.analyzed_at && (
            <AnalysisAge iso={result.analyzed_at} timeframe={proposal.timeframe} />
          )}
          {result?.market_open === false && (
            <span className="rounded bg-warn/20 px-1.5 py-0.5 text-[10px] font-bold uppercase text-warn"
                  title="This market's session is CLOSED (weekend / holiday / out-of-hours) — the prices are the last close, not live. The auto-traders stand down until it reopens.">
              ⏸ Market closed
            </span>
          )}
          {proposal.regime && <RegimeBadge regime={proposal.regime} strategy={proposal.strategy} />}
          <AlignmentBadge alignment={proposal.alignment} />
        </div>
        <div className="flex items-center gap-2">
          <ReviewBadge decision={proposal.review_decision} />
          <StatusBadge status={status} positionOpen={positionOpen} standAside={noTrade && !proposal.watch} />
          {onRunAnalysis && (
            <button
              onClick={onRunAnalysis}
              disabled={analyzing}
              className="btn btn-primary"
              title="Re-run analysis for the charted symbol"
            >
              {analyzing ? "Analyzing…" : "Run analysis"}
            </button>
          )}
        </div>
      </div>

      {/* FOLLOW-UP: you're already in this pair — does the latest read still back the trade? */}
      {openPosition && (
        <FollowUp
          pos={openPosition}
          proposal={proposal}
          analyzedAt={result?.analyzed_at ?? null}
          onRecheck={onRunAnalysis}
          analyzing={analyzing}
        />
      )}

      {/* Already-armed context: this analysis is a FRESH new-trade check, so a "no trade" here does
          NOT contradict a setup that's already armed on this pair. */}
      {armedSetup && (
        <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-sm">
          <div className="font-medium text-amber-300">
            ⏳ Already armed — {armedSetup.direction.toUpperCase()} {armedSetup.order_type.replace("_", "-")}
            {" @ "}{fmtPrice(armedSetup.trigger_price)}
            {armedSetup.rr != null && <span className="text-neutral-400"> · ~{armedSetup.rr}R</span>}
          </div>
          <div className="text-neutral-400">
            A “wait for the break” {armedSetup.direction} is already pending on this pair. The analysis
            below is a FRESH <em>new-trade</em> check from the current price — standing aside here is
            expected and does NOT cancel the armed order (it fires on its own trigger).
          </div>
        </div>
      )}

      {!noTrade && (
        <div className="grid grid-cols-3 gap-2 text-sm">
          <Stat label="Entry" value={fmtPrice(proposal.entry)} />
          <Stat label="Stop" value={fmtPrice(proposal.stop_loss)} valueClass="text-bear" />
          <Stat label="Target" value={fmtPrice(proposal.take_profit)} valueClass="text-bull" />
        </div>
      )}

      {!aiNonOpen && !noTrade && (() => {
        const pct = Math.round(proposal.confidence * 100);
        const color = pct >= 70 ? "bg-bull" : pct >= 50 ? "bg-warn" : "bg-bear";
        const txt = pct >= 70 ? "text-bull" : pct >= 50 ? "text-warn" : "text-bear";
        return (
          <div className="flex items-center gap-3 text-sm">
            <span className="text-neutral-400">Confidence</span>
            <div className="relative h-2 flex-1 rounded bg-neutral-800">
              <div className={`h-2 rounded ${color}`} style={{ width: `${pct}%` }} />
              {/* 70% marker — the Hybrid auto-open threshold, so you can see if it clears the bar. */}
              <div
                className="absolute top-[-2px] h-3 w-px bg-neutral-500"
                style={{ left: "70%" }}
                title="70% — Hybrid auto-open threshold"
              />
            </div>
            <span className={`tabular-nums font-semibold ${txt}`}>{pct}%</span>
          </div>
        );
      })()}

      {/* Risk Manager verdict — deterministic, final. When the AI DECIDED not to open a market trade
          (stand aside / arm / blocked), there's nothing to size, so we DON'T show the raw red
          "Risk Manager: VETOED — orchestrator returned NO_TRADE" (which reads like the risk manager
          overrode the AI). We reframe it: the AI made the call, this isn't a risk veto. */}
      {aiNonOpen ? (
        <div
          className={`rounded-md border p-2 text-sm ${
            aiArm ? "border-amber-500/40 bg-amber-500/10" : "border-neutral-700 bg-neutral-800/40"
          }`}
        >
          <div className={`font-medium ${aiArm ? "text-amber-300" : "text-neutral-300"}`}>
            {aiArm ? "Pending order — not a rejection" : "AI's call — not a risk veto"}
          </div>
          <div className="text-neutral-400">
            {aiArm
              ? "Nothing opens now. The Risk Manager sizes + approves it automatically when the trigger hits and the setup still checks out."
              : "The AI decided not to trade, so there's nothing for the Risk Manager to size. This is the AI's call, not a risk-manager rejection."}
          </div>
        </div>
      ) : noTrade ? (
        <StandAsideCard proposal={proposal} />
      ) : (
      <div
        className={`rounded-md border p-2 text-sm ${
          !risk.approved
            ? "border-bear/40 bg-bear/10"
            : risk.min_lot_floored
              ? "border-warn/40 bg-warn/10"
              : "border-bull/40 bg-bull/10"
        }`}
      >
        <div className="font-medium">
          Risk Manager: {risk.decision.toUpperCase()}
          {risk.min_lot_floored && (
            <span className="ml-2 rounded bg-warn/20 px-1.5 py-0.5 text-xs font-bold text-warn">
              ⚠ broker min · over cap
            </span>
          )}
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
      )}

      {/* Structured AI decision — the created scenarios, the chosen one, why, action + levels. */}
      {ai && <AiDecisionCard d={ai} />}

      {/* Cost, leverage, and an adjustable (3%-capped) size — what you'll spend before approving. */}
      {!noTrade && (
        <div className="rounded-md border border-neutral-800 p-3 text-sm">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-xs font-semibold uppercase tracking-wide text-neutral-400">
              Cost &amp; size {previewBusy && <span className="text-neutral-500">· …</span>}
            </span>
            {capped && (
              <span
                className="rounded bg-warn/20 px-2 py-0.5 text-xs font-medium text-warn"
                title="Your size was reduced to fit the 3% per-trade risk cap (RISK.md) or the exposure budget."
              >
                capped at 3%
              </span>
            )}
          </div>

          <div className="grid grid-cols-3 gap-2">
            <Stat label="Risk" value={fmtUsd(riskUsd)} />
            <Stat label="Reward" value={fmtUsd(rewardUsd)} />
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
                  title="Set the maximum size allowed by the 3% per-trade risk cap"
                >
                  max {maxLots} lots (3% cap)
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

      {/* Market read behind the decision — shown for an actionable setup ("Why this setup") AND for a
          stand-aside ("What the analysis saw"), so a no-trade isn't just a bare one-liner. */}
      <SetupSignals proposal={proposal} standAside={noTrade}
        onToggleScenLevels={onToggleScenLevels} scenLevelsShown={scenLevelsShown} />

      {/* For an actionable setup the chips above already cover it, so only surface the LLM reviewer's
          note if present (the no-trade rationale now lives in the StandAsideCard). The structured
          AiDecisionCard (when present) replaces the raw rationale wall entirely. */}
      {!ai && !noTrade && reviewNote(proposal.rationale) && (
        <p className="text-xs leading-relaxed text-neutral-400">{reviewNote(proposal.rationale)}</p>
      )}

      {!ai && <ReviewExplanation rationale={proposal.rationale} />}

      {proposal.conditional && (
        <div className="rounded-md border border-amber-700/40 bg-amber-900/10 p-2">
          <div className="mb-1 text-xs text-amber-300/90">
            Blocked by structure now — but valid on a break. Arm it and the system will re-check and
            open it automatically when the level gives way:
          </div>
          <ArmSetupButton
            symbol={proposal.symbol}
            assetClass={proposal.asset_class}
            timeframe={proposal.timeframe}
            conditional={proposal.conditional}
          />
        </div>
      )}

      <Reasoning result={result} />

      {/* AI two-scenario read — INFO ONLY (the engine's decision never uses it) and it costs AI tokens,
          so it's fetched ON DEMAND via the button, not auto-run on every analysis. */}
      {result && (
        <div className="rounded-md border border-violet-800/40 bg-violet-950/10 p-2">
          {scenario ? (
            <ScenarioCard read={scenario} levelsShown={scenLevelsShown}
              assetClass={proposal.asset_class} timeframe={proposal.timeframe}
              onShowLevels={onToggleScenLevels
                ? () => onToggleScenLevels(scenario
                    ? { support: scenario.nearest_support ?? null, resistance: scenario.nearest_resistance ?? null,
                        target: scenario.target ?? null, invalidation: scenario.invalidation_price ?? null }
                    : null)
                : undefined} />
          ) : scenarioBusy ? (
            <div className="text-xs text-violet-300/80">🤖 Reasoning out two scenarios…</div>
          ) : onLoadScenarios ? (
            <button
              onClick={onLoadScenarios}
              className="flex w-full items-center justify-center gap-1.5 text-xs font-medium text-violet-300 hover:text-violet-200"
              title="Fetch the AI's two forward scenarios for this pair. Info only — it does NOT change the engine's decision, and it uses AI tokens, so it's opt-in."
            >
              🤖 Show AI scenarios
              <span className="text-[10px] text-neutral-500">(optional · uses AI tokens)</span>
            </button>
          ) : null}
        </div>
      )}

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

// Plain-English card shown when the engine returns NO_TRADE (or "watching"). It reframes the raw
// "Risk Manager: VETOED / 0%" — which reads like a risk rejection — into what actually happened: the
// analysis engine chose to stand aside. Shows the engine's reason + a couple of derived "why" points.
function StandAsideCard({ proposal }: { proposal: TradeProposal }) {
  const watching = proposal.watch;
  const tfs = proposal.technical?.timeframes ?? [];
  const entryTf = tfs.find((t) => t.timeframe === proposal.timeframe) ?? tfs[0];
  const macro = tfs.length ? immediateHigher(tfs, proposal.timeframe) : undefined;
  const adx = entryTf?.indicators?.["adx"];

  // An armed pullback (buy-the-dip) is WATCHING with a conditional — the entry-TF counter-move is the
  // setup, not a conflict, so skip the "they disagree" framing (the rationale explains it).
  const armed = !!proposal.conditional;

  // Best-effort supporting points derived from the read — each only appears when the data supports it.
  const points: string[] = [];
  if (
    !armed && entryTf && macro && entryTf.trend !== macro.trend &&
    entryTf.trend !== "sideways" && macro.trend !== "sideways"
  ) {
    points.push(
      `Your timeframe (${entryTf.timeframe}) is ${entryTf.trend.toUpperCase()} but the bigger-picture ` +
        `${macro.timeframe} is ${macro.trend.toUpperCase()} — they disagree, so there's no confluence to trade with.`,
    );
  }
  if (typeof adx === "number" && adx < 20) {
    points.push(
      `Trend strength is weak (ADX ${adx.toFixed(0)}, under 20) — choppy conditions where entries often ` +
        `chop out, so the engine waits for a cleaner, stronger move.`,
    );
  }

  return (
    <div className="rounded-md border border-neutral-700 bg-neutral-800/40 p-3 text-sm">
      <div className="flex items-center gap-2">
        <span className="text-base">{watching ? "👀" : "✋"}</span>
        <span className="font-semibold text-neutral-200">
          {watching ? "Watching — setup still forming" : "No trade — the engine stood aside"}
        </span>
      </div>

      {/* The engine's own reason (plain language). */}
      <p className="mt-1.5 leading-relaxed text-neutral-100">{proposal.rationale}</p>

      {points.length > 0 && (
        <ul className="mt-2 space-y-1 text-neutral-300">
          {points.map((p, i) => (
            <li key={i} className="flex gap-1.5">
              <span className="text-neutral-500">•</span>
              <span>{p}</span>
            </li>
          ))}
        </ul>
      )}

      {/* Reframe it: a stand-aside is the engine's call, not a risk-limit rejection. */}
      <div className="mt-2 rounded bg-neutral-900/50 px-2.5 py-2 text-xs leading-relaxed text-neutral-400">
        <span className="font-medium text-neutral-300">What this means:</span> this is the analysis
        engine's own decision from the current read — <span className="text-neutral-300">not</span> a
        Risk-Manager rejection and not an error. The engine only opens a trade when the pieces line up:
        a setup on your timeframe, agreement with the higher-timeframe trend, enough trend strength and
        momentum, and a worthwhile reward-to-risk. When they don't, it stands aside instead of forcing a
        low-quality trade. There's simply no setup to size right now — re-run analysis later, or wait
        for the setup to form.
      </div>
    </div>
  );
}

// Normalize a timeframe's trend to a concise keyword. It's usually "up"/"down"/"sideways", but can
// occasionally arrive as a verbose description — keep the factor VALUE short so it never overflows.
function trendWord(t: string | undefined | null): string {
  if (!t) return "—";
  const s = t.trim().toLowerCase();
  if (["up", "uptrend", "bullish", "rising"].includes(s)) return "UP";
  if (["down", "downtrend", "bearish", "falling"].includes(s)) return "DOWN";
  if (["sideways", "range", "flat", "neutral"].includes(s)) return "SIDEWAYS";
  // Verbose/descriptive value: a range/corrective/mixed read -> SIDEWAYS (matches the neutral verdict);
  // otherwise a single clear direction; else SIDEWAYS. Keeps the factor value a clean keyword.
  if (/(sideways|range|flat|neutral|chop|correct|consolidat|mixed)/.test(s)) return "SIDEWAYS";
  const up = /\b(up|uptrend|bull|rising|higher)\b/.test(s);
  const down = /\b(down|downtrend|bear|falling|lower)\b/.test(s);
  if (up && !down) return "UP";
  if (down && !up) return "DOWN";
  return "SIDEWAYS";
}

function SetupSignals({ proposal, standAside, onToggleScenLevels, scenLevelsShown }: {
  proposal: TradeProposal; standAside?: boolean;
  onToggleScenLevels?: (levels: { support: number | null; resistance: number | null; target: number | null; invalidation: number | null } | null) => void;
  scenLevelsShown?: boolean;
}) {
  const tech = proposal.technical;
  if (!tech || !tech.timeframes.length) return null;

  const entryTf = tech.timeframes.find((t) => t.timeframe === proposal.timeframe) ?? tech.timeframes[0];
  const ind = entryTf?.indicators ?? {};
  // The higher-TF row shows the IMMEDIATE higher timeframe — the one the engine's trend gate uses.
  const macro = immediateHigher(tech.timeframes, proposal.timeframe);

  const adx = ind["adx"] as number | undefined;
  const macdH = ind["macd_hist"] as number | undefined;
  const atr = ind["atr14"] as number | undefined;
  // A MACD within the engine's noise band (|hist| < 10% of ATR, the same _MOM_ATR_FRAC gate the
  // engine uses) is FLAT — neither for nor against; don't count a near-zero histogram as a con.
  const macdFlat = macdH != null && atr != null && atr > 0 && Math.abs(macdH) < 0.1 * atr;
  const rsi = ind["rsi14"] as number | undefined;
  const e = proposal.entry;
  const s = proposal.stop_loss;
  const t = proposal.take_profit;
  const rr = e != null && s != null && t != null && e !== s ? Math.abs(t - e) / Math.abs(e - s) : null;

  // The direction the engine was weighing. Priority: a live proposal → an ARMED conditional's
  // direction (a buy-the-dip pullback arms a LONG even though the entry-TF is down) → the entry-TF
  // trend. Each factor is scored FOR/AGAINST that direction, so the read reflects the real trade.
  const ot = proposal.conditional?.order_type ?? "";
  const armedDir: "long" | "short" | null = ot.startsWith("buy") ? "long" : ot.startsWith("sell") ? "short" : null;
  const dir: "long" | "short" | null =
    proposal.direction === "long" || proposal.direction === "short"
      ? proposal.direction
      : armedDir ?? (entryTf?.trend === "up" ? "long" : entryTf?.trend === "down" ? "short" : null);
  const dirWord = dir ?? "trade";
  const actionWord = dir === "long" ? "buying" : dir === "short" ? "selling" : "trading";
  const htf = macro?.trend;

  type V = "good" | "bad" | "neutral";
  const macdDir = macdH == null ? null : macdH > 0 ? "long" : "short";
  // AI momentum classification (set only when the ambiguous-momentum fork ran) — takes over the
  // Momentum row so you see WHY it was read that way, not just the raw MACD sign.
  const mr = proposal.momentum_read;
  const mrLabel = mr ? ({ healthy_pullback: "Healthy pullback", weak_momentum: "Weak momentum",
                          probable_reversal: "Probable reversal" } as Record<string, string>)[mr.category] ?? mr.category : null;
  const mrVerdict: V | null = mr ? (mr.category === "healthy_pullback" ? "good"
    : mr.category === "probable_reversal" ? "bad" : "neutral") : null;
  const mrTone = mr ? (mr.category === "healthy_pullback" ? "text-bull"
    : mr.category === "probable_reversal" ? "text-bear" : "text-warn") : undefined;
  // AI regime-texture read (set only when the moderate-ADX boundary fork ran) — enriches the ADX row.
  const rg = proposal.regime_read;
  const rgLabel = rg ? ({ emerging_trend: "Emerging trend", choppy_range: "Choppy range",
                          transition: "Transition" } as Record<string, string>)[rg.category] ?? rg.category : null;
  const rgVerdict: V | null = rg ? (rg.category === "emerging_trend" ? "good"
    : rg.category === "choppy_range" ? "bad" : "neutral") : null;
  const rgTone = rg ? (rg.category === "emerging_trend" ? "text-bull"
    : rg.category === "choppy_range" ? "text-bear" : "text-warn") : undefined;
  // AI price-action read at a major opposing level (set only when a big-TF level was in the path).
  const pa = proposal.priceaction_read;
  const paLabel = pa ? ({ likely_break: "Likely break", likely_reject: "Likely reject",
                          indecision: "Indecision" } as Record<string, string>)[pa.category] ?? pa.category : null;
  const paVerdict: V | null = pa ? (pa.category === "likely_break" ? "good"
    : pa.category === "likely_reject" ? "bad" : "neutral") : null;
  const paTone = pa ? (pa.category === "likely_break" ? "text-bull"
    : pa.category === "likely_reject" ? "text-bear" : "text-warn") : undefined;
  const biasStr = proposal.fundamental?.bias;
  const biasDir = biasStr === "bullish" ? "long" : biasStr === "bearish" ? "short" : null;
  const htfAgree = !!(dir && htf && (dir === "long" ? htf === "up" : htf === "down"));
  // htf "against" only counts as a con for a MARKET trade; for an armed pullback the entry-TF
  // counter-move IS the setup, not a conflict — the higher TF is what we're joining.
  const htfAgainst = !!(dir && htf && !armedDir && (dir === "long" ? htf === "down" : htf === "up"));
  // Chasing (buying high / selling low) is a con; entering at a favorable extreme (buy the dip /
  // sell the rally) is a pro.
  const rsiBad = rsi != null && dir != null && ((dir === "short" && rsi <= 30) || (dir === "long" && rsi >= 70));
  const rsiGood = rsi != null && dir != null && ((dir === "long" && rsi <= 35) || (dir === "short" && rsi >= 65));
  // Absolute RSI zone — always reported, even with no trade direction (fixes "76 = normal range").
  const rsiZone = rsi == null ? null : rsi >= 70 ? "overbought" : rsi <= 30 ? "oversold" : "normal";

  const factors: { label: string; value: string; tone?: string; verdict: V; note: string }[] = [
    {
      label: "Entry-TF trend",
      value: trendWord(entryTf?.trend),
      tone: trendTone(entryTf?.trend),
      verdict: dir ? "neutral" : "bad",
      note: dir
        ? `Your ${proposal.timeframe} points ${trendWord(entryTf?.trend)} — the ${dirWord} the engine weighed.`
        : "No clear trend on your timeframe — nothing to trade.",
    },
    {
      label: `Higher-TF trend (${macro?.timeframe ?? "—"})`,
      value: trendWord(htf),
      tone: trendTone(htf),
      verdict: htfAgainst ? "bad" : htfAgree ? "good" : "neutral",
      note: htfAgainst
        ? `The ${macro?.timeframe} is ${trendWord(htf)} — against a ${dirWord}. No confluence: this is the blocker.`
        : htfAgree
          ? `Agrees with the ${dirWord} — confluence.`
          : "Sideways — neither helps nor blocks.",
    },
    {
      label: "Trend strength (ADX)",
      value: rg
        ? `AI: ${rgLabel}${rg.confidence != null ? ` (${Math.round(rg.confidence * 100)}%)` : ""}`
        : adx == null ? "—" : `${adx.toFixed(0)} ${adx >= 25 ? "(strong)" : adx < 20 ? "(weak)" : "(moderate)"}`,
      tone: rg ? rgTone : undefined,
      verdict: rg ? rgVerdict! : adx == null ? "neutral" : adx >= 25 ? "good" : adx < 20 ? "bad" : "neutral",
      note: rg
        ? `AI read the moderate-ADX regime — ${rg.evidence}`
        : adx == null ? "" : adx >= 25 ? "Strong trend — worth trading." : adx < 20 ? "Weak / choppy — trend entries fail here." : "Moderate — trend still forming.",
    },
    // Price-action at a major opposing level — only present when the AI level-read ran.
    ...(pa
      ? [{
          label: "Level in the path",
          value: `AI: ${paLabel}${pa.confidence != null ? ` (${Math.round(pa.confidence * 100)}%)` : ""}`,
          tone: paTone,
          verdict: paVerdict!,
          note: `AI read the level — ${pa.evidence}`,
        }]
      : []),
    {
      label: "Momentum (MACD)",
      value: mr
        ? `AI: ${mrLabel}${mr.confidence != null ? ` (${Math.round(mr.confidence * 100)}%)` : ""}`
        : macdH == null ? "—" : macdFlat ? `flat (${macdH.toFixed(3)})` : `${macdH > 0 ? "bullish" : "bearish"} (${macdH.toFixed(3)})`,
      tone: mr ? mrTone : macdH == null || macdFlat ? undefined : macdH > 0 ? "text-bull" : "text-bear",
      verdict: mr ? mrVerdict! : macdFlat ? "neutral" : macdDir && dir ? (macdDir === dir ? "good" : "bad") : "neutral",
      note: mr
        ? `AI read the pullback — ${mr.evidence}`
        : macdFlat ? "Momentum is flat (within the noise band) — neither for nor against."
          : macdDir && dir ? (macdDir === dir ? `Momentum backs the ${dirWord}.` : `Momentum runs against the ${dirWord}.`) : "",
    },
    {
      label: "RSI (14)",
      value: rsi == null ? "—" : rsi.toFixed(1),
      tone: rsiBad ? "text-bear" : rsiGood ? "text-bull"
        : rsiZone === "overbought" || rsiZone === "oversold" ? "text-warn" : undefined,
      verdict: rsiBad ? "bad" : rsiGood ? "good" : "neutral",
      note: rsiBad
        ? `${rsi!.toFixed(0)} is ${dir === "short" ? "oversold" : "overbought"} — ${actionWord} into an exhausted move (chasing).`
        : rsiGood
          ? `${rsi!.toFixed(0)} is ${dir === "long" ? "a dip — buying low (value)" : "a rally — selling high (value)"}.`
          : rsi == null ? ""
            : rsiZone === "overbought" ? `${rsi!.toFixed(0)} is overbought (>70) — stretched; a pullback is likely.`
              : rsiZone === "oversold" ? `${rsi!.toFixed(0)} is oversold (<30) — stretched; a bounce is likely.`
                : "In a normal range — room to move.",
    },
    {
      label: "Fundamental bias",
      value: (biasStr ?? "—").toUpperCase(),
      verdict: biasDir && dir ? (biasDir === dir ? "good" : "bad") : "neutral",
      note: biasDir && dir ? (biasDir === dir ? `Leans with the ${dirWord}.` : `Leans against the ${dirWord}.`) : "Neutral — no lean either way.",
    },
    // Reward:Risk only exists for an actionable setup (a stand-aside has no levels yet).
    ...(standAside
      ? []
      : [{
          label: "Reward : Risk",
          value: rr == null ? "—" : `${rr.toFixed(2)} : 1`,
          tone: rr != null && rr >= 2 ? "text-bull" : undefined,
          verdict: (rr == null ? "neutral" : rr >= 1.5 ? "good" : "bad") as V,
          note: rr == null ? "" : rr >= 1.5 ? "Worthwhile payoff for the risk." : "Too thin — reward doesn't justify the risk.",
        }]),
  ];

  const goods = factors.filter((f) => f.verdict === "good").length;
  const bads = factors.filter((f) => f.verdict === "bad").length;
  const V_ICON: Record<V, string> = { good: "✓", bad: "✗", neutral: "•" };
  const V_TONE: Record<V, string> = { good: "text-bull", bad: "text-bear", neutral: "text-neutral-600" };

  // ---- Probability outlook (DETERMINISTIC — derived from the same read above; no AI tokens) ----
  // Turns the factors into rough odds for the three ways this resolves for the direction the engine
  // weighed: it continues (target), stalls/ranges (no follow-through / an armed limit never fills), or
  // reverses (invalidation). A heuristic, not a guarantee — the % just makes the spread explicit.
  const armedLimit = (proposal.conditional?.order_type ?? "").includes("limit");
  const healthyPB = mr?.category === "healthy_pullback";
  const rejecting = pa?.category === "likely_reject";   // AI reads the opposing level as HOLDING (price rejects)

  // Nearest S/R across ALL timeframes, tagged with the TF (so a path can cite "1H resistance 84.406 /
  // 4H support 81.794" like a chart-reader) — the outlook, the narratives, and the "show levels" button
  // all RESPECT these, not just the R:R. `pxRef` = entry, or last close for a watch.
  const pxRef = (proposal.entry ?? (ind["last_close"] as number | undefined)) ?? null;
  const resAbove: { price: number; tf: string }[] = [];
  const supBelow: { price: number; tf: string }[] = [];
  if (pxRef != null) {
    for (const tfr of tech.timeframes) {
      for (const r of tfr.resistance_levels ?? []) if (typeof r === "number" && r > pxRef) resAbove.push({ price: r, tf: tfr.timeframe });
      for (const sv of tfr.support_levels ?? []) if (typeof sv === "number" && sv < pxRef) supBelow.push({ price: sv, tf: tfr.timeframe });
    }
  }
  const nearResL = resAbove.sort((a, b) => a.price - b.price)[0] ?? null;   // nearest above
  const nearSupL = supBelow.sort((a, b) => b.price - a.price)[0] ?? null;   // nearest below
  const nearRes = nearResL?.price ?? null;
  const nearSup = nearSupL?.price ?? null;
  const setupTarget = (proposal.take_profit ?? proposal.conditional?.take_profit) ?? null;
  const setupStop = (proposal.stop_loss ?? proposal.conditional?.stop_loss) ?? null;
  // Headroom (in ATR) toward the target side — a wall just ahead caps upside; clear runway helps.
  const targetSide = dir === "long" ? nearRes : dir === "short" ? nearSup : null;
  const headroomAtr = targetSide != null && pxRef != null && atr && atr > 0 ? Math.abs(targetSide - pxRef) / atr : null;

  const outlook = (() => {
    if (!dir) return null;                                // no directional edge -> no scenarios
    let cont = 0.45, stall = 0.20, rev = 0.15;
    if (htfAgree) cont += 0.20; else if (htfAgainst) { cont -= 0.15; rev += 0.18; }
    if (adx != null) { if (adx >= 40) cont += 0.15; else if (adx >= 25) cont += 0.08; else if (adx < 20) { cont -= 0.05; rev += 0.10; stall += 0.10; } }
    if (macdFlat) stall += 0.10;
    else if (macdDir && macdDir === dir) cont += 0.08;
    else if (macdDir && macdDir !== dir && !healthyPB) rev += 0.06;   // pullback IS the setup -> no penalty
    if (rsiGood) cont += 0.05;
    if (rsiBad) rev += 0.10;
    if (mr) { if (mr.category === "healthy_pullback") cont += 0.12; else if (mr.category === "weak_momentum") stall += 0.15; else if (mr.category === "probable_reversal") { rev += 0.22; cont -= 0.10; } }
    if (pa) {
      const c = pa.confidence ?? 0.6;
      if (pa.category === "likely_break") cont += 0.10 + 0.15 * c;        // AI: the wall gives way -> continue
      else if (pa.category === "likely_reject") {                         // AI: the wall HOLDS -> reject & pull back
        const w = 0.20 + 0.35 * c;                                        // weighted by the AI's confidence
        cont -= w; stall += w * 0.65; rev += w * 0.35;
      } else stall += 0.10;                                               // indecision
    }
    if (rg) { if (rg.category === "emerging_trend") cont += 0.06; else if (rg.category === "choppy_range") { stall += 0.10; cont -= 0.05; } else stall += 0.05; }
    if (armedLimit) stall += 0.10;                        // a pending limit can simply never get filled
    // STRUCTURE: a tested wall just ahead caps the continuation. When the AI explicitly reads the level
    // as HOLDING (likely_reject), even a strong trend loses the usual break-through exemption.
    if (headroomAtr != null) {
      if (headroomAtr < 1 && (rejecting || !(adx != null && adx >= 40))) { cont -= 0.10; stall += 0.06; rev += 0.04; }
      else if (headroomAtr >= 3 && !rejecting) cont += 0.05;
    }
    const raw = [Math.max(0.04, cont), Math.max(0.04, stall), Math.max(0.04, rev)];
    const sum = raw[0] + raw[1] + raw[2];
    const pct = raw.map((r) => Math.round((r / sum) * 100));
    pct[0] += 100 - (pct[0] + pct[1] + pct[2]);           // absorb rounding into the top bucket
    return { pct };
  })();
  // Prefer the actionable R:R; fall back to the armed conditional's R:R (a watch has no proposal-level levels).
  const rrEff = rr ?? proposal.conditional?.rr ?? null;
  const rrSuffix = rrEff != null ? ` (~${rrEff.toFixed(1)}R)` : "";
  const isPullback = armedLimit || healthyPB;
  const long = dir === "long";
  // Human-readable level references, tagged with their timeframe (e.g. "1H resistance 84.406").
  const resStr = nearResL ? `${nearResL.tf.toUpperCase()} resistance ${fmtPrice(nearResL.price)}` : null;
  const supStr = nearSupL ? `${nearSupL.tf.toUpperCase()} support ${fmtPrice(nearSupL.price)}` : null;
  const tgtStr = setupTarget != null ? fmtPrice(setupTarget) : null;
  const stopStr = setupStop != null ? fmtPrice(setupStop) : null;

  // Each scenario is a PATH narrative citing the real levels. Two framings: the engine is REJECTING at
  // the opposing level (AI likely_reject -> the pause/pullback is the main outcome, NOT a continuation),
  // or it's WITH the trend (continuation is the main outcome).
  let contLabel: string, contPath: string, stallLabel: string, stallPath: string, revLabel: string, revPath: string;
  if (rejecting) {
    contLabel = long ? "Breaks resistance → continues up" : "Breaks support → continues down";
    contPath = long
      ? `Breaks through ${resStr ?? "resistance"} and continues up${tgtStr ? ` → target ${tgtStr}` : ""}${rrSuffix}.`
      : `Breaks ${supStr ?? "support"} and continues down${tgtStr ? ` → target ${tgtStr}` : ""}${rrSuffix}.`;
    stallLabel = long ? "Rejects at resistance, pulls back" : "Rejects at support, bounces";
    stallPath = long
      ? `Rejects near ${resStr ?? "resistance"} (as the AI read), pulls back ${supStr ? `toward ${supStr}` : "to value (~EMA20)"} — a pause in the uptrend, not a breakout.`
      : `Rejects near ${supStr ?? "support"} (as the AI read), bounces ${resStr ? `toward ${resStr}` : "to value (~EMA20)"} — a pause in the downtrend, not a breakdown.`;
    revLabel = long ? "Deeper reversal — loses support" : "Deeper reversal — breaks resistance";
    revPath = long
      ? `Turns down hard and loses ${supStr ?? "support"} → the uptrend structure breaks${stopStr ? `, stop ${stopStr}` : ""}.`
      : `Turns up hard and breaks ${resStr ?? "resistance"} → the downtrend structure breaks${stopStr ? `, stop ${stopStr}` : ""}.`;
  } else {
    contLabel = isPullback
      ? (long ? "Pullback to support, then continuation up" : "Bounce to resistance, then continuation down")
      : (long ? "Continuation up" : "Continuation down");
    contPath = isPullback
      ? (long
          ? `${resStr ? `Rejects near ${resStr}, ` : ""}pulls back ${supStr ? `toward ${supStr}` : "to value (~EMA20)"}, then resumes upward if support holds${tgtStr ? ` → target ${tgtStr}` : ""}${rrSuffix}.`
          : `${supStr ? `Bounces near ${supStr}, ` : ""}rallies ${resStr ? `toward ${resStr}` : "to value (~EMA20)"}, then resumes downward if resistance holds${tgtStr ? ` → target ${tgtStr}` : ""}${rrSuffix}.`)
      : (long
          ? `Holds ${supStr ? `above ${supStr}` : "the trend"}${resStr ? ` and pushes through ${resStr}` : ""}${tgtStr ? ` toward target ${tgtStr}` : ""}${rrSuffix}.`
          : `Holds ${resStr ? `below ${resStr}` : "the trend"}${supStr ? ` and breaks ${supStr}` : ""}${tgtStr ? ` toward target ${tgtStr}` : ""}${rrSuffix}.`);
    stallLabel = "Stalls / ranges — no follow-through";
    stallPath = `Chops ${supStr && resStr ? `between ${supStr} and ${resStr}` : "sideways"} without committing${armedLimit ? " — the pending limit may never fill" : ""}.`;
    revLabel = long ? "Loses support — reverses" : "Breaks resistance — reverses";
    revPath = long
      ? `Breaks below ${supStr ?? "support"} → the long invalidates${stopStr ? `, stop ${stopStr}` : ""} (~-1R).`
      : `Breaks above ${resStr ?? "resistance"} → the short invalidates${stopStr ? `, stop ${stopStr}` : ""} (~-1R).`;
  }

  const scenarios = outlook ? [
    { key: "cont", tone: "text-bull", bar: "bg-bull", prob: outlook.pct[0], label: contLabel, path: contPath },
    { key: "stall", tone: "text-warn", bar: "bg-warn", prob: outlook.pct[1], label: stallLabel, path: stallPath },
    { key: "rev", tone: "text-bear", bar: "bg-bear", prob: outlook.pct[2], label: revLabel, path: revPath },
  ] : [];
  // The "chosen" path = the MOST LIKELY outcome (the engine's implied read) — not always continuation.
  const chosenKey = scenarios.length ? [...scenarios].sort((a, b) => b.prob - a.prob)[0].key : null;

  // The setup's own lines for the "Show levels" button (reuses the AI-scenario chart overlay channel):
  // target + stop/invalidation from the proposal (or the armed conditional) + the nearest S/R it respects.
  const setupLevels = (setupTarget != null || setupStop != null || nearSup != null || nearRes != null)
    ? { support: nearSup, resistance: nearRes, target: setupTarget, invalidation: setupStop }
    : null;

  return (
    <div className="rounded-md border border-neutral-800 p-3">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-wide text-neutral-400">
          {standAside ? "What the analysis saw" : "Why this setup"}
        </span>
        <span className="text-[10px] tabular-nums">
          <span className="text-bull">{goods} for</span>
          <span className="text-neutral-600"> · </span>
          <span className="text-bear">{bads} against</span>
        </span>
      </div>
      <div className="space-y-1.5">
        {factors.map((f) => (
          <div key={f.label} className="flex gap-2 text-sm">
            <span className={`mt-0.5 w-3 shrink-0 text-center font-bold ${V_TONE[f.verdict]}`}>{V_ICON[f.verdict]}</span>
            <div className="min-w-0 flex-1">
              <div className="flex items-baseline justify-between gap-2">
                <span className="shrink-0 text-neutral-300">{f.label}</span>
                <span className={`min-w-0 flex-1 truncate text-right tabular-nums ${f.tone ?? "text-neutral-200"}`}
                      title={f.value}>{f.value}</span>
              </div>
              {f.note && <div className="text-[11px] leading-snug text-neutral-500">{f.note}</div>}
            </div>
          </div>
        ))}
      </div>

      {/* Probability outlook — deterministic scenarios from the read above (no AI tokens). */}
      {scenarios.length > 0 && (
        <div className="mt-3 border-t border-neutral-800 pt-2.5">
          <div className="mb-1.5 flex items-center justify-between">
            <span className="text-xs font-semibold uppercase tracking-wide text-neutral-400">
              Probability outlook
            </span>
            <span className="text-[10px] text-neutral-600">from the read · not a guarantee</span>
          </div>
          {/* stacked odds bar (continue / stall / reverse) */}
          <div className="mb-2 flex h-1.5 overflow-hidden rounded-full bg-neutral-800">
            {scenarios.map((sc) => (
              <div key={sc.key} className={sc.bar} style={{ width: `${sc.prob}%` }} title={`${sc.label} — ${sc.prob}%`} />
            ))}
          </div>
          <div className="space-y-2">
            {[...scenarios].sort((a, b) => b.prob - a.prob).map((sc) => (
              <div key={sc.key} className="flex gap-2 text-sm">
                <span className={`w-9 shrink-0 text-right font-semibold tabular-nums ${sc.tone}`}>{sc.prob}%</span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <span className={`font-medium ${sc.tone}`}>{sc.label}</span>
                    {sc.key === chosenKey && (
                      <span className="rounded bg-neutral-700 px-1 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-neutral-300">
                        most likely
                      </span>
                    )}
                  </div>
                  <div className="text-[11px] leading-snug text-neutral-400">{sc.path}</div>
                </div>
              </div>
            ))}
          </div>

          {/* Plot the setup's own lines — target, stop/invalidation, and the nearest S/R it respects. */}
          {onToggleScenLevels && setupLevels && (
            <button
              onClick={() => onToggleScenLevels(scenLevelsShown ? null : setupLevels)}
              className="mt-2.5 w-full rounded border border-neutral-700 py-1.5 text-xs font-medium text-neutral-200 hover:bg-neutral-800"
              title="Draw the target, stop/invalidation, and nearest support/resistance on the chart"
            >
              {scenLevelsShown ? "✕ Hide levels on the chart" : "📈 Show these levels on the chart"}
            </button>
          )}
        </div>
      )}
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

// FOLLOW-UP on a trade you are already in: does the latest read still back it?
//
// The card below is a NEW-ENTRY check — it answers "would I open this now?". That is not the same
// question as "should I stay in?", and conflating them is how people talk themselves out of good
// trades. So this states the comparison plainly and marks the one case that genuinely matters:
// the engine now reading the OPPOSITE direction, i.e. the thesis you entered on has flipped.
//
// It never says "close". Exits belong to the position advisor (stop / target / trail), which acts on
// its own rules; this is context for your decision, not an instruction.
function FollowUp({
  pos, proposal, analyzedAt, onRecheck, analyzing,
}: {
  pos: PositionView;
  proposal: TradeProposal;
  analyzedAt?: string | null;
  onRecheck?: () => void;
  analyzing?: boolean;
}) {
  const mine = pos.direction.toLowerCase();                 // long | short
  const now = proposal.direction;                           // long | short | no_trade
  const agrees = now === mine;
  const opposed = (now === "long" || now === "short") && now !== mine;

  const tone = opposed
    ? "border-bear/40 bg-bear/10"
    : agrees
      ? "border-bull/40 bg-bull/10"
      : "border-neutral-700 bg-neutral-800/40";
  const headline = opposed
    ? `⚠ The engine now reads ${now.toUpperCase()} — the opposite of your ${mine.toUpperCase()}`
    : agrees
      ? `✓ Still reads ${now.toUpperCase()} — the latest analysis agrees with your trade`
      : "• No fresh setup right now — that is not an exit signal";
  const detail = opposed
    ? "The read you entered on has flipped. Worth a hard look: tighten the stop, take part off, or " +
      "close. It is not automatic — a flip against an open trade often happens mid-pullback."
    : agrees
      ? "The direction, trend and momentum still line up the way they did when you opened."
      : "The engine only says it would not OPEN a new trade here (no clean entry from the current " +
        "price). Your existing position is managed by its stop, target and the advisor.";

  const pnl = pos.unrealized_pnl ?? 0;
  return (
    <div className={`rounded-md border p-2 text-sm ${tone}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-medium">{headline}</span>
        <span className="flex items-center gap-2">
          {analyzedAt && <AnalysisAge iso={analyzedAt} timeframe={proposal.timeframe} />}
          {onRecheck && (
            <button
              onClick={onRecheck}
              disabled={analyzing}
              className="btn btn-ghost text-xs"
              title="Re-run the analysis now and compare it against this open trade"
            >
              {analyzing ? "Checking…" : "Re-check now"}
            </button>
          )}
        </span>
      </div>
      <div className="mt-1 text-neutral-400">{detail}</div>

      {/* The advisor's verdict on the trade you HOLD — the direct answer to "stay or not?", which
          the new-entry check above can only hint at. Deterministic (no AI), so it costs nothing. */}
      <AdvisorVerdict symbol={pos.symbol} refreshKey={analyzedAt ?? ""} />
      <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-0.5 text-[11px] text-neutral-400">
        <span>
          You are {mine.toUpperCase()} from <strong className="text-neutral-200">{fmtPrice(pos.entry_price)}</strong>
        </span>
        {pos.last_price != null && <span>now {fmtPrice(pos.last_price)}</span>}
        <span className={pnl >= 0 ? "text-bull" : "text-bear"}>
          {pnl >= 0 ? "+" : "−"}{fmtUsd(Math.abs(pnl))}
        </span>
        {pos.stop_loss != null && <span>stop {fmtPrice(pos.stop_loss)}</span>}
        {pos.take_profit != null && <span>target {fmtPrice(pos.take_profit)}</span>}
      </div>
    </div>
  );
}

// The position advisor's read on a trade you already hold.
//
// This is the piece the entry analysis genuinely cannot give you: `thesis` is judged against the
// levels you ACTUALLY entered on, so it answers "is the reason I'm in this still true?" rather than
// "would I open it again from here?". Deterministic — no LLM, no tokens — so it can be polled freely.
function AdvisorVerdict({ symbol, refreshKey }: { symbol: string; refreshKey: string }) {
  const [advice, setAdvice] = useState<PositionAdvice | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .positionAdvice()
      .then((list) => {
        if (cancelled) return;
        setAdvice(list.find((a) => a.symbol.toUpperCase() === symbol.toUpperCase()) ?? null);
      })
      .catch(() => {
        if (!cancelled) setAdvice(null);   // advisor off / unreachable — just show nothing
      });
    return () => {
      cancelled = true;
    };
  }, [symbol, refreshKey]);

  if (!advice) return null;

  const THESIS: Record<string, { label: string; cls: string }> = {
    intact: { label: "Thesis intact", cls: "bg-bull/15 text-bull" },
    weakening: { label: "Thesis weakening", cls: "bg-warn/15 text-warn" },
    invalidated: { label: "Thesis invalidated", cls: "bg-bear/15 text-bear" },
    unknown: { label: "Thesis unclear", cls: "bg-neutral-700/60 text-neutral-400" },
  };
  const t = THESIS[advice.thesis] ?? THESIS.unknown;

  return (
    <div className="mt-2 rounded border border-neutral-700/60 bg-neutral-900/40 p-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className={`rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide ${t.cls}`}>
          {t.label}
        </span>
        <span className="text-xs font-medium text-neutral-200">{advice.headline}</span>
        {advice.r_multiple != null && (
          <span className={`text-[11px] ${advice.r_multiple >= 0 ? "text-bull" : "text-bear"}`}>
            {advice.r_multiple >= 0 ? "+" : ""}{advice.r_multiple.toFixed(2)}R
          </span>
        )}
        {!advice.has_stop && (
          <span className="rounded bg-bear/15 px-1.5 py-0.5 text-[10px] font-bold text-bear">
            NO STOP
          </span>
        )}
      </div>
      {advice.detail && (
        <div className="mt-1 text-[11px] leading-snug text-neutral-400">{advice.detail}</div>
      )}
      {advice.event_label && (
        <div className="mt-1 text-[11px] text-warn">
          ⚠ {advice.event_label}
          {advice.minutes_to_event != null && ` in ~${advice.minutes_to_event}m`}
        </div>
      )}
    </div>
  );
}

// Bars of the entry timeframe, in minutes — how long one "candle" of context lasts.
const TF_MINUTES: Record<string, number> = {
  "1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440,
};

// WHEN this read was taken, and how much to trust it now.
//
// A read is only as good as the tape it was taken from. The same card looks identical whether it
// ran 30 seconds or 3 days ago, so age is shown plainly and COLOURED by how many bars of the entry
// timeframe have closed since: within ~1 bar nothing has really changed, past ~3 the levels and
// momentum it cites may simply no longer be true. Hover for the exact local time.
function AnalysisAge({ iso, timeframe }: { iso: string; timeframe: string }) {
  const safe = /[Z+]|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`;
  const mins = Math.max(0, (Date.now() - new Date(safe).getTime()) / 60000);
  const bar = TF_MINUTES[timeframe] ?? 60;
  const bars = mins / bar;
  const tone =
    bars >= 3 ? "bg-bear/15 text-bear" : bars >= 1 ? "bg-warn/15 text-warn" : "text-neutral-500";
  const note =
    bars >= 3 ? " · stale, re-run it" : bars >= 1 ? " · a bar has closed since" : "";
  return (
    <span
      className={`rounded px-1.5 py-0.5 text-[11px] ${tone}`}
      title={`Analysed ${localTime(iso)}${note ? ` — ${Math.floor(bars)} ${timeframe} bar(s) have closed since` : ""}`}
    >
      🕐 {ago(iso)}
      {note}
    </span>
  );
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

// Trend-alignment grade — how clearly the direction stacks up across TFs + strength + momentum.
// A+ (≥0.85) = the clearest "price is going up/down"; the highest-conviction setups to lean into.
function AlignmentBadge({ alignment }: { alignment?: number | null }) {
  if (alignment == null) return null;
  const grade = alignment >= 0.85 ? "A+" : alignment >= 0.7 ? "A" : alignment >= 0.5 ? "B" : "C";
  const cls =
    grade === "A+" ? "bg-bull/25 text-bull ring-1 ring-bull/40"
    : grade === "A" ? "bg-bull/15 text-bull"
    : grade === "B" ? "bg-neutral-700 text-neutral-300"
    : "bg-neutral-800 text-neutral-500";
  return (
    <span
      className={`rounded px-1.5 py-0.5 text-[10px] font-bold ${cls}`}
      title={`Trend alignment ${(alignment * 100).toFixed(0)}% (${grade}) — how clearly every timeframe + strength + momentum stack the same way. A+ = the clearest, highest-conviction direction.`}
    >
      {grade === "A+" ? "★ A+" : grade} align
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

function StatusBadge({ status, positionOpen, standAside }: { status: string | null; positionOpen?: boolean | null; standAside?: boolean }) {
  if (!status) return null;
  // A stand-aside proposal is stored as "risk_vetoed" (nothing to size), which misreads as a risk
  // rejection — show "no trade" instead. An executed proposal whose position has since closed reads
  // "closed", not "executed".
  const label = standAside
    ? "no trade"
    : status === "executed" && positionOpen === false
      ? "closed"
      : status;
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

