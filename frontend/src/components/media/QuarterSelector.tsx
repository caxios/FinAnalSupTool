/**
 * QuarterSelector.tsx
 * ───────────────────
 * Fiscal-quarter picker for the Earnings section: a year dropdown + Q1–Q4
 * pills (e.g. 2026 Q1). Controlled via `value` / `onChange`.
 */

export interface QuarterValue {
  year: number;
  quarter: number; // 1–4
}

interface Props {
  value: QuarterValue;
  onChange: (v: QuarterValue) => void;
}

const QUARTERS = [1, 2, 3, 4];

/** Default to the most recently completed quarter. */
export function defaultQuarter(): QuarterValue {
  const now = new Date();
  const q = Math.floor(now.getMonth() / 3) + 1; // current calendar quarter
  // Step back one quarter (earnings for a quarter land after it ends).
  if (q === 1) return { year: now.getFullYear() - 1, quarter: 4 };
  return { year: now.getFullYear(), quarter: q - 1 };
}

export default function QuarterSelector({ value, onChange }: Props) {
  const thisYear = new Date().getFullYear();
  const years: number[] = [];
  for (let y = thisYear + 1; y >= thisYear - 8; y--) years.push(y);

  return (
    <div className="quarter-selector">
      <select
        className="quarter-year"
        value={value.year}
        onChange={(e) => onChange({ ...value, year: Number(e.target.value) })}
      >
        {years.map((y) => (
          <option key={y} value={y}>
            {y}
          </option>
        ))}
      </select>
      <div className="range-presets">
        {QUARTERS.map((q) => (
          <button
            key={q}
            className={`range-pill ${value.quarter === q ? "range-pill-active" : ""}`}
            onClick={() => onChange({ ...value, quarter: q })}
          >
            Q{q}
          </button>
        ))}
      </div>
    </div>
  );
}
