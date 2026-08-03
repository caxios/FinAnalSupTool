/**
 * Pagination.tsx
 * ──────────────
 * Presentational pager: [‹ Prev] [1] [2] … [N] [Next ›] with a windowed set of
 * page numbers (ellipses when there are many pages). Renders nothing for a
 * single page.
 */

interface Props {
  page: number;
  pageCount: number;
  onChange: (page: number) => void;
  /** Optional "Showing 1–10 of 42" label. */
  rangeStart?: number;
  rangeEnd?: number;
  total?: number;
}

/** Build a compact list of page numbers with `"…"` gaps around the current page. */
function pageWindow(current: number, count: number): (number | "…")[] {
  if (count <= 7) {
    return Array.from({ length: count }, (_, i) => i + 1);
  }
  const out: (number | "…")[] = [1];
  const left = Math.max(2, current - 1);
  const right = Math.min(count - 1, current + 1);
  if (left > 2) out.push("…");
  for (let p = left; p <= right; p++) out.push(p);
  if (right < count - 1) out.push("…");
  out.push(count);
  return out;
}

export default function Pagination({
  page,
  pageCount,
  onChange,
  rangeStart,
  rangeEnd,
  total,
}: Props) {
  if (pageCount <= 1) return null;
  const items = pageWindow(page, pageCount);

  return (
    <nav className="pagination" aria-label="Pagination">
      {rangeStart != null && total != null && (
        <span className="pagination-summary">
          {rangeStart}–{rangeEnd} of {total}
        </span>
      )}
      <div className="pagination-controls">
        <button
          className="page-btn"
          onClick={() => onChange(page - 1)}
          disabled={page <= 1}
          aria-label="Previous page"
        >
          ‹ Prev
        </button>

        {items.map((it, i) =>
          it === "…" ? (
            <span key={`gap-${i}`} className="page-gap">
              …
            </span>
          ) : (
            <button
              key={it}
              className={`page-btn${it === page ? " page-btn-active" : ""}`}
              onClick={() => onChange(it)}
              aria-current={it === page ? "page" : undefined}
            >
              {it}
            </button>
          )
        )}

        <button
          className="page-btn"
          onClick={() => onChange(page + 1)}
          disabled={page >= pageCount}
          aria-label="Next page"
        >
          Next ›
        </button>
      </div>
    </nav>
  );
}
