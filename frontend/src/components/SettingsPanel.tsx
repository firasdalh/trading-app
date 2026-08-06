import { useEffect, useState } from "react";
import { api } from "../api/client";
import { assetLabel } from "../format";
import type { AssetClass, ExecutionMode, LlmStatus, Mt5Status, SettingsResponse } from "../types";

const LLM_DEFAULT_MODEL: Record<string, string> = {
  anthropic: "claude-opus-4-8",
  gemini: "gemini-2.5-flash",
  openai: "gpt-5-mini",
};

const ASSET_CLASSES: AssetClass[] = ["stock", "crypto", "forex", "metal", "energy", "index"];
const BROKERS = ["sim", "alpaca", "ccxt", "oanda", "mt5"];

interface Props {
  settings: SettingsResponse | null;
  onClose: () => void;
  onChanged: () => void;
}

// Mode C (auto-execute live) was removed — the Hybrid auto-pilot is the automation path.
const MODES: { value: ExecutionMode; label: string; desc: string }[] = [
  { value: "A_PROPOSE_APPROVE", label: "A · Propose & Approve", desc: "AI proposes; you approve every order." },
  { value: "B_AUTO_PAPER", label: "B · Auto-execute (Paper)", desc: "Risk-approved proposals execute automatically — paper only." },
];

// Settings modal: execution mode (A/B) with the live-confirmation gate, broker env,
// a read-only view of the RISK.md-bounded risk limits, and a flatten-all action.
export function SettingsPanel({ settings, onClose, onChanged }: Props) {
  const [phrase, setPhrase] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [maxPos, setMaxPos] = useState("");
  const [exposurePct, setExposurePct] = useState("");
  useEffect(() => {
    if (settings) {
      setMaxPos(String(settings.risk.max_open_positions));
      setExposurePct((settings.risk.max_total_exposure * 100).toFixed(0));
    }
  }, [settings]);

  const currentMode = settings?.app.execution_mode;
  const isLive = settings?.app.broker_env === "live";
  const reconfirm = settings?.live_re_confirm_required;

  const run = async (fn: () => Promise<unknown>, ok: string) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await fn();
      setNotice(ok);
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const chooseMode = (mode: ExecutionMode) => {
    run(() => api.setMode(mode), `Switched to ${mode}.`);
  };

  const saveMaxPos = () => {
    const n = Math.round(Number(maxPos));
    if (!Number.isFinite(n) || n < 1 || n === settings?.risk.max_open_positions) return;
    run(() => api.updateRisk({ max_open_positions: n }), `Max open trades set to ${n}.`);
  };
  const saveExposure = () => {
    const pct = Number(exposurePct);
    if (!Number.isFinite(pct) || pct <= 0 || pct > 100) return;
    if (settings && Math.abs(pct / 100 - settings.risk.max_total_exposure) < 1e-9) return;
    run(() => api.updateRisk({ max_total_exposure: pct / 100 }), `Max exposure set to ${pct}%.`);
  };

  return (
    <div className="fixed inset-0 z-30 flex items-start justify-center bg-black/60 p-4">
      <div className="card mt-12 flex max-h-[88vh] w-full max-w-lg flex-col">
        <div className="flex shrink-0 items-center justify-between">
          <h2 className="text-lg font-semibold">Settings</h2>
          <button onClick={onClose} className="text-neutral-400 hover:text-white">
            ✕
          </button>
        </div>

        <div className="mt-4 space-y-4 overflow-y-auto pr-1">
        {error && (
          <div className="rounded border border-bear/40 bg-bear/10 px-3 py-2 text-sm text-bear">{error}</div>
        )}
        {notice && (
          <div className="rounded border border-bull/40 bg-bull/10 px-3 py-2 text-sm text-bull">{notice}</div>
        )}

        <LlmSection />

        <Mt5Section onChanged={onChanged} />

        <section>
          <div className="mb-2 text-sm font-medium text-neutral-300">Execution mode</div>
          <div className="space-y-2">
            {MODES.map((m) => {
              const active = currentMode === m.value;
              return (
                <button
                  key={m.value}
                  disabled={busy}
                  onClick={() => chooseMode(m.value)}
                  className={`w-full rounded-md border px-3 py-2 text-left text-sm ${
                    active ? "border-blue-500 bg-blue-500/10" : "border-neutral-700 hover:bg-neutral-800"
                  }`}
                >
                  <div className="font-medium">{m.label}</div>
                  <div className="text-xs text-neutral-400">{m.desc}</div>
                </button>
              );
            })}
          </div>
        </section>

        <section>
          <div className="mb-2 text-sm font-medium text-neutral-300">Analysis language</div>
          {/* A setting, not a per-item translate button: analysis you have to ask to translate is
              analysis you stop reading. Chosen once, applied to every rationale, advisor note and
              summary. Numbers, levels and symbols stay as they are, and the buttons/labels stay
              English — a trader reads 25460.00 / XAUUSDm / 1.5R the same way in both languages. */}
          <div className="flex gap-2">
            {([
              { code: "en", label: "English", note: "Original — instant, no AI call" },
              { code: "ar", label: "العربية", note: "الشروحات بالعربية" },
            ] as const).map((opt) => {
              const active = (settings?.app.analysis_language ?? "en") === opt.code;
              return (
                <button
                  key={opt.code}
                  disabled={busy}
                  onClick={() => run(() => api.setAnalysisLanguage(opt.code),
                                     `Analysis language: ${opt.label}.`)}
                  className={`flex-1 rounded-md border px-3 py-2 text-left text-sm ${
                    active ? "border-blue-500 bg-blue-500/10" : "border-neutral-700 hover:bg-neutral-800"
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="font-medium" dir={opt.code === "ar" ? "rtl" : undefined}>
                      {opt.label}
                    </span>
                    {active && <span className="text-xs font-semibold text-bull">ON</span>}
                  </div>
                  <div className="text-xs text-neutral-400" dir={opt.code === "ar" ? "rtl" : undefined}>
                    {opt.note}
                  </div>
                </button>
              );
            })}
          </div>
          <div className="mt-1 text-[11px] text-neutral-500">
            Applies to the analysis text only — rationales, advisor notes and summaries. Prices,
            levels and the interface stay as they are.
          </div>
        </section>

        <section>
          <div className="mb-2 text-sm font-medium text-neutral-300">Strategy</div>
          <button
            disabled={busy}
            onClick={() =>
              run(() => api.setTrendOnly(!settings?.app.trend_only_mode),
                  `Trend-only mode ${settings?.app.trend_only_mode ? "OFF" : "ON"}.`)}
            className={`w-full rounded-md border px-3 py-2 text-left text-sm ${
              settings?.app.trend_only_mode
                ? "border-blue-500 bg-blue-500/10"
                : "border-neutral-700 hover:bg-neutral-800"
            }`}
          >
            <div className="flex items-center justify-between">
              <span className="font-medium">Trend-only mode</span>
              <span className={`text-xs font-semibold ${
                settings?.app.trend_only_mode ? "text-bull" : "text-neutral-400"
              }`}>
                {settings?.app.trend_only_mode ? "ON" : "OFF"}
              </span>
            </div>
            <div className="text-xs text-neutral-400">
              Trade only clear (ADX≥25) trends; stand aside in moderate / ranging / volatile.
              Backtests: same return, ~40% less drawdown.
            </div>
          </button>

          <button
            disabled={busy}
            onClick={() =>
              run(() => api.setAiMomentumRead(!settings?.app.ai_momentum_read),
                  `AI momentum-read ${settings?.app.ai_momentum_read ? "OFF" : "ON"}.`)}
            className={`mt-2 w-full rounded-md border px-3 py-2 text-left text-sm ${
              settings?.app.ai_momentum_read
                ? "border-blue-500 bg-blue-500/10"
                : "border-neutral-700 hover:bg-neutral-800"
            }`}
          >
            <div className="flex items-center justify-between">
              <span className="font-medium">AI momentum-read</span>
              <span className={`text-xs font-semibold ${
                settings?.app.ai_momentum_read ? "text-bull" : "text-neutral-400"
              }`}>
                {settings?.app.ai_momentum_read ? "ON" : "OFF"}
              </span>
            </div>
            <div className="text-xs text-neutral-400">
              At an ambiguous pullback (MACD rolling over / RSI stretched), the AI classifies it as a
              healthy pullback, weak momentum, or probable reversal — the deterministic engine then
              decides enter / wait / reject / arm. The AI only labels; it never overrides.
            </div>
          </button>

          <button
            disabled={busy}
            onClick={() =>
              run(() => api.setAiRegimeRead(!settings?.app.ai_regime_read),
                  `AI regime-read ${settings?.app.ai_regime_read ? "OFF" : "ON"}.`)}
            className={`mt-2 w-full rounded-md border px-3 py-2 text-left text-sm ${
              settings?.app.ai_regime_read
                ? "border-blue-500 bg-blue-500/10"
                : "border-neutral-700 hover:bg-neutral-800"
            }`}
          >
            <div className="flex items-center justify-between">
              <span className="font-medium">AI regime-read</span>
              <span className={`text-xs font-semibold ${
                settings?.app.ai_regime_read ? "text-bull" : "text-neutral-400"
              }`}>
                {settings?.app.ai_regime_read ? "ON" : "OFF"}
              </span>
            </div>
            <div className="text-xs text-neutral-400">
              Only at the grey zone between trend and range (moderate ADX), the AI reads the texture as an
              emerging trend, choppy range, or transition — the engine then treats it as a trend, a range,
              or stands pat. The AI only labels; every gate still runs.
            </div>
          </button>

          <button
            disabled={busy}
            onClick={() =>
              run(() => api.setAiPriceactionRead(!settings?.app.ai_priceaction_read),
                  `AI price-action-read ${settings?.app.ai_priceaction_read ? "OFF" : "ON"}.`)}
            className={`mt-2 w-full rounded-md border px-3 py-2 text-left text-sm ${
              settings?.app.ai_priceaction_read
                ? "border-blue-500 bg-blue-500/10"
                : "border-neutral-700 hover:bg-neutral-800"
            }`}
          >
            <div className="flex items-center justify-between">
              <span className="font-medium">AI price-action-read</span>
              <span className={`text-xs font-semibold ${
                settings?.app.ai_priceaction_read ? "text-bull" : "text-neutral-400"
              }`}>
                {settings?.app.ai_priceaction_read ? "ON" : "OFF"}
              </span>
            </div>
            <div className="text-xs text-neutral-400">
              When a major level sits in a trade's path, the AI reads whether price is likely to reject,
              break through, or is undecided — the engine then waits or takes the trade through the level.
              The AI only labels; it never overrides.
            </div>
          </button>
        </section>

        <section className="space-y-2">
          <div className="text-sm font-medium text-neutral-300">Live confirmation phrase</div>
          <input
            name="live-confirm-phrase"
            autoComplete="off"
            type="text"
            value={phrase}
            onChange={(e) => setPhrase(e.target.value)}
            placeholder="Required to enable live trading"
            className="w-full rounded bg-neutral-800 px-2 py-1.5 text-sm"
          />
          {isLive && reconfirm && (
            <button
              disabled={busy || !phrase.trim()}
              onClick={() => run(() => api.liveConfirm(phrase), "Live trading re-confirmed.")}
              className="btn bg-bear text-white hover:bg-red-700"
            >
              Re-confirm live trading (required after restart)
            </button>
          )}
          {isLive && (
            <button
              disabled={busy}
              onClick={() => run(() => api.setBrokerEnv("paper"), "Switched back to paper.")}
              className="btn ml-2 bg-neutral-700 text-white hover:bg-neutral-600"
            >
              Switch back to paper
            </button>
          )}
        </section>

        <section>
          <div className="mb-2 text-sm font-medium text-neutral-300">Broker per asset class</div>
          <div className="grid grid-cols-2 gap-2">
            {ASSET_CLASSES.map((ac) => (
              <label key={ac} className="text-sm">
                <div className="mb-1 text-xs text-neutral-400">{assetLabel(ac)}</div>
                <select
                  name={`broker-${ac}`}
                  disabled={busy}
                  value={settings?.app.broker_map?.[ac] ?? "sim"}
                  onChange={(e) =>
                    run(() => api.setBrokerMap({ [ac]: e.target.value }), `${ac} → ${e.target.value}`)
                  }
                  className="w-full rounded bg-neutral-800 px-2 py-1.5"
                >
                  {BROKERS.map((b) => (
                    <option key={b} value={b}>
                      {b}
                    </option>
                  ))}
                </select>
              </label>
            ))}
          </div>
          <p className="mt-1 text-xs text-neutral-500">
            Exness = <code>mt5</code> (needs the MT5 terminal running). Unconfigured brokers
            safely fall back to the sim paper broker.
          </p>
        </section>

        <section>
          <div className="mb-2 text-sm font-medium text-neutral-300">
            Risk limits (bounded by RISK.md)
          </div>
          {settings && (
            <>
              <div className="grid grid-cols-2 gap-2 text-xs text-neutral-400">
                <div>Per-trade risk: {(settings.risk.risk_per_trade * 100).toFixed(2)}%</div>
                <label className="flex items-center gap-1.5" title="How many positions can be open at once. Takes effect immediately.">
                  <span>Max open trades:</span>
                  <input
                    type="number" min={1} max={20} value={maxPos} disabled={busy}
                    onChange={(e) => setMaxPos(e.target.value)} onBlur={saveMaxPos}
                    onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
                    className="field w-14 px-1.5 py-0.5 text-right tabular-nums"
                  />
                </label>
                <div>Max daily loss: {(settings.risk.max_daily_loss * 100).toFixed(1)}%</div>
                <label className="flex items-center gap-1.5" title="Total risk-at-entry budget across all open trades. Must be big enough to fit the trade count.">
                  <span>Max exposure:</span>
                  <input
                    type="number" min={1} max={100} value={exposurePct} disabled={busy}
                    onChange={(e) => setExposurePct(e.target.value)} onBlur={saveExposure}
                    onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
                    className="field w-14 px-1.5 py-0.5 text-right tabular-nums"
                  />
                  <span>%</span>
                </label>
                <div>Per-pair cooldown: {settings.risk.per_pair_cooldown_minutes}m</div>
                <div>Loss cooldown: {settings.risk.loss_cooldown_minutes}m</div>
              </div>
              <p className="mt-1.5 text-[11px] text-neutral-500">
                The two are linked: to open N trades at {(settings.risk.risk_per_trade * 100).toFixed(0)}% risk
                each, Max exposure must be ≥ N × {(settings.risk.risk_per_trade * 100).toFixed(0)}%. E.g. 5
                trades → set Max exposure ≥ {(settings.risk.risk_per_trade * 5 * 100).toFixed(0)}%. Raising the
                trade count alone won't open more unless exposure allows it.
              </p>
            </>
          )}
        </section>

        <section className="border-t border-neutral-800 pt-3">
          <button
            disabled={busy}
            onClick={() => run(() => api.flatten(), "All positions flattened.")}
            className="btn border border-bear bg-bear/15 text-bear hover:bg-bear/25"
          >
            Flatten all positions
          </button>
        </section>
        </div>
      </div>
    </div>
  );
}

function LlmSection() {
  const [status, setStatus] = useState<LlmStatus | null>(null);
  const [provider, setProvider] = useState("anthropic");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .llmStatus()
      .then((s) => {
        setStatus(s);
        setProvider(s.provider);
        setModel(s.model);
      })
      .catch(() => {});
  }, []);

  const save = async () => {
    setBusy(true);
    try {
      const res = await api.setLlm({
        provider,
        model: model || LLM_DEFAULT_MODEL[provider],
        api_key: apiKey || undefined,
      });
      setStatus(res);
      setApiKey("");
    } catch (e) {
      setStatus({ provider, model, available: false, tested_ok: false,
                  error: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="rounded-md border border-neutral-800 p-3">
      <div className="mb-2 flex items-center justify-between">
        <div className="text-sm font-medium text-neutral-300">AI model (agents)</div>
        {status && (
          <span
            className={`rounded px-2 py-0.5 text-xs font-medium ${
              status.available ? "bg-bull/20 text-bull" : "bg-neutral-800 text-neutral-400"
            }`}
          >
            {status.available ? `${status.provider} · ${status.model}` : "no key"}
          </span>
        )}
      </div>
      {status?.tested_ok === false && status.error && (
        <div className="mb-2 text-xs text-bear">test failed: {status.error}</div>
      )}
      {status?.tested_ok && <div className="mb-2 text-xs text-bull">connection OK ✓</div>}

      <div className="grid grid-cols-2 gap-2">
        <label className="text-sm">
          <div className="mb-1 text-xs text-neutral-400">Provider</div>
          <select
            name="llm-provider"
            value={provider}
            onChange={(e) => {
              setProvider(e.target.value);
              setModel(LLM_DEFAULT_MODEL[e.target.value] ?? "");
            }}
            className="w-full rounded bg-neutral-800 px-2 py-1.5"
          >
            <option value="anthropic">Claude (Anthropic)</option>
            <option value="gemini">Gemini (Google)</option>
            <option value="openai">GPT (OpenAI)</option>
          </select>
        </label>
        <label className="text-sm">
          <div className="mb-1 text-xs text-neutral-400">Model</div>
          <input
            name="llm-model"
            autoComplete="off"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder={LLM_DEFAULT_MODEL[provider]}
            className="w-full rounded bg-neutral-800 px-2 py-1.5"
          />
        </label>
        <input
          name="llm-api-key"
          autoComplete="off"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          type="password"
          placeholder={`${provider} API key`}
          className="col-span-2 rounded bg-neutral-800 px-2 py-1.5 text-sm"
        />
      </div>
      <div className="mt-2">
        <button onClick={save} disabled={busy} className="btn btn-primary">
          {busy ? "Saving…" : "Save & test"}
        </button>
      </div>
      <p className="mt-1 text-xs text-neutral-500">
        Gemini: <code>gemini-2.5-flash</code> (fast/cheap). OpenAI: <code>gpt-5-mini</code>{" "}
        (fast/cheap) or <code>gpt-5</code> (max reasoning). Without a key, agents use the offline
        deterministic logic.
      </p>
    </section>
  );
}

function Mt5Section({ onChanged }: { onChanged: () => void }) {
  const [status, setStatus] = useState<Mt5Status | null>(null);
  const [server, setServer] = useState("");
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);

  const refreshStatus = () => {
    api
      .mt5Status()
      .then((s) => {
        setStatus(s);
        if (s.server) setServer(s.server);
        if (s.login) setLogin(String(s.login));
      })
      .catch(() => {});
  };

  useEffect(refreshStatus, []);

  const connect = async () => {
    setBusy(true);
    try {
      const res = await api.connectMt5({
        login: login ? Number(login) : undefined,
        password: password || undefined,
        server: server || undefined,
      });
      setStatus(res);
      setPassword(""); // never keep the password in the field
      if (res.connected) onChanged();
    } catch (e) {
      setStatus({ configured: true, connected: false, error: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="rounded-md border border-neutral-800 p-3">
      <div className="mb-2 flex items-center justify-between">
        <div className="text-sm font-medium text-neutral-300">Exness / MetaTrader 5</div>
        {status && (
          <span
            className={`rounded px-2 py-0.5 text-xs font-medium ${
              status.connected ? "bg-bull/20 text-bull" : "bg-neutral-800 text-neutral-400"
            }`}
          >
            {status.connected ? `connected · ${status.is_paper ? "demo/paper" : "LIVE"}` : "not connected"}
          </span>
        )}
      </div>

      {status?.connected && (
        <div className="mb-2 text-xs text-neutral-400">
          Account {status.login ?? "(terminal session)"} on {status.server ?? "—"} · equity $
          {status.equity?.toLocaleString()} · {status.open_positions ?? 0} open
        </div>
      )}
      {status && !status.connected && status.error && (
        <div className="mb-2 text-xs text-bear">{status.error}</div>
      )}

      <p className="mb-2 text-xs text-neutral-500">
        Make sure the MT5 desktop terminal is running, logged into Exness, with “Algo Trading”
        enabled. Leave login/password blank to use the terminal’s current account.
      </p>

      <div className="grid grid-cols-3 gap-2">
        <input
          name="mt5-server"
          autoComplete="off"
          value={server}
          onChange={(e) => setServer(e.target.value)}
          placeholder="Server (e.g. Exness-MT5Trial)"
          className="col-span-3 rounded bg-neutral-800 px-2 py-1.5 text-sm"
        />
        <input
          name="mt5-login"
          autoComplete="off"
          value={login}
          onChange={(e) => setLogin(e.target.value)}
          placeholder="Login (optional)"
          inputMode="numeric"
          className="rounded bg-neutral-800 px-2 py-1.5 text-sm"
        />
        <input
          name="mt5-password"
          autoComplete="new-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="Password (optional)"
          type="password"
          className="col-span-2 rounded bg-neutral-800 px-2 py-1.5 text-sm"
        />
      </div>
      <div className="mt-2 flex gap-2">
        <button onClick={connect} disabled={busy} className="btn btn-primary">
          {busy ? "Connecting…" : "Connect MT5"}
        </button>
        <button onClick={refreshStatus} disabled={busy} className="btn bg-neutral-700 text-white hover:bg-neutral-600">
          Recheck
        </button>
      </div>
    </section>
  );
}
