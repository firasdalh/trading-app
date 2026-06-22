import { useEffect, useState, type ReactNode } from "react";
import { api } from "../api/client";
import type { ExplainedReview } from "../types";

// Bilingual section labels (the AI returns the CONTENT in the chosen language; labels are fixed).
const L = {
  en: { explain: "Explain simply", decision: "Decision", main: "Main reason", pros: "In favour",
        cons: "Against", conclusion: "Conclusion", grade: "Grade", loading: "Thinking…",
        error: "Could not generate the explanation." },
  ar: { explain: "العربية", decision: "القرار", main: "السبب الرئيسي", pros: "الإيجابيات",
        cons: "السلبيات", conclusion: "الخلاصة", grade: "التقييم", loading: "جارٍ التحليل…",
        error: "تعذّر توليد الشرح." },
} as const;

type Lang = "en" | "ar";

// A collapsible, structured (Decision / main reason / pros / cons / conclusion) re-rendering of a
// raw AI-review rationale, with an English↔Arabic toggle. On-demand + cached per language, so it
// costs nothing until the trader asks for it and is instant when toggling back.
export function ReviewExplanation({ rationale }: { rationale: string }) {
  const [open, setOpen] = useState(false);
  const [lang, setLang] = useState<Lang>("en");
  const [cache, setCache] = useState<Partial<Record<Lang, ExplainedReview>>>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Invalidate cached explanations whenever the underlying proposal (rationale) changes — otherwise
  // the EN and AR caches could be built from DIFFERENT proposals and contradict each other (one
  // says SHORT, the other LONG). If the panel is open, re-fetch the shown language for the new text.
  useEffect(() => {
    setCache({});
    setError(null);
    if (!open || !rationale?.trim()) return;
    let cancelled = false;
    setLoading(true);
    api.explainReview(rationale, lang)
      .then((res) => { if (!cancelled) setCache({ [lang]: res }); })
      .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : String(e)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rationale]);

  if (!rationale?.trim()) return null;

  const load = async (l: Lang) => {
    if (cache[l]) return;
    setLoading(true);
    setError(null);
    try {
      const res = await api.explainReview(rationale, l);
      setCache((c) => ({ ...c, [l]: res }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  const openWith = (l: Lang) => {
    setLang(l);
    setOpen(true);
    void load(l);
  };

  if (!open) {
    return (
      <div className="flex flex-wrap gap-2">
        <button onClick={() => openWith("en")}
                className="btn bg-neutral-800 text-xs text-neutral-300 hover:bg-neutral-700">
          🧠 {L.en.explain}
        </button>
        <button onClick={() => openWith("ar")}
                className="btn bg-neutral-800 text-xs text-neutral-300 hover:bg-neutral-700">
          🌐 {L.ar.explain}
        </button>
      </div>
    );
  }

  const t = L[lang];
  const rtl = lang === "ar";
  const data = cache[lang];

  return (
    <div className="rounded-md border border-neutral-800 bg-neutral-900/60 p-3">
      <div className="mb-2 flex items-center gap-2">
        <div className="flex overflow-hidden rounded border border-neutral-700 text-xs">
          <button onClick={() => openWith("en")}
                  className={`px-2 py-0.5 ${lang === "en" ? "bg-blue-600 text-white" : "text-neutral-400 hover:text-neutral-200"}`}>
            EN
          </button>
          <button onClick={() => openWith("ar")}
                  className={`px-2 py-0.5 ${lang === "ar" ? "bg-blue-600 text-white" : "text-neutral-400 hover:text-neutral-200"}`}>
            عربي
          </button>
        </div>
        <button onClick={() => setOpen(false)} className="ml-auto text-xs text-neutral-500 hover:text-neutral-300">
          ✕
        </button>
      </div>

      {loading && <div className="text-sm text-neutral-400">{t.loading}</div>}
      {error && <div className="text-sm text-bear">{t.error}</div>}

      {data && (
        <div dir={rtl ? "rtl" : "ltr"} className={`space-y-2 ${rtl ? "text-right" : ""}`}>
          <div className="flex flex-wrap items-center gap-2">
            <span className={`text-sm font-bold ${data.is_trade ? "text-bull" : "text-bear"}`}>
              {data.is_trade ? "🟢" : "🔴"} {data.decision}
            </span>
            {data.grade && (
              <span className="rounded bg-neutral-800 px-1.5 py-0.5 text-[10px] text-neutral-300">
                {t.grade} {data.grade}
              </span>
            )}
          </div>

          <Section title={t.main}>{data.main_reason}</Section>

          {data.pros.length > 0 && (
            <div>
              <div className="text-xs font-semibold text-neutral-400">{t.pros}</div>
              <ul className="space-y-0.5 text-sm text-neutral-300">
                {data.pros.map((p, i) => <li key={i}>✅ {p}</li>)}
              </ul>
            </div>
          )}

          {data.cons.length > 0 && (
            <div>
              <div className="text-xs font-semibold text-neutral-400">{t.cons}</div>
              <ul className="space-y-0.5 text-sm text-neutral-300">
                {data.cons.map((c, i) => <li key={i}>❌ {c}</li>)}
              </ul>
            </div>
          )}

          <Section title={t.conclusion}>{data.conclusion}</Section>
        </div>
      )}
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <div className="text-xs font-semibold text-neutral-400">{title}</div>
      <p className="text-sm text-neutral-200">{children}</p>
    </div>
  );
}
