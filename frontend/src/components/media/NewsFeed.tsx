/**
 * NewsFeed.tsx
 * ────────────
 * Renders a list of news articles (company or macro) with graceful
 * loading / not-configured / error / empty states.
 */

import type { NewsResponse } from "../../types";
import MediaNotice from "./MediaNotice";
import Pagination from "./Pagination";
import { usePagination } from "../../hooks/usePagination";

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
  const articles = data?.articles ?? [];
  // 10 articles per page; reset to page 1 when the fetched set changes.
  const pager = usePagination(articles, 10, articles[0]?.url ?? articles.length);

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

  if (articles.length === 0) {
    return (
      <MediaNotice
        icon="📭"
        message={data.message ?? "No recent articles found."}
      />
    );
  }

  return (
    <>
      <ul className="news-list">
        {pager.pageItems.map((a, i) => (
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
      <Pagination
        page={pager.page}
        pageCount={pager.pageCount}
        onChange={pager.setPage}
        rangeStart={pager.rangeStart}
        rangeEnd={pager.rangeEnd}
        total={pager.total}
      />
    </>
  );
}
