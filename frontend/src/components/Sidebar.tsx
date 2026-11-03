import { useEffect, useState, type ReactNode } from "react";
import { NavLink } from "react-router-dom";
import type { AgentStatus } from "../api/client";
import { lastKnownText, statusLabel, type DisplayStatus } from "../lib/status";

const COLLAPSED_KEY = "ta.nav.collapsed";

type NavItem = { to: string; label: string; icon: ReactNode };

const GROUPS: { label: string; items: NavItem[] }[] = [
  {
    label: "Monitor",
    items: [
      {
        to: "/",
        label: "Dashboard",
        icon: (
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ flex: "0 0 18px" }}>
            <rect x="3" y="3" width="7" height="9" rx="1.5" />
            <rect x="14" y="3" width="7" height="5" rx="1.5" />
            <rect x="14" y="12" width="7" height="9" rx="1.5" />
            <rect x="3" y="16" width="7" height="5" rx="1.5" />
          </svg>
        ),
      },
      {
        to: "/indicators",
        label: "Indicators",
        icon: (
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ flex: "0 0 18px" }}>
            <polyline points="3 17 9 11 13 15 21 7" />
            <line x1="3" y1="21" x2="21" y2="21" />
          </svg>
        ),
      },
      {
        to: "/performance",
        label: "Performance",
        icon: (
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ flex: "0 0 18px" }}>
            <line x1="6" y1="20" x2="6" y2="14" />
            <line x1="12" y1="20" x2="12" y2="9" />
            <line x1="18" y1="20" x2="18" y2="4" />
          </svg>
        ),
      },
    ],
  },
  {
    label: "Learn",
    items: [
      {
        to: "/insights",
        label: "Insights",
        icon: (
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ flex: "0 0 18px" }}>
            <path d="M9 18h6" />
            <path d="M10 21h4" />
            <path d="M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2.1h5c0-.9.4-1.6 1-2.1A6 6 0 0 0 12 3z" />
          </svg>
        ),
      },
      {
        to: "/what-if",
        label: "What-if",
        icon: (
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ flex: "0 0 18px" }}>
            <circle cx="6" cy="6" r="2" />
            <circle cx="6" cy="18" r="2" />
            <circle cx="18" cy="9" r="2" />
            <path d="M6 8v8" />
            <path d="M6 13c0-3 3-4 6-4h4" />
          </svg>
        ),
      },
      {
        to: "/reviews",
        label: "Reviews",
        icon: (
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ flex: "0 0 18px" }}>
            <rect x="3" y="5" width="18" height="16" rx="2" />
            <line x1="3" y1="10" x2="21" y2="10" />
            <line x1="8" y1="3" x2="8" y2="7" />
            <line x1="16" y1="3" x2="16" y2="7" />
          </svg>
        ),
      },
      {
        to: "/inspector",
        label: "LLM inspector",
        icon: (
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ flex: "0 0 18px" }}>
            <circle cx="11" cy="11" r="6" />
            <line x1="16" y1="16" x2="21" y2="21" />
          </svg>
        ),
      },
    ],
  },
  {
    label: "Control",
    items: [
      {
        to: "/readiness",
        label: "Readiness",
        icon: (
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ flex: "0 0 18px" }}>
            <path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z" />
            <polyline points="9 12 11 14 15 10" />
          </svg>
        ),
      },
      {
        to: "/settings",
        label: "Settings",
        icon: (
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ flex: "0 0 18px" }}>
            <line x1="4" y1="6" x2="20" y2="6" />
            <line x1="4" y1="12" x2="20" y2="12" />
            <line x1="4" y1="18" x2="20" y2="18" />
            <circle cx="9" cy="6" r="2" fill="var(--ta-panel)" />
            <circle cx="15" cy="12" r="2" fill="var(--ta-panel)" />
            <circle cx="7" cy="18" r="2" fill="var(--ta-panel)" />
          </svg>
        ),
      },
    ],
  },
];

/**
 * Logo, Ask button, screens grouped Monitor / Learn / Control, agent status
 * and collapse button at the bottom. Collapses to a 72 px icon rail; the
 * choice is remembered per browser.
 */
export function Sidebar({
  status,
  lastKnown,
  onAsk,
}: {
  status: DisplayStatus;
  lastKnown?: { status: AgentStatus; at: number };
  onAsk?: () => void;
}) {
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem(COLLAPSED_KEY) === "1",
  );

  useEffect(() => {
    localStorage.setItem(COLLAPSED_KEY, collapsed ? "1" : "0");
  }, [collapsed]);

  return (
    <aside
      aria-label="Sidebar"
      className={`sidebar${collapsed ? " sidebar--collapsed" : ""}`}
    >
      <div className="sidebar__brand">
        <div className="sidebar__logo">TA</div>
        {!collapsed && (
          <div style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
            <div className="sidebar__brandname">Trading Agent</div>
            <div className="sidebar__brandsub">Paper · local · v0.1</div>
          </div>
        )}
      </div>

      <button
        type="button"
        aria-label="Ask the agent"
        title="Ask the agent"
        className="nav__item nav__item--ask"
        onClick={onAsk}
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ flex: "0 0 18px" }}>
          <path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z" />
        </svg>
        {!collapsed && <span>Ask the agent</span>}
      </button>

      <nav aria-label="Main" className="nav">
        {GROUPS.map((group) => (
          <div key={group.label} style={{ display: "contents" }}>
            <div className="nav__group">{group.label}</div>
            {group.items.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === "/"}
                aria-label={item.label}
                title={item.label}
                className={({ isActive }) =>
                  `nav__item${isActive ? " nav__item--active" : ""}`
                }
              >
                {item.icon}
                {!collapsed && <span>{item.label}</span>}
              </NavLink>
            ))}
          </div>
        ))}
      </nav>

      <div className="sidebar__spacer" />

      <div
        className="sidebar__status"
        title={
          status === "disconnected" && lastKnown
            ? lastKnownText(lastKnown.status, lastKnown.at)
            : undefined
        }
      >
        <span className={`status-pill__dot status-pill__dot--${status}`} aria-hidden="true" />
        {!collapsed && <span>{statusLabel(status)}</span>}
      </div>

      <button
        type="button"
        aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
        aria-expanded={!collapsed}
        title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
        className="nav__item sidebar__toggle"
        onClick={() => setCollapsed((v) => !v)}
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ flex: "0 0 18px" }}>
          <rect x="3" y="4" width="18" height="16" rx="2" />
          <line x1="9" y1="4" x2="9" y2="20" />
        </svg>
        {!collapsed && <span>Collapse</span>}
      </button>
    </aside>
  );
}
