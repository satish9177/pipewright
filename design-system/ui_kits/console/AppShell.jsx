// AppShell — sidebar nav + top bar + main content slot.
// Fixed chrome; only the main panel scrolls.

function AppShell({ route, onNavigate, pendingGateCount, children }) {
  const navItems = [
    { id: "runs",   label: "Runs",        icon: "git-commit-horizontal", count: null },
    { id: "gates",  label: "Approval queue", icon: "shield-check", count: pendingGateCount },
    { id: "memory", label: "Memory",      icon: "database", count: null },
  ];

  return (
    <div style={{
      display: "grid",
      gridTemplateColumns: "232px 1fr",
      gridTemplateRows: "48px 1fr",
      height: "100vh",
      background: "var(--pw-bg)",
      fontFamily: "var(--pw-font-sans)",
      color: "var(--pw-fg)",
    }}>
      {/* Sidebar */}
      <aside style={{
        gridRow: "1 / -1",
        background: "var(--pw-paper-2)",
        borderRight: "1px solid var(--pw-border)",
        display: "flex",
        flexDirection: "column",
      }}>
        <div style={{
          height: 48,
          display: "flex", alignItems: "center", gap: 10,
          padding: "0 16px",
          borderBottom: "1px solid var(--pw-border)",
        }}>
          <img src="../../assets/pipewright-mark.svg" width="22" height="22" alt="" />
          <div style={{ fontSize: 16, fontWeight: 600, letterSpacing: "-0.01em" }}>pipewright</div>
        </div>

        <nav style={{ padding: "12px 8px", display: "flex", flexDirection: "column", gap: 2 }}>
          <Eyebrow style={{ padding: "4px 10px 8px", color: "var(--pw-fg-subtle)" }}>WORKSPACE</Eyebrow>
          {navItems.map((item) => {
            const active = route.screen === item.id;
            return (
              <button
                key={item.id}
                onClick={() => onNavigate({ screen: item.id })}
                style={{
                  display: "flex", alignItems: "center", gap: 10,
                  padding: "7px 10px",
                  background: active ? "var(--pw-ink-900)" : "transparent",
                  color: active ? "var(--pw-paper)" : "var(--pw-fg)",
                  border: "none",
                  borderRadius: 2,
                  cursor: "pointer",
                  fontSize: 13,
                  fontFamily: "var(--pw-font-sans)",
                  fontWeight: active ? 500 : 400,
                  textAlign: "left",
                  transition: "background 120ms ease-out",
                }}
                onMouseEnter={e => { if (!active) e.currentTarget.style.background = "var(--pw-paper-3)"; }}
                onMouseLeave={e => { if (!active) e.currentTarget.style.background = "transparent"; }}
              >
                <SidebarIcon name={item.icon} active={active} />
                <span style={{ flex: 1 }}>{item.label}</span>
                {item.count > 0 && (
                  <span style={{
                    fontFamily: "var(--pw-font-mono)",
                    fontSize: 10,
                    padding: "1px 6px",
                    background: active ? "var(--pw-copper-600)" : "var(--pw-copper-100)",
                    color: active ? "var(--pw-paper)" : "var(--pw-copper-700)",
                    borderRadius: 999,
                    letterSpacing: "0.04em",
                  }}>{item.count}</span>
                )}
              </button>
            );
          })}
        </nav>

        <div style={{ flex: 1 }} />

        <div style={{
          padding: "12px 16px",
          borderTop: "1px solid var(--pw-border)",
          display: "flex", flexDirection: "column", gap: 4,
        }}>
          <Eyebrow style={{ marginBottom: 2 }}>CONNECTION</Eyebrow>
          <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}>
            <span style={{
              width: 6, height: 6, borderRadius: "50%", background: "var(--pw-pass-600)",
            }} />
            <Mono style={{ fontSize: 11 }}>localhost:8001</Mono>
          </div>
          <div style={{ fontFamily: "var(--pw-font-mono)", fontSize: 10, color: "var(--pw-fg-subtle)" }}>
            v0.1.0 · gemini-2.5-flash
          </div>
        </div>
      </aside>

      {/* Top bar */}
      <header style={{
        gridColumn: "2",
        height: 48,
        borderBottom: "1px solid var(--pw-border)",
        background: "var(--pw-bg)",
        display: "flex",
        alignItems: "center",
        padding: "0 20px",
        gap: 16,
      }}>
        <div style={{ flex: 1, display: "flex", alignItems: "center", gap: 12 }}>
          <Crumbs route={route} onNavigate={onNavigate} />
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexShrink: 0 }}>
          <input
            placeholder="Search runs, gates, run_id…"
            style={{
              width: 260,
              fontFamily: "var(--pw-font-sans)", fontSize: 12,
              padding: "5px 10px",
              background: "var(--pw-paper-2)",
              border: "1px solid var(--pw-border)",
              borderRadius: 2,
              outline: "none",
            }}
          />
          <Button variant="primary" size="sm" onClick={() => onNavigate({ screen: "start" })} style={{ whiteSpace: "nowrap" }}>
            Start a run
          </Button>
        </div>
      </header>

      {/* Main content */}
      <main style={{
        gridColumn: "2",
        overflow: "auto",
        background: "var(--pw-bg)",
      }}>
        {children}
      </main>
    </div>
  );
}

// Inline SVG icons — avoids the lucide createIcons() timing issue
// when React re-renders the sidebar.
function SidebarIcon({ name, active }) {
  const stroke = active ? "#F6F4EE" : "#0E1116";
  const opacity = active ? 1 : 0.75;
  const common = {
    width: 16, height: 16, viewBox: "0 0 24 24",
    fill: "none", stroke, strokeWidth: 1.6,
    strokeLinecap: "round", strokeLinejoin: "round",
    style: { opacity, flex: "0 0 16px" },
  };
  if (name === "git-commit-horizontal") return (
    <svg {...common}><circle cx="12" cy="12" r="3"/><line x1="3" y1="12" x2="9" y2="12"/><line x1="15" y1="12" x2="21" y2="12"/></svg>
  );
  if (name === "shield-check") return (
    <svg {...common}><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>
  );
  if (name === "database") return (
    <svg {...common}><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v6c0 1.7 4 3 9 3s9-1.3 9-3V5"/><path d="M3 11v6c0 1.7 4 3 9 3s9-1.3 9-3v-6"/></svg>
  );
  return null;
}

function Crumbs({ route, onNavigate }) {
  const parts = [];
  if (route.screen === "runs") {
    if (route.runId) {
      parts.push({ label: "Runs", action: () => onNavigate({ screen: "runs" }) });
      parts.push({ label: shortId(route.runId), action: null });
    } else {
      parts.push({ label: "Runs", action: null });
    }
  } else if (route.screen === "gates") {
    if (route.gateId) {
      parts.push({ label: "Approval queue", action: () => onNavigate({ screen: "gates" }) });
      parts.push({ label: "Gate " + shortId(route.gateId), action: null });
    } else {
      parts.push({ label: "Approval queue", action: null });
    }
  } else if (route.screen === "memory") {
    parts.push({ label: "Memory", action: null });
  } else if (route.screen === "start") {
    parts.push({ label: "Start a run", action: null });
  }

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13 }}>
      {parts.map((p, i) => (
        <React.Fragment key={i}>
          {p.action ? (
            <button
              onClick={p.action}
              style={{
                fontFamily: "var(--pw-font-sans)", fontSize: 13,
                color: "var(--pw-fg-muted)", background: "transparent",
                border: "none", padding: 0, cursor: "pointer",
              }}
            >{p.label}</button>
          ) : (
            <span style={{ color: "var(--pw-fg)", fontWeight: i === parts.length - 1 ? 500 : 400 }}>
              {p.action ? null : ""}{p.label}
            </span>
          )}
          {i < parts.length - 1 && (
            <span style={{ color: "var(--pw-fg-subtle)", fontFamily: "var(--pw-font-mono)", fontSize: 12 }}>/</span>
          )}
        </React.Fragment>
      ))}
    </div>
  );
}

Object.assign(window, { AppShell });
