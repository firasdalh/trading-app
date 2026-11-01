import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { ChartWindow } from "./components/ChartWindow";
import "./index.css";

// ?chart=SYMBOL is a chart window (one pair's chart alone in a tab), not the desk: no header, no
// book, and none of the desk's watchers - see ChartWindow.
const chartWindow = new URLSearchParams(window.location.search).has("chart");

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {chartWindow ? <ChartWindow /> : <App />}
  </React.StrictMode>,
);
