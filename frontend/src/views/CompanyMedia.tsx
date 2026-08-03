/**
 * views/CompanyMedia.tsx
 * ──────────────────────
 * View 2 — Company Media & Events (qualitative, per-company).
 *
 * Sections, each with its own control:
 *   - News        → date-range selector
 *   - Videos      → date-range selector + channel picker/manager
 *   - Earnings    → fiscal-quarter (year + Q1–Q4) selector
 *
 * The company is derived from the uploaded filings (DashboardContext).
 */

import { useState } from "react";
import type { EarningsResponse, NewsRange } from "../types";
import { useDashboard } from "../context/DashboardContext";
import { useAsync } from "../hooks/useAsync";
import { getCompanyNews, getCompanyVideos, getEarnings } from "../api";
import NewsFeed from "../components/media/NewsFeed";
import VideoList from "../components/media/VideoList";
import MediaNotice from "../components/media/MediaNotice";
import EarningsTranscript from "../components/media/EarningsTranscript";
import DateRangeSelector, {
  defaultRange,
} from "../components/media/DateRangeSelector";
import ChannelBar from "../components/media/ChannelBar";
import QuarterSelector, {
  defaultQuarter,
  type QuarterValue,
} from "../components/media/QuarterSelector";

function EarningsSection({
  data,
  loading,
  error,
}: {
  data: EarningsResponse | null;
  loading: boolean;
  error: string | null;
}) {
  if (loading)
    return <MediaNotice variant="loading" message="Fetching earnings-call transcript…" />;
  if (error) return <MediaNotice variant="error" message={error} />;
  if (!data) return null;
  if (!data.configured) {
    return (
      <MediaNotice
        title="Earnings not configured"
        message={
          data.message ??
          "Set TAVILY_API_KEY on the backend to fetch earnings-call transcripts."
        }
      />
    );
  }

  if (!data.found || !data.transcript) {
    return (
      <MediaNotice
        icon="📭"
        message={
          data.message ??
          `No earnings-call transcript found for Q${data.quarter} ${data.year}.`
        }
      />
    );
  }

  return <EarningsTranscript data={data} />;
}

export default function CompanyMedia() {
  const { company, refreshKey } = useDashboard();

  const [newsRange, setNewsRange] = useState<NewsRange>(defaultRange(30));
  const [videoRange, setVideoRange] = useState<NewsRange>(defaultRange(30));
  const [channel, setChannel] = useState("all");
  const [quarter, setQuarter] = useState<QuarterValue>(defaultQuarter());

  const cik = company?.cik ?? null;
  const news = useAsync(
    () => getCompanyNews(newsRange),
    [cik, refreshKey, JSON.stringify(newsRange)]
  );
  const videos = useAsync(
    () => getCompanyVideos(videoRange, channel),
    [cik, refreshKey, JSON.stringify(videoRange), channel]
  );
  const earnings = useAsync(
    () => getEarnings(quarter.year, quarter.quarter),
    [cik, refreshKey, quarter.year, quarter.quarter]
  );

  const companyLabel = company
    ? `${company.name ?? "Unknown company"}${company.ticker ? ` (${company.ticker})` : ""}`
    : null;

  return (
    <div className="view-scroll">
      <div className="view-head">
        <h1 className="view-title">Company Media &amp; Events</h1>
        {companyLabel ? (
          <div className="view-subtitle">{companyLabel}</div>
        ) : (
          <div className="view-subtitle view-subtitle-muted">
            Upload a 10-K/10-Q to identify the company
          </div>
        )}
      </div>

      {!company && (
        <MediaNotice
          icon="📄"
          title="No company yet"
          message="Upload an SEC filing on the Dashboard — the company is detected from it, then news, videos, and earnings appear here."
        />
      )}

      {company && (
        <>
          <section className="view-section">
            <h2 className="section-title">📰 News</h2>
            <div className="range-bar">
              <span className="range-bar-label">Range</span>
              <DateRangeSelector value={newsRange} onChange={setNewsRange} />
            </div>
            <NewsFeed data={news.data} loading={news.loading} error={news.error} />
          </section>

          <section className="view-section">
            <h2 className="section-title">🎬 Analysis Videos</h2>
            <div className="range-bar">
              <span className="range-bar-label">Range</span>
              <DateRangeSelector value={videoRange} onChange={setVideoRange} />
              <ChannelBar scope="company" value={channel} onChange={setChannel} />
            </div>
            <VideoList data={videos.data} loading={videos.loading} error={videos.error} />
          </section>

          <section className="view-section">
            <h2 className="section-title">📞 Earnings</h2>
            <div className="range-bar">
              <span className="range-bar-label">Quarter</span>
              <QuarterSelector value={quarter} onChange={setQuarter} />
            </div>
            <EarningsSection
              data={earnings.data}
              loading={earnings.loading}
              error={earnings.error}
            />
          </section>
        </>
      )}
    </div>
  );
}
