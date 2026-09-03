/**
 * NetWorthHeader.tsx
 * ──────────────────
 * The number the user opened the page for, and which did not exist anywhere in
 * the app until now: **what am I worth**.
 *
 * Beneath it, the split between equity and cash with an allocation bar, and
 * **FX exposure** — the share of net worth whose won value moves with the
 * exchange rate. For someone who converts won to dollars in order to invest,
 * that single figure summarises their whole currency situation.
 *
 * The rate is shown with its `as_of` date and a stale badge: a rate the user
 * cannot date is a rate they cannot check.
 */

import type { PortfolioResponse } from "../../types";
import Money from "./Money";

function pct(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : `${(n * 100).toFixed(1)}%`;
}

export default function NetWorthHeader({ data }: { data: PortfolioResponse }) {
  const equityW = data.equity_weight ?? 0;
  const cashW = data.cash_weight ?? 0;
  const hasNetWorth = data.net_worth_krw !== null;

  return (
    <section className="networth">
      <div className="networth-main">
        <div className="networth-label">Net worth</div>
        <div className="networth-value">
          <Money krw={data.net_worth_krw} usd={data.net_worth_usd} />
        </div>
      </div>

      {hasNetWorth && (
        <>
          <div className="networth-split">
            <div className="networth-part">
              <span className="networth-part-label">
                Equity <span className="networth-part-pct">{pct(equityW)}</span>
              </span>
              <Money
                krw={data.equity_total_krw}
                usd={data.equity_total_usd}
                compact
              />
            </div>
            <div className="networth-part">
              <span className="networth-part-label">
                Cash <span className="networth-part-pct">{pct(cashW)}</span>
              </span>
              <Money
                krw={data.cash_total_krw}
                usd={data.cash_total_usd}
                compact
              />
            </div>
            <div className="networth-part">
              <span className="networth-part-label">
                Dollar exposure
                <span className="networth-part-pct">{pct(data.fx_exposure)}</span>
              </span>
              <span className="networth-fx-note">
                of your net worth moves with USDKRW
              </span>
            </div>
          </div>

          <div className="networth-bar" title={`Equity ${pct(equityW)} · Cash ${pct(cashW)}`}>
            <div
              className="networth-bar-equity"
              style={{ width: `${Math.max(0, Math.min(100, equityW * 100))}%` }}
            />
            <div
              className="networth-bar-cash"
              style={{ width: `${Math.max(0, Math.min(100, cashW * 100))}%` }}
            />
          </div>
        </>
      )}

      <div className="networth-fx">
        {data.fx?.rate ? (
          <>
            <span className="networth-fx-rate">
              1 USD = {data.fx.rate.toLocaleString("ko-KR", {
                maximumFractionDigits: 2,
              })}{" "}
              KRW
            </span>
            {data.fx.as_of && (
              <span className="networth-fx-asof">
                as of {data.fx.as_of.slice(0, 10)}
              </span>
            )}
            {data.fx.is_stale && (
              <span
                className="networth-fx-stale"
                title="This quote is older than a normal trading gap."
              >
                stale
              </span>
            )}
          </>
        ) : (
          <span className="networth-fx-missing">
            No exchange rate available — converted figures are shown as “—”.
          </span>
        )}
      </div>

      {data.note && <div className="networth-note">{data.note}</div>}
    </section>
  );
}
