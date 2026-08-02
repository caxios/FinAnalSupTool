/**
 * DateRangeSelector.tsx
 * ─────────────────────
 * Preset + custom date-range control for the news / video feeds.
 *
 * Presets (1D / 1W / 1M / 3M / 6M / 1Y) map to a `days` look-back; "Custom"
 * reveals two date inputs and emits explicit start/end dates. Controlled by
 * the parent view via `value` / `onChange`.
 */

import type { NewsRange } from "../../types";

interface Preset {
  key: NewsRange["preset"];
  label: string;
  days: number;
}

const PRESETS: Preset[] = [
  { key: "1d", label: "1D", days: 1 },
  { key: "1w", label: "1W", days: 7 },
  { key: "1m", label: "1M", days: 30 },
  { key: "3m", label: "3M", days: 90 },
  { key: "6m", label: "6M", days: 180 },
  { key: "1y", label: "1Y", days: 365 },
];

/** Format a Date as YYYY-MM-DD (local). */
function toISODate(d: Date): string {
  return d.toISOString().slice(0, 10);
}

/** The default range shown on first render. */
export function defaultRange(days = 30): NewsRange {
  const preset = PRESETS.find((p) => p.days === days) ?? PRESETS[2];
  return { preset: preset.key, days: preset.days };
}

interface Props {
  value: NewsRange;
  onChange: (r: NewsRange) => void;
}

export default function DateRangeSelector({ value, onChange }: Props) {
  const today = toISODate(new Date());

  function selectCustom() {
    // Seed a sensible window so the feed fetches immediately.
    const monthAgo = new Date();
    monthAgo.setDate(monthAgo.getDate() - 30);
    onChange({
      preset: "custom",
      start: value.start ?? toISODate(monthAgo),
      end: value.end ?? today,
    });
  }

  return (
    <div className="range-selector">
      <div className="range-presets">
        {PRESETS.map((p) => (
          <button
            key={p.key}
            className={`range-pill ${value.preset === p.key ? "range-pill-active" : ""}`}
            onClick={() => onChange({ preset: p.key, days: p.days })}
          >
            {p.label}
          </button>
        ))}
        <button
          className={`range-pill ${value.preset === "custom" ? "range-pill-active" : ""}`}
          onClick={selectCustom}
        >
          Custom
        </button>
      </div>

      {value.preset === "custom" && (
        <div className="range-custom">
          <label className="range-field">
            <span>From</span>
            <input
              type="date"
              value={value.start ?? ""}
              max={value.end ?? today}
              onChange={(e) =>
                onChange({ ...value, preset: "custom", start: e.target.value })
              }
            />
          </label>
          <label className="range-field">
            <span>To</span>
            <input
              type="date"
              value={value.end ?? ""}
              min={value.start ?? undefined}
              max={today}
              onChange={(e) =>
                onChange({ ...value, preset: "custom", end: e.target.value })
              }
            />
          </label>
        </div>
      )}
    </div>
  );
}
