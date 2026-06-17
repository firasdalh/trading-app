import { useState } from "react";
import { api } from "../api/client";
import { fmtPrice } from "../format";
import type { ConditionalSuggestion } from "../types";

type Lang = "en" | "ar";

// A comprehensive, bilingual explanation of WHY this is a buy/sell stop (a 'wait for the break'
// order) — generated deterministically from the suggestion, so it's instant, free, and always
// matches the engine's logic (no LLM call needed for fixed reasoning).
function explain(c: ConditionalSuggestion, lang: Lang) {
  const isBuy = c.order_type.startsWith("buy");
  const isStop = c.order_type.endsWith("stop");
  const dir = isBuy ? "long" : "short";
  const trg = fmtPrice(c.trigger_price);
  const sl = fmtPrice(c.stop_loss);
  const tp = fmtPrice(c.take_profit);
  const rr = c.rr.toFixed(1);

  if (lang === "ar") {
    const noun = isBuy ? "شراء" : "بيع";
    const beyond = isBuy ? "فوق" : "تحت";
    const level = isBuy ? "مقاومة" : "دعم";
    const move = isStop ? "كسر" : "ارتداد";
    return {
      title: `أمر ${isStop ? "إيقاف" : "محدّد"} ${noun} — انتظر ${isStop ? "الكسر" : "الارتداد"}`,
      sections: [
        ["ما هذا؟", `أمر ${noun} معلّق عند ${trg}. لا ينفّذ شيئًا حتى يصل السعر ${beyond} ${trg}.`],
        ["لماذا ليس الآن؟", `الصفقة سليمة مع الاتجاه، لكن يوجد مستوى ${level} بين الدخول الحالي والهدف يسدّ الطريق — الدخول الآن يعني مطاردة داخل هذا الحاجز. لذلك ننتظر ${move} المستوى؛ وعندها ينفتح الطريق إلى ${tp} وتصبح نسبة ~${rr}R حقيقية.`],
        ["ماذا يحدث عند التحفيز؟", `عند إغلاق شمعة الساعة ${beyond} ${trg}، يعيد النظام التحليل الكامل (الفحص المزدوج: ثقة + فيتو ذكاء + تحجيم مخاطرة جديد) ويفتح الصفقة فقط إن بقيت صالحة في تلك اللحظة — وإلا تُلغى.`],
        ["المستويات", `الدخول ${trg} · الوقف ${sl} (خلف المستوى المكسور) · الهدف ${tp} · ~${rr}R.`],
        ["المخاطرة", `التسليح لا يخاطر بشيء — لا يوجد أمر فعّال ولا هامش مستخدَم حتى التحفيز. وكل حدود RISK.md تُطبَّق عند الفتح.`],
      ] as [string, string][],
    };
  }

  const side = c.order_type.replace("_", " ");
  const beyond = isBuy ? "above" : "below";
  const level = isBuy ? "resistance" : "support";
  return {
    title: `${side} — wait for the ${isStop ? "break" : "pullback"}`,
    sections: [
      ["What it is", `A pending ${side} at ${trg}. It does nothing until price ${isStop ? "breaks" : "pulls back"} ${beyond} ${trg}.`],
      ["Why not now", `The ${dir} is valid with the trend, but a ${level} level sits between the current entry and the target, blocking a clean path — entering now would be chasing into that wall. We wait for price to ${isStop ? "break" : "reach"} it; once it gives way the road to ${tp} opens and the ~${rr}R becomes real.`],
      ["At the trigger", `When an hourly candle closes ${beyond} ${trg}, the system re-runs the full analysis (the double-check: fresh confidence + AI veto + risk sizing) and opens the ${dir} only if it still qualifies at that moment — otherwise it's dropped.`],
      ["Levels", `Entry ${trg} · Stop ${sl} (just beyond the broken level) · Target ${tp} · ~${rr}R.`],
      ["Risk", `Arming risks nothing — no order is live and no margin is used until the trigger. All RISK.md limits apply when it opens.`],
    ] as [string, string][],
  };
}

// Arms a conditional ('wait for the break') setup from an engine suggestion, with a comprehensive
// bilingual explanation of why it's a buy/sell stop. Arming never opens a trade — the system
// re-checks at the trigger and only then opens it / queues it for approval.
export function ArmSetupButton({ symbol, assetClass, timeframe, conditional, onArmed }: {
  symbol: string;
  assetClass: string;
  timeframe: string;
  conditional: ConditionalSuggestion;
  onArmed?: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [lang, setLang] = useState<Lang>("en");
  const direction = conditional.order_type.startsWith("buy") ? "long" : "short";

  const arm = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.armConditional({
        symbol, asset_class: assetClass, timeframe, direction,
        order_type: conditional.order_type, trigger_price: conditional.trigger_price,
        stop_loss: conditional.stop_loss, take_profit: conditional.take_profit,
        confidence: conditional.confidence, rr: conditional.rr, reason: conditional.reason,
      });
      setDone(true);
      onArmed?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const e = explain(conditional, lang);
  const rtl = lang === "ar";

  return (
    <div className="flex flex-col gap-1">
      {done ? (
        <div className="text-xs text-amber-300">⚡ Armed — waiting for the break</div>
      ) : (
        <>
          <button
            onClick={arm}
            disabled={busy}
            title={conditional.reason}
            className="btn self-start bg-amber-600/20 text-xs text-amber-300 hover:bg-amber-600/30"
          >
            {busy
              ? "Arming…"
              : `⚡ Arm ${conditional.order_type.replace("_", " ")} @ ${fmtPrice(conditional.trigger_price)} (~${conditional.rr.toFixed(1)}R)`}
          </button>
          {error && <span className="text-xs text-bear">{error}</span>}
        </>
      )}

      <button
        onClick={() => setOpen((o) => !o)}
        className="self-start text-[11px] text-neutral-500 hover:text-neutral-300"
      >
        {open ? "▾" : "▸"} Why this order? · لماذا هذا الأمر؟
      </button>

      {open && (
        <div className="rounded-md border border-neutral-800 bg-neutral-900/60 p-2.5">
          <div className="mb-2 flex items-center gap-2">
            <div className="flex overflow-hidden rounded border border-neutral-700 text-xs">
              <button onClick={() => setLang("en")}
                      className={`px-2 py-0.5 ${lang === "en" ? "bg-blue-600 text-white" : "text-neutral-400 hover:text-neutral-200"}`}>
                EN
              </button>
              <button onClick={() => setLang("ar")}
                      className={`px-2 py-0.5 ${lang === "ar" ? "bg-blue-600 text-white" : "text-neutral-400 hover:text-neutral-200"}`}>
                عربي
              </button>
            </div>
          </div>
          <div dir={rtl ? "rtl" : "ltr"} className={`space-y-1.5 ${rtl ? "text-right" : ""}`}>
            <div className="text-sm font-semibold text-amber-300">{e.title}</div>
            {e.sections.map(([label, body]) => (
              <div key={label}>
                <div className="text-xs font-semibold text-neutral-400">{label}</div>
                <p className="text-sm text-neutral-200">{body}</p>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
