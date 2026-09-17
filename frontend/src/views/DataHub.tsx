/**
 * views/DataHub.tsx
 * ───────────────────
 * The Unified Data Tab — the single hub for browsing and selectively
 * fetching all company-level raw data, plus a YouTube channel insights
 * sub-tab and a lightweight market-overview sub-tab.
 *
 * Sub-tabs:
 *   1. Company Data   — Fetch Control Panel + a viewer strip (Financials,
 *                        Other Filings, News, Earnings, Price Chart)
 *   2. YouTube Insights — company + macro/industry video feeds
 *   3. Market Overview  — market-wide sentiment gauge + macro news
 *
 * Replaces the old Dashboard / Company Media / Macro Sentiment views. Manual
 * PDF upload (UploadModal) is triggered from here rather than the Header.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { EarningsResponse, FilingMeta, NewsRange } from "../types";
import { useDashboard } from "../context/DashboardContext";
import { useAsync } from "../hooks/useAsync";
import {
  getCompanyNews, getCompanyVideos, getEarnings, getMacroNews, getMacroVideos,
  getMarketSentiment, getCachedPrice,
} from "../api";
import UpperPane from "../components/UpperPane";
import LowerPane from "../components/LowerPane";
import UploadModal from "../components/UploadModal";
import FetchControlPanel from "../components/data/FetchControlPanel";
import OtherFilingsView from "../components/data/OtherFilingsView";
import NewsFeed from "../components/media/NewsFeed";
import VideoList from "../components/media/VideoList";
import MediaNotice from "../components/media/MediaNotice";
import EarningsTranscript from "../components/media/EarningsTranscript";
import SentimentDashboard from "../components/media/SentimentDashboard";
import DateRangeSelector, { defaultRange } from "../components/media/DateRangeSelector";
import ChannelBar from "../components/media/ChannelBar";
import QuarterSelector, { defaultQuarter, type QuarterValue } from "../components/media/QuarterSelector";

type SubTab = "company" | "youtube" | "market";
type ViewerTab = "financials" | "other" | "news" | "earnings" | "price";

function isoDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}

function defaultPeriod(): { start: string; end: string } {
  const end = new Date();
  const start = new Date();
  start.setDate(start.getDate() - 365);
  return { start: isoDate(start), end: isoDate(end) };
}

// =============================================================================
// Financials viewer (resizable UpperPane/LowerPane split)
// =============================================================================

function FinancialsPanel({ ticker, refreshKey, periods }: {
  ticker: string | null; refreshKey: number; periods: ReturnType<typeof useDashboard>["periods"];
}) {
  const [splitRatio, setSplitRatio] = useState(50);
  const [isDragging, setIsDragging] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  const handleMouseDown = useCallback(() => setIsDragging(true), []);
  const handleMouseMove = useCallback(
    (e: MouseEvent) => {
      if (!isDragging || !containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      const ratio = ((e.clientY - rect.top) / rect.height) * 100;
      setSplitRatio(Math.min(80, Math.max(20, ratio)));
    },
    [isDragging]
  );
  const handleMouseUp = useCallback(() => setIsDragging(false), []);

  useEffect(() => {
    if (isDragging) {
      window.addEventListener("mousemove", handleMouseMove);
      window.addEventListener("mouseup", handleMouseUp);
    }
    return () => {
      window.removeEventListener("mousemove", handleMouseMove);
      window.removeEventListener("mouseup", handleMouseUp);
    };
  }, [isDragging, handleMouseMove, handleMouseUp]);

  return (
    <div
      ref={containerRef}
      className={`split-container ${isDragging ? "split-dragging" : ""}`}
    >
      <div style={{ height: `${splitRatio}%` }}>
        <UpperPane refreshKey={refreshKey} ticker={ticker} />
      </div>
      <div className="split-handle" onMouseDown={handleMouseDown}>
        <div className="split-handle-bar" />
      </div>
      <div style={{ height: `${100 - splitRatio}%` }}>
        <LowerPane periods={periods} ticker={ticker} />
      </div>
    </div>
  );
}

// =============================================================================
// News viewer
// =============================================================================

function NewsPanel({ ticker, refreshKey }: { ticker: string; refreshKey: number }) {
  const [range, setRange] = useState<NewsRange>(defaultRange(30));
  const news = useAsync(
    () => getCompanyNews(ticker, range),
    [ticker, refreshKey, JSON.stringify(range)]
  );
  return (
    <>
      <div className="range-bar">
        <span className="range-bar-label">Range</span>
        <DateRangeSelector value={range} onChange={setRange} />
      </div>
      <NewsFeed data={news.data} loading={news.loading} error={news.error} />
    </>
  );
}

// =============================================================================
// Earnings viewer
// =============================================================================

function EarningsSection({ data, loading, error }: {
  data: EarningsResponse | null; loading: boolean; error: string | null;
}) {
  if (loading) return <MediaNotice variant="loading" message="Fetching earnings-call transcript…" />;
  if (error) return <MediaNotice variant="error" message={error} />;
  if (!data) return null;
  if (!data.configured) {
    return (
      <MediaNotice
        title="Earnings not configured"
        message={data.message ?? "Set TAVILY_API_KEY on the backend to fetch earnings-call transcripts."}
      />
    );
  }
  if (!data.found || !data.transcript) {
    return (
      <MediaNotice icon="📭" message={data.message ?? `No earnings-call transcript found for Q${data.quarter} ${data.year}.`} />
    );
  }
  return <EarningsTranscript data={data} />;
}

function EarningsPanel({ ticker, refreshKey }: { ticker: string; refreshKey: number }) {
  const [quarter, setQuarter] = useState<QuarterValue>(defaultQuarter());
  const earnings = useAsync(
    () => getEarnings(ticker, quarter.year, quarter.quarter),
    [ticker, refreshKey, quarter.year, quarter.quarter]
  );
  return (
    <>
      <div className="range-bar">
        <span className="range-bar-label">Quarter</span>
        <QuarterSelector value={quarter} onChange={setQuarter} />
      </div>
      <EarningsSection data={earnings.data} loading={earnings.loading} error={earnings.error} />
    </>
  );
}

// =============================================================================
// Price viewer — a simple stat panel + monthly-close sparkline from the
// cached TechnicalData (no live fetch; the Fetch Control Panel populates it).
// =============================================================================

interface CachedTechnical {
  current_price?: number; period_high?: number; period_low?: number; period_return?: number;
  sma_50?: number | null; sma_200?: number | null; rsi_14?: number | null;
  golden_cross?: boolean; price_vs_sma50?: string; price_vs_sma200?: string;
  monthly_closes?: { month: string; close: number }[];
  period_start?: string; period_end?: string;
}

function Sparkline({ points }: { points: { month: string; close: number }[] }) {
  if (points.length < 2) return null;
  const w = 600, h = 120, pad = 8;
  const values = points.map((p) => p.close);
  const min = Math.min(...values), max = Math.max(...values);
  const span = max - min || 1;
  const step = (w - pad * 2) / (points.length - 1);
  const path = points
    .map((p, i) => {
      const x = pad + i * step;
      const y = h - pad - ((p.close - min) / span) * (h - pad * 2);
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="price-sparkline" preserveAspectRatio="none">
      <path d={path} fill="none" stroke="currentColor" strokeWidth={2} />
    </svg>
  );
}

function PricePanel({ ticker, refreshKey }: { ticker: string; refreshKey: number }) {
  const cached = useAsync(() => getCachedPrice(ticker), [ticker, refreshKey]);

  if (cached.loading) return <MediaNotice variant="loading" message="Loading price data…" />;
  if (cached.error) return <MediaNotice variant="error" message={cached.error} />;
  const data = cached.data?.data as CachedTechnical | null | undefined;
  if (!data) {
    return (
      <MediaNotice icon="📈" message="No price data cached yet — check 'Price & Technicals' above and click Fetch Selected." />
    );
  }

  return (
    <div className="price-panel">
      <div className="price-stats-grid">
        <div className="price-stat"><span className="price-stat-label">Current</span><span className="price-stat-value">${data.current_price?.toFixed(2) ?? "—"}</span></div>
        <div className="price-stat"><span className="price-stat-label">Period Return</span><span className="price-stat-value">{data.period_return != null ? `${(data.period_return * 100).toFixed(1)}%` : "—"}</span></div>
        <div className="price-stat"><span className="price-stat-label">SMA 50</span><span className="price-stat-value">{data.sma_50?.toFixed(2) ?? "—"} ({data.price_vs_sma50 ?? "n/a"})</span></div>
        <div className="price-stat"><span className="price-stat-label">SMA 200</span><span className="price-stat-value">{data.sma_200?.toFixed(2) ?? "—"} ({data.price_vs_sma200 ?? "n/a"})</span></div>
        <div className="price-stat"><span className="price-stat-label">RSI (14)</span><span className="price-stat-value">{data.rsi_14?.toFixed(1) ?? "—"}</span></div>
        <div className="price-stat"><span className="price-stat-label">Golden Cross</span><span className="price-stat-value">{data.golden_cross ? "Yes" : "No"}</span></div>
      </div>
      {data.monthly_closes && data.monthly_closes.length > 1 && (
        <div className="price-chart-block">
          <Sparkline points={data.monthly_closes} />
          <div className="price-chart-range">
            <span>{data.monthly_closes[0].month}</span>
            <span>{data.monthly_closes[data.monthly_closes.length - 1].month}</span>
          </div>
        </div>
      )}
      <p className="price-panel-note">
        Cached window: {data.period_start ?? "?"} → {data.period_end ?? "?"}
      </p>
    </div>
  );
}

// =============================================================================
// Sub-tab 1: Company Data
// =============================================================================

const VIEWER_TABS: { id: ViewerTab; label: string }[] = [
  { id: "financials", label: "📄 Financials" },
  { id: "other", label: "📋 Other Filings" },
  { id: "news", label: "📰 News" },
  { id: "earnings", label: "📞 Earnings" },
  { id: "price", label: "📈 Price Chart" },
];

function CompanyDataPanel({ onUploadComplete, onDataChanged }: {
  onUploadComplete: (filings: FilingMeta[]) => void;
  onDataChanged: () => void;
}) {
  const { activeTicker, periods, refreshKey, company } = useDashboard();
  const [period] = useState(defaultPeriod);
  const [startDate, setStartDate] = useState(period.start);
  const [endDate, setEndDate] = useState(period.end);
  const [viewerTab, setViewerTab] = useState<ViewerTab>("financials");
  const [uploadOpen, setUploadOpen] = useState(false);

  const companyLabel = company
    ? `${company.name ?? "Unknown company"}${company.ticker ? ` (${company.ticker})` : ""}`
    : activeTicker;

  return (
    <div className="data-company-panel">
      <div className="data-hub-toolbar">
        <div className="data-hub-company">
          {companyLabel ?? <span className="view-subtitle-muted">No company selected</span>}
        </div>
        <div className="data-hub-period">
          <span className="range-bar-label">Period</span>
          <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
          <span>to</span>
          <input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
        </div>
        <button className="btn-upload" onClick={() => setUploadOpen(true)}>
          📁 Upload PDF
        </button>
      </div>

      {!activeTicker && (
        <MediaNotice
          icon="📄"
          title="No company selected"
          message="Upload a filing, or type a ticker into the SEC 10-K/10-Q fetch below and pick it in the header once it appears."
        />
      )}

      {activeTicker && (
        <>
          <FetchControlPanel
            ticker={activeTicker}
            startDate={startDate}
            endDate={endDate}
            onFetchComplete={onDataChanged}
          />

          <div className="raw-data-agent-tabs data-viewer-tabs">
            {VIEWER_TABS.map((t) => (
              <button
                key={t.id}
                className={`ledger-tab ${viewerTab === t.id ? "is-active" : ""}`}
                onClick={() => setViewerTab(t.id)}
              >
                {t.label}
              </button>
            ))}
          </div>

          <div className="data-viewer-content">
            {viewerTab === "financials" && (
              <FinancialsPanel ticker={activeTicker} refreshKey={refreshKey} periods={periods} />
            )}
            {viewerTab === "other" && (
              <OtherFilingsView ticker={activeTicker} refreshKey={refreshKey} />
            )}
            {viewerTab === "news" && <NewsPanel ticker={activeTicker} refreshKey={refreshKey} />}
            {viewerTab === "earnings" && <EarningsPanel ticker={activeTicker} refreshKey={refreshKey} />}
            {viewerTab === "price" && <PricePanel ticker={activeTicker} refreshKey={refreshKey} />}
          </div>
        </>
      )}

      {uploadOpen && (
        <UploadModal
          initialMode="upload"
          onClose={() => setUploadOpen(false)}
          onUploadComplete={(filings) => {
            onUploadComplete(filings);
            setUploadOpen(false);
          }}
        />
      )}
    </div>
  );
}

// =============================================================================
// Sub-tab 2: YouTube Insights
// =============================================================================

function YouTubeInsightsPanel() {
  const { activeTicker, refreshKey } = useDashboard();
  const [companyRange, setCompanyRange] = useState<NewsRange>(defaultRange(30));
  const [companyChannel, setCompanyChannel] = useState("all");
  const [macroRange, setMacroRange] = useState<NewsRange>(defaultRange(7));
  const [macroChannel, setMacroChannel] = useState("all");

  const companyVideos = useAsync(
    () => (activeTicker ? getCompanyVideos(activeTicker, companyRange, companyChannel) : Promise.resolve(null)),
    [activeTicker, refreshKey, JSON.stringify(companyRange), companyChannel]
  );
  const macroVideos = useAsync(
    () => getMacroVideos(macroRange, macroChannel),
    [JSON.stringify(macroRange), macroChannel]
  );

  return (
    <div className="view-scroll">
      <section className="view-section">
        <h2 className="section-title">🎬 Company Videos</h2>
        {!activeTicker ? (
          <MediaNotice icon="📄" message="Pick a company to see its analysis videos." />
        ) : (
          <>
            <div className="range-bar">
              <span className="range-bar-label">Range</span>
              <DateRangeSelector value={companyRange} onChange={setCompanyRange} />
              <ChannelBar scope="company" value={companyChannel} onChange={setCompanyChannel} />
            </div>
            <VideoList
              data={companyVideos.data}
              loading={companyVideos.loading}
              error={companyVideos.error}
              ticker={activeTicker}
            />
          </>
        )}
      </section>

      <section className="view-section">
        <h2 className="section-title">🌐 Macro &amp; Industry Videos</h2>
        <div className="range-bar">
          <span className="range-bar-label">Range</span>
          <DateRangeSelector value={macroRange} onChange={setMacroRange} />
          <ChannelBar scope="macro" value={macroChannel} onChange={setMacroChannel} />
        </div>
        <VideoList data={macroVideos.data} loading={macroVideos.loading} error={macroVideos.error} />
      </section>
    </div>
  );
}

// =============================================================================
// Sub-tab 3: Market Overview
// =============================================================================

function MarketOverviewPanel() {
  const [newsRange, setNewsRange] = useState<NewsRange>(defaultRange(7));
  const sentiment = useAsync(getMarketSentiment, []);
  const news = useAsync(() => getMacroNews(newsRange), [JSON.stringify(newsRange)]);

  return (
    <div className="view-scroll">
      <section className="view-section">
        <h2 className="section-title">📊 Market Sentiment</h2>
        <SentimentDashboard data={sentiment.data} loading={sentiment.loading} error={sentiment.error} />
      </section>

      <section className="view-section">
        <h2 className="section-title">🌐 Macro News</h2>
        <div className="range-bar">
          <span className="range-bar-label">Range</span>
          <DateRangeSelector value={newsRange} onChange={setNewsRange} />
        </div>
        <NewsFeed data={news.data} loading={news.loading} error={news.error} />
      </section>
    </div>
  );
}

// =============================================================================
// DataHub — sub-tab shell
// =============================================================================

interface DataHubProps {
  onUploadComplete: (filings: FilingMeta[]) => void;
  onDataChanged: () => void;
}

export default function DataHub({ onUploadComplete, onDataChanged }: DataHubProps) {
  const [subTab, setSubTab] = useState<SubTab>("company");

  return (
    <div className="data-hub">
      <div className="data-hub-tabs">
        <button
          className={`ledger-tab ${subTab === "company" ? "is-active" : ""}`}
          onClick={() => setSubTab("company")}
        >
          🏢 Company Data
        </button>
        <button
          className={`ledger-tab ${subTab === "youtube" ? "is-active" : ""}`}
          onClick={() => setSubTab("youtube")}
        >
          🎥 YouTube Insights
        </button>
        <button
          className={`ledger-tab ${subTab === "market" ? "is-active" : ""}`}
          onClick={() => setSubTab("market")}
        >
          🌐 Market Overview
        </button>
      </div>

      {subTab === "company" && (
        <CompanyDataPanel onUploadComplete={onUploadComplete} onDataChanged={onDataChanged} />
      )}
      {subTab === "youtube" && <YouTubeInsightsPanel />}
      {subTab === "market" && <MarketOverviewPanel />}
    </div>
  );
}
