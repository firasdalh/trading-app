import { useEffect, useState } from "react";
import { api } from "../api/client";
import { fmtPrice, fmtUsd } from "../format";
import { ArmSetupButton } from "./ArmSetupButton";
import { RegimeBadge } from "./RegimeBadge";
import { AiDecisionCard } from "./AiDecisionCard";
import { ReviewExplanation } from "./ReviewExplanation";
import { ScenarioCard } from "./ScenarioCard";
import type {
  AiScenarioRead,
  AnalyzeResponse,
  ConditionalSetupView,
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
  armedSetup?: ConditionalSetupView | null;   // an armed 'wait for the break' setup for this symbol
  busy: boolean;
  equity?: number | null;
  onApprove: (lots?: number | null) => void;
  onReject: () => void;
  onRunAnalysis?: () => void;   // re-run analysis for the charted symbol (same as the top button)
  analyzing?: boolean;
  scenario?: AiScenarioRead | null;   // Step 4: AI two-scenario read (info only)
  scenarioBusy?: boolean;
}

// Shows the current proposal: direction, levels, confidence, the risk-adjusted size, the
// risk-manager verdict, the cost/leverage + an adjustable (3%-capped) size, and each agent's
// reasoning (expandable). Approve/Reject in Mode A.
export function ProposalPanel({ result, status, positionOpen, armedSetup, busy, equity, onApprove, onReject, onRunAnalysis, analyzing, scenario, scenarioBusy }: Props) {
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
      <SetupSignals proposal={proposal} standAside={noTrade} />

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

      {/* AI two-scenario read (the clean "what's likely + the levels + invalidation" analysis). Shown
          ALSO when the AI is the DECIDER, so you always see the scenario read the decision follows. */}
      {(scenario || scenarioBusy) && (
        <div className="rounded-md border border-violet-800/40 bg-violet-950/10 p-2">
          {scenario ? (
            <ScenarioCard read={scenario} />
          ) : (
            <div className="text-xs text-violet-300/80">🤖 Reasoning out two scenarios…</div>
          )}
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

function SetupSignals({ proposal, standAside }: { proposal: TradeProposal; standAside?: boolean }) {
  const tech = proposal.technical;
  if (!tech || !tech.timeframes.length) return null;

  const entryTf = tech.timeframes.find((t) => t.timeframe === proposal.timeframe) ?? tech.timeframes[0];
  const ind = entryTf?.indicators ?? {};
  // The higher-TF row shows the IMMEDIATE higher timeframe — the one the engine's trend gate uses.
  const macro = immediateHigher(tech.timeframes, proposal.timeframe);

  const adx = ind["adx"] as number | undefined;
  const macdH = ind["macd_hist"] as number | undefined;
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
      value: (entryTf?.trend ?? "—").toUpperCase(),
      tone: trendTone(entryTf?.trend),
      verdict: dir ? "neutral" : "bad",
      note: dir
        ? `Your ${proposal.timeframe} points ${(entryTf?.trend ?? "").toUpperCase()} — the ${dirWord} the engine weighed.`
        : "No clear trend on your timeframe — nothing to trade.",
    },
    {
      label: `Higher-TF trend (${macro?.timeframe ?? "—"})`,
      value: (htf ?? "—").toUpperCase(),
      tone: trendTone(htf),
      verdict: htfAgainst ? "bad" : htfAgree ? "good" : "neutral",
      note: htfAgainst
        ? `The ${macro?.timeframe} is ${(htf ?? "").toUpperCase()} — against a ${dirWord}. No confluence: this is the blocker.`
        : htfAgree
          ? `Agrees with the ${dirWord} — confluence.`
          : "Sideways — neither helps nor blocks.",
    },
    {
      label: "Trend strength (ADX)",
      value: adx == null ? "—" : `${adx.toFixed(0)} ${adx >= 25 ? "(strong)" : adx < 20 ? "(weak)" : "(moderate)"}`,
      verdict: adx == null ? "neutral" : adx >= 25 ? "good" : adx < 20 ? "bad" : "neutral",
      note: adx == null ? "" : adx >= 25 ? "Strong trend — worth trading." : adx < 20 ? "Weak / choppy — trend entries fail here." : "Moderate — trend still forming.",
    },
    {
      label: "Momentum (MACD)",
      value: macdH == null ? "—" : `${macdH > 0 ? "bullish" : "bearish"} (${macdH.toFixed(3)})`,
      tone: macdH == null ? undefined : macdH > 0 ? "text-bull" : "text-bear",
      verdict: macdDir && dir ? (macdDir === dir ? "good" : "bad") : "neutral",
      note: macdDir && dir ? (macdDir === dir ? `Momentum backs the ${dirWord}.` : `Momentum runs against the ${dirWord}.`) : "",
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
                <span className="text-neutral-300">{f.label}</span>
                <span className={`shrink-0 tabular-nums ${f.tone ?? "text-neutral-200"}`}>{f.value}</span>
              </div>
              {f.note && <div className="text-[11px] leading-snug text-neutral-500">{f.note}</div>}
            </div>
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

