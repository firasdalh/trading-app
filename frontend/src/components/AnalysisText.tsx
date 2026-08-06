import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";

// Analysis prose rendered in the user's chosen language.
//
// The language is a SETTING, picked once, not a per-item "translate" button: analysis you have to
// ask to translate is analysis you stop reading. So when Arabic is selected every rationale, advisor
// note and hybrid summary arrives in Arabic on its own.
//
// English is free — it is the source language, so it renders instantly with no request at all.
// Arabic costs one small model call per distinct string, which is why the results are cached
// module-wide: the same note appears in several panels and across re-renders, and the poll loops
// would otherwise re-translate identical text every few seconds.
//
// While a translation is in flight the ENGLISH text stays on screen, faintly dimmed. Showing a
// spinner instead would hide information the trader may be acting on; a slightly faded sentence they
// can already read is strictly better than an empty box.

const cache = new Map<string, string>();
const inflight = new Map<string, Promise<string | null>>();

async function translate(text: string): Promise<string | null> {
  const hit = cache.get(text);
  if (hit !== undefined) return hit;
  const running = inflight.get(text);
  if (running) return running;                  // dedupe: several panels can mount the same note
  const req = api
    .translateAnalysis(text, "ar")
    .then((r) => {
      const out = (r?.text || "").trim();
      if (out) cache.set(text, out);
      return out || null;
    })
    .catch(() => null)                          // no model / offline -> silently keep English
    .finally(() => inflight.delete(text));
  inflight.set(text, req);
  return req;
}

export function AnalysisText({
  text,
  lang,
  className = "",
  as: Tag = "span",
}: {
  text: string | null | undefined;
  lang: string | undefined;          // "ar" | "en" — from settings.app.analysis_language
  className?: string;
  as?: "span" | "div" | "p";
}) {
  const source = (text ?? "").trim();
  const wantArabic = lang === "ar" && source.length > 0;
  const [translated, setTranslated] = useState<string | null>(
    wantArabic ? cache.get(source) ?? null : null,
  );
  const [pending, setPending] = useState(false);
  const latest = useRef(source);

  useEffect(() => {
    latest.current = source;
    if (!wantArabic) {
      setTranslated(null);
      return;
    }
    const cached = cache.get(source);
    if (cached !== undefined) {
      setTranslated(cached);
      return;
    }
    setPending(true);
    void translate(source).then((out) => {
      // Guard against the text changing while the request was in flight — a stale translation
      // attached to a NEW rationale would be worse than no translation at all.
      if (latest.current !== source) return;
      setTranslated(out);
      setPending(false);
    });
  }, [source, wantArabic]);

  if (!source) return null;
  const showArabic = wantArabic && translated;
  return (
    <Tag
      className={`${className}${pending && wantArabic ? " opacity-60" : ""}`}
      dir={showArabic ? "rtl" : undefined}
      lang={showArabic ? "ar" : undefined}
    >
      {showArabic ? translated : source}
    </Tag>
  );
}
