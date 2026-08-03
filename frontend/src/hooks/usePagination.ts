/**
 * usePagination.ts
 * ────────────────
 * Client-side pagination over an already-fetched array.
 *
 * The media feeds fetch a large batch up front (e.g. up to 50 videos / 30
 * articles in one API call) and then slice it into fixed-size pages here, so
 * paging is instant and doesn't spend extra API quota. The current page resets
 * to 1 whenever the underlying list changes (a new fetch).
 */

import { useEffect, useMemo, useState } from "react";

export interface Pagination<T> {
  page: number;
  pageCount: number;
  pageItems: T[];
  setPage: (p: number) => void;
  rangeStart: number; // 1-based index of the first item on this page
  rangeEnd: number; // 1-based index of the last item on this page
  total: number;
}

/**
 * @param items      Full list of items to paginate.
 * @param pageSize   Items shown per page (default 10).
 * @param resetKey   When this value changes, jump back to page 1 (e.g. a new
 *                   query/range). Defaults to the item count.
 */
export function usePagination<T>(
  items: T[],
  pageSize = 10,
  resetKey?: unknown
): Pagination<T> {
  const [page, setPage] = useState(1);

  const total = items.length;
  const pageCount = Math.max(1, Math.ceil(total / pageSize));

  // Reset to the first page whenever the list identity changes.
  useEffect(() => {
    setPage(1);
  }, [resetKey ?? total]);

  // Guard against the page falling out of range if the list shrinks.
  const safePage = Math.min(page, pageCount);

  const pageItems = useMemo(() => {
    const start = (safePage - 1) * pageSize;
    return items.slice(start, start + pageSize);
  }, [items, safePage, pageSize]);

  const rangeStart = total === 0 ? 0 : (safePage - 1) * pageSize + 1;
  const rangeEnd = Math.min(safePage * pageSize, total);

  return {
    page: safePage,
    pageCount,
    pageItems,
    setPage: (p) => setPage(Math.min(Math.max(1, p), pageCount)),
    rangeStart,
    rangeEnd,
    total,
  };
}
