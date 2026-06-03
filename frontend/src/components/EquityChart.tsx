import { useEffect, useRef } from "react";
import { ColorType, createChart, IChartApi, ISeriesApi, UTCTimestamp } from "lightweight-charts";

interface Props {
  points: { ts: string; equity: number }[];
}

// Simple area chart of the backtest equity curve.
export function EquityChart({ points }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Area"> | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: "#a3a3a3" },
      grid: { vertLines: { color: "#262626" }, horzLines: { color: "#262626" } },
      timeScale: { timeVisible: false, borderColor: "#404040" },
      rightPriceScale: { borderColor: "#404040" },
    });
    const series = chart.addAreaSeries({
      lineColor: "#3b82f6",
      topColor: "rgba(59,130,246,0.4)",
      bottomColor: "rgba(59,130,246,0.02)",
      lineWidth: 2,
    });
    chartRef.current = chart;
    seriesRef.current = series;
    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!seriesRef.current) return;
    // De-dupe identical timestamps (lightweight-charts requires strictly ascending unique time).
    const seen = new Set<number>();
    const data = points
      .map((p) => ({ time: Math.floor(Date.parse(p.ts) / 1000) as UTCTimestamp, value: p.equity }))
      .filter((d) => {
        if (seen.has(d.time)) return false;
        seen.add(d.time);
        return true;
      });
    seriesRef.current.setData(data);
    chartRef.current?.timeScale().fitContent();
  }, [points]);

  return <div ref={containerRef} className="h-[300px] w-full" />;
}
