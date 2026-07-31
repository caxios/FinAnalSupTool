/**
 * VideoList.tsx
 * ─────────────
 * Grid of embedded YouTube videos. Each card can lazily load its transcript
 * (and a Gemini summary) on demand via GET /media/transcript.
 */

import { useState } from "react";
import type { VideoResponse, Video, TranscriptResponse } from "../../types";
import { getTranscript } from "../../api";
import MediaNotice from "./MediaNotice";

interface VideoListProps {
  data: VideoResponse | null;
  loading: boolean;
  error: string | null;
}

function VideoCard({ video }: { video: Video }) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [transcript, setTranscript] = useState<TranscriptResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function toggleTranscript() {
    if (open) {
      setOpen(false);
      return;
    }
    setOpen(true);
    if (transcript || loading) return; // already loaded / loading
    setLoading(true);
    setErr(null);
    try {
      setTranscript(await getTranscript(video.video_id, true));
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed to load transcript.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="video-card">
      <div className="video-embed">
        <iframe
          src={video.embed_url}
          title={video.title}
          loading="lazy"
          allow="accelerometer; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
          allowFullScreen
        />
      </div>
      <div className="video-body">
        <a
          className="video-title"
          href={video.url}
          target="_blank"
          rel="noopener noreferrer"
        >
          {video.title}
        </a>
        <div className="video-channel">{video.channel}</div>
        <button className="video-transcript-btn" onClick={toggleTranscript}>
          {open ? "Hide transcript" : "Transcript & summary"}
        </button>

        {open && (
          <div className="transcript-block">
            {loading && <div className="transcript-status">Loading transcript…</div>}
            {err && <div className="transcript-status transcript-error">{err}</div>}
            {transcript && !transcript.available && (
              <div className="transcript-status">
                {transcript.message ?? "No transcript available."}
              </div>
            )}
            {transcript && transcript.available && (
              <>
                {transcript.summary && (
                  <div className="transcript-summary">
                    <div className="transcript-label">AI Summary</div>
                    <div className="transcript-summary-text">
                      {transcript.summary}
                    </div>
                  </div>
                )}
                <details className="transcript-full">
                  <summary>Full transcript</summary>
                  <p className="transcript-text">{transcript.text}</p>
                </details>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export default function VideoList({ data, loading, error }: VideoListProps) {
  if (loading) return <MediaNotice variant="loading" message="Loading videos…" />;
  if (error) return <MediaNotice variant="error" message={error} />;
  if (!data) return null;

  if (!data.configured) {
    return (
      <MediaNotice
        title="Videos not configured"
        message={
          data.message ??
          "Set YOUTUBE_API_KEY on the backend to enable YouTube video search."
        }
      />
    );
  }

  if (data.videos.length === 0) {
    return <MediaNotice icon="🎬" message={data.message ?? "No videos found."} />;
  }

  return (
    <div className="video-grid">
      {data.videos.map((v) => (
        <VideoCard key={v.video_id} video={v} />
      ))}
    </div>
  );
}
