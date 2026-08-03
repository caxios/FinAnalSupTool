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
    return <MediaNotice variant="loading" message="Loading earnings material…" />;
  if (error) return <MediaNotice variant="error" message={error} />;
  if (!data) return null;
  if (!data.configured) {
    return (
      <MediaNotice
        title="Earnings not configured"
        message={
          data.message ??
          "Set YOUTUBE_API_KEY and/or TAVILY_API_KEY to see earnings material."
        }
      />
    );
  }

  const hasAnything =
    data.summary || data.videos.length > 0 || data.articles.length > 0;

  return (
    <div className="earnings-section">
      {data.summary && (
        <div className="earnings-summary">
          <div className="transcript-label">
            Q{data.quarter} {data.year} Earnings Highlights (AI summary)
          </div>
          <div className="transcript-summary-text">{data.summary}</div>
        </div>
      )}

      {data.videos.length > 0 && (
        <VideoList
          data={{
            configured: true,
            scope: "company",
            videos: data.videos,
            message: null,
          }}
          loading={false}
          error={null}
        />
      )}

      {data.articles.length > 0 && (
        <ul className="news-list">
          {data.articles.map((a, i) => (
            <li key={`${a.url}-${i}`} className="news-item">
              <a
                className="news-title"
                href={a.url}
                target="_blank"
                rel="noopener noreferrer"
              >
                {a.title}
              </a>
              {a.source && (
                <div className="news-meta">
                  <span className="news-source">{a.source}</span>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      {!hasAnything && (
        <MediaNotice
          icon="📭"
          message={`No earnings material found for Q${data.quarter} ${data.year}.`}
        />
      )}
    </div>
  );
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
