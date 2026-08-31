/**
 * Sidebar.tsx
 * ───────────
 * Left navigation rail. Switches between the SPA views via react-router
 * `NavLink`s (no full-page reload). Active route gets an accent highlight.
 */

import { NavLink } from "react-router-dom";

interface NavItem {
  to: string;
  label: string;
  icon: string;
  hint: string;
}

const NAV_ITEMS: NavItem[] = [
  { to: "/", label: "Dashboard", icon: "📊", hint: "Financials & filings" },
  { to: "/media", label: "Company Media", icon: "📰", hint: "News, videos, earnings" },
  { to: "/macro", label: "Macro Sentiment", icon: "🌐", hint: "Market-wide trends" },
  { to: "/analysis", label: "Deep Analysis", icon: "🧭", hint: "Multi-agent report" },
  { to: "/portfolio", label: "Portfolio", icon: "💼", hint: "Holdings & journal" },
];

export default function Sidebar() {
  return (
    <nav className="sidebar">
      {NAV_ITEMS.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          // `end` on "/" so it isn't marked active for every nested route.
          end={item.to === "/"}
          className={({ isActive }) =>
            `sidebar-link ${isActive ? "sidebar-link-active" : ""}`
          }
          title={item.hint}
        >
          <span className="sidebar-icon">{item.icon}</span>
          <span className="sidebar-labels">
            <span className="sidebar-label">{item.label}</span>
            <span className="sidebar-hint">{item.hint}</span>
          </span>
        </NavLink>
      ))}
    </nav>
  );
}
