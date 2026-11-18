"use client";

import {
  AreaSeries,
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef, useState } from "react";

import { api } from "@/lib/api";
import { price } from "@/lib/format";
import type { Chain, ChartData, ChartInterval } from "@/lib/types";

const INTERVALS: ChartInterval[] = ["1m", "5m", "15m", "1h", "4h", "1d"];
const BULL = "#10b981";
const BEAR = "#f43f5e";

export function PriceChart({ chain, address }: { chain: Chain; address: string }) {
  const el = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const lineRef = useRef<ISeriesApi<"Area"> | null>(null);
  const [interval, setIntervalValue] = useState<ChartInterval>("5m");
  const [data, setData] = useState<ChartData | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Create chart once.
  useEffect(() => {
    if (!el.current) return;
    const chart = createChart(el.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#8b93a7",
        fontFamily: getComputedStyle(document.body).getPropertyValue("--font-jetbrains") || "monospace",
        fontSize: 11,
        attributionLogo: false,
      },
      grid: { vertLines: { color: "rgba(255,255,255,0.035)" }, horzLines: { color: "rgba(255,255,255,0.035)" } },
      rightPriceScale: { borderColor: "rgba(255,255,255,0.08)", scaleMargins: { top: 0.1, bottom: 0.25 } },
      timeScale: { borderColor: "rgba(255,255,255,0.08)", timeVisible: true, secondsVisible: false },
      crosshair: { mode: CrosshairMode.Normal },
      localization: { priceFormatter: (p: number) => price(p) },
    });
    const candles = chart.addSeries(CandlestickSeries, {
      upColor: BULL,
      downColor: BEAR,
      wickUpColor: BULL,
      wickDownColor: BEAR,
      borderVisible: false,
      priceFormat: { type: "custom", formatter: (p: number) => price(p), minMove: 1e-12 },
    });
    // Snapshot fallback has one price per scan cycle: an area line reads better than flat candles.
    const line = chart.addSeries(AreaSeries, {
      lineColor: BULL,
      lineWidth: 2,
      topColor: "rgba(16,185,129,0.25)",
      bottomColor: "rgba(16,185,129,0)",
      priceFormat: { type: "custom", formatter: (p: number) => price(p), minMove: 1e-12 },
      visible: false,
    });
    const volume = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "vol",
      lastValueVisible: false,
      priceLineVisible: false,
    });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 }, visible: false });
    chartRef.current = chart;
    candleRef.current = candles;
    lineRef.current = line;
    volRef.current = volume;
    return () => {
      chart.remove();
      chartRef.current = null;
    };
  }, []);

  // Load data on token / interval change, refresh every 60s.
  useEffect(() => {
    const ctl = new AbortController();
    let timer: ReturnType<typeof setInterval>;
    const load = async () => {
      try {
        const d = await api.chart(chain, address, interval, ctl.signal);
        setData(d);
        setError(null);
        const ohlc = d.source === "geckoterminal";
        candleRef.current?.applyOptions({ visible: ohlc });
        lineRef.current?.applyOptions({ visible: !ohlc });
        volRef.current?.applyOptions({ visible: ohlc });
        candleRef.current?.setData(
          ohlc
            ? d.candles.map((c) => ({ time: c.time as UTCTimestamp, open: c.open, high: c.high, low: c.low, close: c.close }))
            : [],
        );
        lineRef.current?.setData(ohlc ? [] : d.candles.map((c) => ({ time: c.time as UTCTimestamp, value: c.close })));
        volRef.current?.setData(
          ohlc
            ? d.candles.map((c) => ({
                time: c.time as UTCTimestamp,
                value: c.volume,
                color: c.close >= c.open ? "rgba(16,185,129,0.35)" : "rgba(244,63,94,0.35)",
              }))
            : [],
        );
        chartRef.current?.timeScale().fitContent();
      } catch (e) {
        if ((e as Error).name !== "AbortError") setError("Chart unavailable");
      }
    };
    setData(null);
    load();
    timer = setInterval(load, 60_000);
    return () => {
      ctl.abort();
      clearInterval(timer);
    };
  }, [chain, address, interval]);

  const empty = data && data.candles.length === 0;

  return (
    <div className="card overflow-hidden">
      <div className="flex items-center justify-between border-b border-line px-3 py-2">
        <div className="flex gap-0.5" role="tablist" aria-label="Chart interval">
          {INTERVALS.map((iv) => (
            <button
              key={iv}
              role="tab"
              aria-selected={iv === interval}
              onClick={() => setIntervalValue(iv)}
              className={`num rounded px-2 py-0.5 text-[11px] transition-colors ${
                iv === interval ? "bg-raised text-ink" : "text-faint hover:text-ink"
              }`}
            >
              {iv}
            </button>
          ))}
        </div>
        <span className="text-[10px] text-faint">
          {data?.source === "snapshots" ? "Scanner snapshots (GeckoTerminal busy)" : data ? "GeckoTerminal" : ""}
        </span>
      </div>
      <div className="relative h-[260px]">
        <div ref={el} className="absolute inset-0" />
        {(!data || empty || error) && (
          <div className="absolute inset-0 grid place-items-center text-xs text-faint">
            {error ?? (empty ? "No price history yet" : <span className="skeleton h-full w-full rounded-none" />)}
          </div>
        )}
      </div>
    </div>
  );
}
