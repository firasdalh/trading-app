import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { ChartWindow } from "./components/ChartWindow";
import { ErrorBoundary } from "./components/ErrorBoundary";
import "./index.css";
// ?chart=SYMBOL is a chart window (one pair's chart alone in a tab), not the desk: no header, no
// book, and none of the desk's watchers - see ChartWindow.
const chartWindow = new URLSearchParams(window.location.search).has("chart");

// The last line of defence: a page that still says what broke, instead of a blank one.
const crashed = (error: Error) => (
  <div className="m-6 max-w-xl rounded-lg border border-bear/50 bg-bear/10 p-4 text-sm text-neutral-200">
    <div className="font-bold text-bear">The desk hit an error and stopped drawing.</div>
    <div className="mt-1 font-mono text-xs text-neutral-400">{error.message}</div>
    <div className="mt-2 text-neutral-400">
      Open trades keep their stops and targets at the broker, and the backend keeps running. Reload to get the desk back.
    </div>
    <button onClick={() => window.location.reload()} className="mt-3 rounded border border-neutral-600 px-3 py-1 hover:bg-neutral-800">
      Reload
    </button>
  </div>
);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ErrorBoundary name="desk" fallback={crashed}>
      {chartWindow ? <ChartWindow /> : <App />}
    </ErrorBoundary>
  </React.StrictMode>,
);
