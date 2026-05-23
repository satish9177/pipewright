// Runs list — main runs dashboard view.

function RunsList({ runs, onOpen, onOpenGate }) {
  const pending = runs.filter(r => r.status === "paused");
  const recent = runs;

  return (
    <div style={{ padding: "24px 28px", maxWidth: 1200, margin: "0 auto" }}>
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", marginBottom: 22 }}>
        <div>
          <Eyebrow>PIPELINE RUNS</Eyebrow>
          <h1 style={{
            fontFamily: "var(--pw-font-sans)", fontSize: 28, fontWeight: 600,
            letterSpacing: "-0.01em", margin: "4px 0 0",
          }}>All runs</h1>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <Stat label="QUEUED" value="0" />
          <Stat label="RUNNING" value="0" />
          <Stat label="PAUSED" value={String(pending.length)} accent={pending.length > 0} />
          <Stat label="COMPLETE TODAY" value={String(runs.filter(r => r.status === "complete").length)} />
        </div>
      </div>

      {pending.length > 0 && (
        <Card style={{ marginBottom: 18, borderColor: "var(--pw-copper-600)" }}>
          <div style={{ padding: "14px 18px", display: "flex", alignItems: "center", gap: 16 }}>
            <Tag tone="copper">APPROVAL · PIPELINE PAUSED</Tag>
            <div style={{ flex: 1, fontSize: 13, color: "var(--pw-fg)" }}>
              <strong style={{ fontWeight: 600 }}>{pending.length} run{pending.length === 1 ? "" : "s"}</strong> waiting on human decision.
            </div>
            <Button
              variant="primary"
              size="sm"
              onClick={() => onOpenGate(pending[0].gate_id)}
            >
              Review oldest →
            </Button>
          </div>
        </Card>
      )}

      <Card>
        <div style={{
          padding: "10px 18px",
          borderBottom: "1px solid var(--pw-border)",
          display: "flex", alignItems: "center", justifyContent: "space-between",
        }}>
          <Eyebrow>RECENT</Eyebrow>
          <div style={{ display: "flex", gap: 8, fontSize: 12 }}>
            <FilterChip label="All" active />
            <FilterChip label="Paused" />
            <FilterChip label="Complete" />
            <FilterChip label="Failed" />
            <FilterChip label="Rejected" />
          </div>
        </div>

        <table style={{ borderCollapse: "collapse", width: "100%" }}>
          <thead>
            <tr style={{ background: "var(--pw-paper-2)" }}>
              <Th>Run</Th>
              <Th>Feature</Th>
              <Th>Stage</Th>
              <Th>Risk</Th>
              <Th>Status</Th>
              <Th>Tests</Th>
              <Th style={{ textAlign: "right" }}>Started</Th>
            </tr>
          </thead>
          <tbody>
            {recent.map((r, i) => (
              <RunRow
                key={r.id}
                run={r}
                isLast={i === recent.length - 1}
                onOpen={() => onOpen(r.id)}
              />
            ))}
          </tbody>
        </table>
      </Card>

      <div style={{
        marginTop: 14, fontFamily: "var(--pw-font-mono)", fontSize: 10,
        color: "var(--pw-fg-subtle)", letterSpacing: "0.04em",
      }}>
        GET /runs · LIMIT 20 · ORDER BY created_at DESC
      </div>
    </div>
  );
}

function Stat({ label, value, accent = false }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end" }}>
      <Eyebrow style={{ color: accent ? "var(--pw-copper-600)" : "var(--pw-fg-subtle)" }}>{label}</Eyebrow>
      <div style={{
        fontFamily: "var(--pw-font-sans)", fontSize: 18, fontWeight: 600,
        color: accent ? "var(--pw-copper-700)" : "var(--pw-fg)",
        letterSpacing: "-0.01em",
      }}>{value}</div>
    </div>
  );
}

function FilterChip({ label, active = false }) {
  return (
    <button style={{
      fontFamily: "var(--pw-font-sans)", fontSize: 12,
      padding: "3px 9px",
      background: active ? "var(--pw-ink-900)" : "transparent",
      color: active ? "var(--pw-paper)" : "var(--pw-fg-muted)",
      border: `1px solid ${active ? "var(--pw-ink-900)" : "var(--pw-border)"}`,
      borderRadius: 2,
      cursor: "pointer",
    }}>{label}</button>
  );
}

function Th({ children, style = {} }) {
  return (
    <th style={{
      textAlign: "left", padding: "10px 14px",
      borderBottom: "1px solid var(--pw-border)",
      fontFamily: "var(--pw-font-sans)", fontWeight: 500,
      fontSize: 10, letterSpacing: "0.08em",
      textTransform: "uppercase",
      color: "var(--pw-fg-subtle)",
      ...style,
    }}>{children}</th>
  );
}

function Td({ children, style = {} }) {
  return (
    <td style={{
      padding: "11px 14px",
      borderBottom: "1px solid var(--pw-border)",
      fontSize: 13, verticalAlign: "middle",
      ...style,
    }}>{children}</td>
  );
}

function RunRow({ run, isLast, onOpen }) {
  const [hover, setHover] = useState(false);
  const rowStyle = {
    cursor: "pointer",
    background: hover ? "var(--pw-steel-50)" : "transparent",
    transition: "background 80ms ease-out",
  };
  const ratio = `${run.tests_passed}/${run.tests_total}`;
  return (
    <tr
      onClick={onOpen}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={rowStyle}
    >
      <Td><Mono>{shortId(run.id)}</Mono></Td>
      <Td>
        <div style={{ color: "var(--pw-fg)" }}>{run.feature_description}</div>
        <div style={{ fontFamily: "var(--pw-font-mono)", fontSize: 11, color: "var(--pw-fg-subtle)", marginTop: 2 }}>
          {run.files_changed.length} file{run.files_changed.length === 1 ? "" : "s"} · {run.duration_seconds}s
        </div>
      </Td>
      <Td><Mono muted>{run.current_step}</Mono></Td>
      <Td><RiskBadge level={run.risk_level} /></Td>
      <Td><StatusPill status={run.status} /></Td>
      <Td>
        <Mono style={{
          color: run.tests_failed > 0 ? "var(--pw-fail-600)" : "var(--pw-pass-600)",
          fontWeight: 500,
        }}>{ratio}</Mono>
      </Td>
      <Td style={{ textAlign: "right" }}>
        <Mono muted style={{ fontSize: 11 }}>{relTime(run.created_at)}</Mono>
      </Td>
    </tr>
  );
}

Object.assign(window, { RunsList });
