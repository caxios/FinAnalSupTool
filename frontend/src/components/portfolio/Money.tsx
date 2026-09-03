/**
 * Money.tsx
 * ─────────
 * Every monetary figure in the portfolio, in **both won and dollars**.
 *
 * Two rules, and they are the whole component:
 *
 *   1. **Both currencies, always.** A Samsung position shows a dollar figure and
 *      an Apple position shows a won figure. They are the same wealth stated
 *      twice, not a native value with a translation.
 *
 *   2. **It never multiplies by a rate.** Both numbers arrive already computed
 *      from the API, because conversion happens in exactly one place on the
 *      backend. Two conversion sites are two places to disagree about what a
 *      figure is worth.
 *
 * A `null` on either side renders as a dash — never a zero, never a stale value.
 * "0" and "unknown" mean very different things to someone reading a balance.
 */

import { createContext, useCallback, useContext, useEffect, useState } from "react";

export type CurrencyView = "both" | "krw" | "usd";

const STORAGE_KEY = "portfolio.currencyView";

const CurrencyViewContext = createContext<CurrencyView>("both");

/** Reads the persisted preference once, defaulting to showing both. */
export function useCurrencyViewState() {
  const [view, setView] = useState<CurrencyView>(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      if (stored === "krw" || stored === "usd" || stored === "both") return stored;
    } catch {
      // Private browsing, blocked storage — the default is perfectly usable.
    }
    return "both";
  });

  const update = useCallback((next: CurrencyView) => {
    setView(next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* preference is a convenience, not state the app depends on */
    }
  }, []);

  return { view, setView: update };
}

export function CurrencyViewProvider({
  view,
  children,
}: {
  view: CurrencyView;
  children: React.ReactNode;
}) {
  return (
    <CurrencyViewContext.Provider value={view}>
      {children}
    </CurrencyViewContext.Provider>
  );
}

export function useCurrencyView(): CurrencyView {
  return useContext(CurrencyViewContext);
}

/** Won carries no decimals — ₩16,450,000.00 is noise. */
export function formatKrw(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return n.toLocaleString("ko-KR", {
    style: "currency",
    currency: "KRW",
    maximumFractionDigits: 0,
  });
}

export function formatUsd(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return n.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  });
}

/** Format an amount in whatever currency it is actually denominated in. */
export function formatNative(
  n: number | null | undefined,
  currency: string | null | undefined
): string {
  return (currency || "USD").toUpperCase() === "KRW" ? formatKrw(n) : formatUsd(n);
}

export default function Money({
  krw,
  usd,
  compact = false,
  signed = false,
  className = "",
}: {
  krw: number | null | undefined;
  usd: number | null | undefined;
  /**
   * Dense table cells: both values on one line, separated by a divider, rather
   * than dropping one. Dropping a currency in the table would defeat the point
   * exactly where a Korean and a US holding are being compared.
   */
  compact?: boolean;
  /** Prefix a leading "+" on gains, for P/L figures. */
  signed?: boolean;
  className?: string;
}) {
  const view = useCurrencyView();

  const sign = (v: number | null | undefined) =>
    signed && v !== null && v !== undefined && v > 0 ? "+" : "";

  const krwText = `${sign(krw)}${formatKrw(krw)}`;
  const usdText = `${sign(usd)}${formatUsd(usd)}`;

  if (view === "krw") {
    return <span className={`money money-single ${className}`}>{krwText}</span>;
  }
  if (view === "usd") {
    return <span className={`money money-single ${className}`}>{usdText}</span>;
  }

  if (compact) {
    return (
      <span className={`money money-compact ${className}`}>
        <span className="money-krw">{krwText}</span>
        <span className="money-divider" aria-hidden="true">
          ·
        </span>
        <span className="money-usd">{usdText}</span>
      </span>
    );
  }

  return (
    <span className={`money money-stacked ${className}`}>
      <span className="money-krw">{krwText}</span>
      <span className="money-usd">{usdText}</span>
    </span>
  );
}

/** The KRW / USD / Both switch. Defaults to Both; Both is the requirement. */
export function CurrencyToggle({
  view,
  onChange,
}: {
  view: CurrencyView;
  onChange: (v: CurrencyView) => void;
}) {
  const options: { key: CurrencyView; label: string }[] = [
    { key: "both", label: "Both" },
    { key: "krw", label: "₩" },
    { key: "usd", label: "$" },
  ];
  return (
    <div className="currency-toggle" role="group" aria-label="Currency display">
      {options.map((o) => (
        <button
          key={o.key}
          className={`currency-toggle-btn ${view === o.key ? "is-active" : ""}`}
          onClick={() => onChange(o.key)}
          title={
            o.key === "both"
              ? "Show both currencies"
              : `Show ${o.key.toUpperCase()} only`
          }
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}
