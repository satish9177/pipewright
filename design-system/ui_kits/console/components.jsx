// Shared atomic components for the Pipewright Console.
// All styles use --pw-* CSS variables from colors_and_type.css.

const { useState, useEffect, useMemo } = React;

// ----- Tag — the brand's signature [BRACKETED] form -----
function Tag({ children, tone = "ink", className = "", style = {} }) {
  const toneColors = {
    ink: "var(--pw-ink-900)",
    muted: "var(--pw-fg-subtle)",
    info: "var(--pw-info-600)",
    pass: "var(--pw-pass-600)",
    wait: "var(--pw-wait-600)",
    fail: "var(--pw-fail-600)",
    copper: "var(--pw-copper-600)",
    paper: "var(--pw-paper)",
  };
  return (
    <span
      className={className}
      style={{
        fontFamily: "var(--pw-font-mono)",
        fontWeight: 500,
        fontSize: 11,
        letterSpacing: "0.08em",
        textTransform: "uppercase",
        color: toneColors[tone] || tone,
        ...style,
      }}
    >
      <span style={{ opacity: 0.55 }}>[</span>
      {children}
      <span style={{ opacity: 0.55 }}>]</span>
    </span>
  );
}

// ----- Eyebrow — small all-caps mono label above headings -----
function Eyebrow({ children, tone = "subtle", style = {} }) {
  const colorMap = {
    subtle: "var(--pw-fg-subtle)",
    copper: "var(--pw-copper-600)",
    info: "var(--pw-info-600)",
  };
  return (
    <div
      style={{
        fontFamily: "var(--pw-font-mono)",
        fontSize: 11,
        letterSpacing: "0.08em",
        textTransform: "uppercase",
        color: colorMap[tone] || tone,
        ...style,
      }}
    >
      {children}
    </div>
  );
}

// ----- Status pill -----
function StatusPill({ status, pulse = false }) {
  // status is a free string; map common ones to a tone
  const s = (status || "").toLowerCase();
  let bg, fg, dot;
  if (["complete", "approved", "pass", "passed"].includes(s)) {
    bg = "#D5E8DC"; fg = "#14502C"; dot = "#1F6E3D";
  } else if (["paused", "pending", "running", "waiting"].includes(s)) {
    bg = "#EBE0BC"; fg = "#6B5410"; dot = "#8A6D1A";
  } else if (["failed", "rejected", "timeout", "fail"].includes(s)) {
    bg = "#F1D4D4"; fg = "#7A1F1F"; dot = "#9B2C2C";
  } else if (["info", "queued"].includes(s)) {
    bg = "#D5DFEF"; fg = "#163A6F"; dot = "#1F4A8A";
  } else {
    bg = "#EDEEF1"; fg = "#3D4550"; dot = "#6B7480";
  }
  const pulsing = pulse || ["paused", "pending", "running", "waiting"].includes(s);
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 6,
      background: bg, color: fg, padding: "2px 9px",
      borderRadius: 999, fontFamily: "var(--pw-font-mono)",
      fontSize: 10, letterSpacing: "0.08em", lineHeight: 1.4,
    }}>
      <span style={{
        width: 6, height: 6, borderRadius: "50%", background: dot,
        animation: pulsing ? "pwPulse 1.2s ease-in-out infinite" : "none",
      }} />
      {(status || "").toUpperCase()}
    </span>
  );
}

// ----- Risk badge -----
function RiskBadge({ level = "medium" }) {
  const l = level.toLowerCase();
  let bg, fg, dot;
  if (l === "low")  { bg = "#D5E8DC"; fg = "#14502C"; dot = "#1F6E3D"; }
  else if (l === "high") { bg = "#F1D4D4"; fg = "#7A1F1F"; dot = "#9B2C2C"; }
  else { bg = "#EBE0BC"; fg = "#6B5410"; dot = "#8A6D1A"; }
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 6,
      background: bg, color: fg, padding: "3px 9px",
      borderRadius: 999, fontFamily: "var(--pw-font-mono)",
      fontSize: 10, letterSpacing: "0.08em", lineHeight: 1.4,
    }}>
      <span style={{ width: 6, height: 6, borderRadius: "50%", background: dot }} />
      RISK · {l.toUpperCase()}
    </span>
  );
}

// ----- Button -----
function Button({ variant = "primary", size = "md", mono = false, children, onClick, disabled, style = {}, type = "button" }) {
  const palettes = {
    primary:   { bg: "#B7531C", fg: "#F6F4EE", bd: "#B7531C", hover: "#8A3F12" },
    secondary: { bg: "#FFFFFF", fg: "#0E1116", bd: "#0E1116", hover: "#EDEEF1" },
    ghost:     { bg: "transparent", fg: "#0E1116", bd: "transparent", hover: "#EDEEF1" },
    danger:    { bg: "#FFFFFF", fg: "#9B2C2C", bd: "#9B2C2C", hover: "#FAE8E8" },
    inverse:   { bg: "transparent", fg: "#F6F4EE", bd: "#3D4550", hover: "#1A1F26" },
  };
  const p = palettes[variant] || palettes.primary;
  const [hover, setHover] = useState(false);
  const padding = size === "sm" ? "5px 10px" : size === "lg" ? "10px 16px" : "7px 13px";
  const fontSize = size === "sm" ? 12 : size === "lg" ? 14 : 13;
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        fontFamily: mono ? "var(--pw-font-mono)" : "var(--pw-font-sans)",
        fontSize, fontWeight: mono ? 500 : 500,
        letterSpacing: mono ? "0.04em" : 0,
        padding, lineHeight: 1, cursor: disabled ? "not-allowed" : "pointer",
        background: disabled ? "#EDEEF1" : hover ? p.hover : p.bg,
        color: disabled ? "#8A929C" : (hover && variant === "primary" ? "#F6F4EE" : p.fg),
        border: `1px solid ${disabled ? "#E4E6EA" : p.bd}`,
        borderRadius: 2,
        transition: "background 120ms ease-out, color 120ms ease-out",
        ...style,
      }}
    >
      {children}
    </button>
  );
}

// ----- Card -----
function Card({ children, style = {} }) {
  return (
    <div style={{
      background: "var(--pw-bg-elev)",
      border: "1px solid var(--pw-border)",
      borderRadius: 4, ...style,
    }}>
      {children}
    </div>
  );
}

// ----- Mono code chip (for IDs, paths) -----
function Mono({ children, muted = false, style = {} }) {
  return (
    <span style={{
      fontFamily: "var(--pw-font-mono)",
      fontSize: 12,
      color: muted ? "var(--pw-fg-subtle)" : "var(--pw-fg)",
      ...style,
    }}>
      {children}
    </span>
  );
}

// ----- Hairline rule -----
function Rule({ vertical = false, style = {} }) {
  return vertical ? (
    <div style={{ width: 1, alignSelf: "stretch", background: "var(--pw-border)", ...style }} />
  ) : (
    <div style={{ height: 1, width: "100%", background: "var(--pw-border)", ...style }} />
  );
}

// ----- Relative time helper -----
function relTime(iso) {
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

// Truncate UUIDs to first 8 chars
function shortId(id) {
  return id ? id.slice(0, 8) : "";
}

Object.assign(window, {
  Tag, Eyebrow, StatusPill, RiskBadge, Button, Card, Mono, Rule, relTime, shortId,
});
