/**
 * views/MacroSentiment.tsx
 * ────────────────────────
 * View 3 — Macro Market Sentiment (market-wide, company-independent).
 *
 * Sections: a market-sentiment dashboard (gauge + indicators, Gemini-
 * synthesized), aggregated macro news, and a macro/economic video feed.
 */

import { useAsync } from "../hooks/useAsync";
import { getMarketSentiment, getMacroNews, getMacroVideos } from "../api";
import NewsFeed from "../components/media/NewsFeed";
import VideoList from "../components/media/VideoList";
import SentimentDashboard from "../components/media/SentimentDashboard";

export default function MacroSentiment() {
  const sentiment = useAsync(getMarketSentiment, []);
  const news = useAsync(getMacroNews, []);
  const videos = useAsync(getMacroVideos, []);

  return (
    <div className="view-scroll">
      <div className="view-head">
        <h1 className="view-title">Macro Market Sentiment</h1>
        <div className="view-subtitle view-subtitle-muted">
          Market-wide trends, independent of any single company
        </div>
      </div>

      <section className="view-section">
        <h2 className="section-title">📊 Market Sentiment</h2>
        <SentimentDashboard
          data={sentiment.data}
          loading={sentiment.loading}
          error={sentiment.error}
        />
      </section>

      <section className="view-section">
        <h2 className="section-title">🌐 Macro News</h2>
        <NewsFeed data={news.data} loading={news.loading} error={news.error} />
      </section>

      <section className="view-section">
        <h2 className="section-title">🎥 Economy &amp; Market Videos</h2>
        <VideoList data={videos.data} loading={videos.loading} error={videos.error} />
      </section>
    </div>
  );
}
