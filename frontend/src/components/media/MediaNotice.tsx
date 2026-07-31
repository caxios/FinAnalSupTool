/**
 * MediaNotice.tsx
 * ───────────────
 * Consistent inline state card for the media/macro views: loading, empty,
 * "not configured" (missing API key), and error states.
 */

interface MediaNoticeProps {
  variant?: "info" | "loading" | "error";
  icon?: string;
  title?: string;
  message: string;
}

export default function MediaNotice({
  variant = "info",
  icon,
  title,
  message,
}: MediaNoticeProps) {
  const defaultIcon =
    variant === "error" ? "⚠️" : variant === "loading" ? "⏳" : "🔌";
  return (
    <div className={`media-notice media-notice-${variant}`}>
      <span className="media-notice-icon">{icon ?? defaultIcon}</span>
      <div className="media-notice-body">
        {title && <div className="media-notice-title">{title}</div>}
        <div className="media-notice-msg">{message}</div>
      </div>
    </div>
  );
}
