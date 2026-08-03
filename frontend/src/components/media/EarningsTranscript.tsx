/**
 * EarningsTranscript.tsx
 * ──────────────────────
 * Renders a fetched earnings-call transcript (from investing.com or Motley
 * Fool) with its source, a link to the original page, a download button, and
 * the full text in a scrollable block.
 */

import type { EarningsResponse } from "../../types";

const SOURCE_LABELS: Record<string, string> = {
  "investing.com": "Investing.com",
  "fool.com": "Motley Fool",
};

function download(data: EarningsResponse) {
  const name =
    `${data.company?.ticker ?? data.company?.name ?? "earnings"}` +
    `-Q${data.quarter}-${data.year}-transcript`;
  const safe = name.replace(/[^\w.-]+/g, "_");
  const header = [data.title, data.url, ""].filter(Boolean).join("\n");
  const blob = new Blob([`${header}\n${data.transcript ?? ""}`], {
    type: "text/plain;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${safe}.txt`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export default function EarningsTranscript({ data }: { data: EarningsResponse }) {
  const sourceLabel = data.source
    ? SOURCE_LABELS[data.source] ?? data.source
    : "";

  return (
    <div className="earnings-transcript">
      <div className="earnings-transcript-head">
        <div className="earnings-transcript-meta">
          {sourceLabel && (
            <span className="earnings-source-badge">{sourceLabel}</span>
          )}
          <span className="earnings-transcript-title">
            {data.title ?? `Q${data.quarter} ${data.year} Earnings Call`}
          </span>
        </div>
        <div className="earnings-transcript-actions">
          {data.url && (
            <a
              className="transcript-download"
              href={data.url}
              target="_blank"
              rel="noopener noreferrer"
            >
              ↗ Source
            </a>
          )}
          <button
            className="transcript-download"
            onClick={() => download(data)}
          >
            ↓ Download .txt
          </button>
        </div>
      </div>
      <pre className="earnings-transcript-text">{data.transcript}</pre>
    </div>
  );
}
