/**
 * views/CompanyMedia.tsx
 * ──────────────────────
 * View 2 — Company Media & Events (qualitative, per-company).
 *
 * Sections: company news feed, analysis videos (+ transcripts), and a
 * best-effort earnings section (earnings-call video + AI-summarized news).
 * The company is derived from the uploaded filings (DashboardContext).
 */

import type { EarningsResponse } from "../types";
import { useDashboard } from "../context/DashboardContext";
import { useAsync } from "../hooks/useAsync";
import { getCompanyNews, getCompanyVideos, getEarnings } from "../api";
import NewsFeed from "../components/media/NewsFeed";
import VideoList from "../components/media/VideoList";
import MediaNotice from "../components/media/MediaNotice";

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

  return (
    <div className="earnings-section">
      {data.summary && (
        <div className="earnings-summary">
          <div className="transcript-label">Earnings Highlights (AI summary)</div>
          <div className="transcript-summary-text">{data.summary}</div>
        </div>
      )}
      {data.video && (
        <div className="earnings-video">
          <div className="video-embed">
            <iframe
              src={data.video.embed_url}
              title={data.video.title}
              loading="lazy"
              allow="accelerometer; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
              allowFullScreen
            />
          </div>
          <a
            className="video-title"
            href={data.video.url}
            target="_blank"
            rel="noopener noreferrer"
          >
            {data.video.title}
          </a>
        </div>
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
              {a.source && <div className="news-meta"><span className="news-source">{a.source}</span></div>}
            </li>
          ))}
        </ul>
      )}
      {!data.summary && !data.video && data.articles.length === 0 && (
        <MediaNotice icon="📭" message="No earnings material found yet." />
      )}
    </div>
  );
}

export default function CompanyMedia() {
  const { company, refreshKey } = useDashboard();

  const cik = company?.cik ?? null;
  const news = useAsync(getCompanyNews, [cik, refreshKey]);
  const videos = useAsync(getCompanyVideos, [cik, refreshKey]);
  const earnings = useAsync(getEarnings, [cik, refreshKey]);

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
            <NewsFeed data={news.data} loading={news.loading} error={news.error} />
          </section>

          <section className="view-section">
            <h2 className="section-title">🎬 Analysis Videos</h2>
            <VideoList data={videos.data} loading={videos.loading} error={videos.error} />
          </section>

          <section className="view-section">
            <h2 className="section-title">📞 Earnings</h2>
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
