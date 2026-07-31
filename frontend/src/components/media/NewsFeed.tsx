/**
 * NewsFeed.tsx
 * ────────────
 * Renders a list of news articles (company or macro) with graceful
 * loading / not-configured / error / empty states.
 */

import type { NewsResponse } from "../../types";
import MediaNotice from "./MediaNotice";

interface NewsFeedProps {
  data: NewsResponse | null;
  loading: boolean;
  error: string | null;
}

function formatDate(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? "" : d.toLocaleDateString();
}

export default function NewsFeed({ data, loading, error }: NewsFeedProps) {
  if (loading) return <MediaNotice variant="loading" message="Loading news…" />;
  if (error) return <MediaNotice variant="error" message={error} />;
  if (!data) return null;

  if (!data.configured) {
    return (
      <MediaNotice
        title="News not configured"
        message={data.message ?? "Set TAVILY_API_KEY on the backend to enable news."}
      />
    );
  }

  if (data.articles.length === 0) {
    return (
      <MediaNotice
        icon="📭"
        message={data.message ?? "No recent articles found."}
      />
    );
  }

  return (
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
          <div className="news-meta">
            {a.source && <span className="news-source">{a.source}</span>}
            {formatDate(a.published) && (
              <span className="news-date">{formatDate(a.published)}</span>
            )}
          </div>
          {a.snippet && <p className="news-snippet">{a.snippet}</p>}
        </li>
      ))}
    </ul>
  );
}
