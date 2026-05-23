// Memory — project facts editor.
// Backed by the memory_facts table. Hard facts the planner reads as context.

function Memory({ facts }) {
  return (
    <div style={{ padding: "24px 28px", maxWidth: 1000, margin: "0 auto" }}>
      <div style={{ display: "flex", alignItems: "flex-end", justifyContent: "space-between", marginBottom: 22 }}>
        <div>
          <Eyebrow>PROJECT MEMORY</Eyebrow>
          <h1 style={{
            fontFamily: "var(--pw-font-sans)", fontSize: 28, fontWeight: 600,
            letterSpacing: "-0.01em", margin: "4px 0 6px",
          }}>Hard facts</h1>
          <p style={{ fontSize: 14, color: "var(--pw-fg-muted)", margin: 0, maxWidth: 580 }}>
            Facts the planner reads as context on every run. Manually curated. Stale entries are flagged but kept.
          </p>
        </div>
        <Button variant="primary" size="sm">+ Add fact</Button>
      </div>

      <Card>
        <div style={{ padding: "10px 18px", borderBottom: "1px solid var(--pw-border)",
                      display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div style={{ display: "flex", gap: 16, fontSize: 12 }}>
            <Mono><span style={{ color: "var(--pw-pass-600)" }}>{facts.filter(f => !f.is_stale).length}</span> active</Mono>
            <Mono muted><span style={{ color: "var(--pw-wait-600)" }}>{facts.filter(f => f.is_stale).length}</span> stale</Mono>
          </div>
          <Mono muted style={{ fontSize: 10 }}>SENT TO PLANNER ON EVERY RUN</Mono>
        </div>
        {facts.map((f, i) => (
          <FactRow key={f.id} fact={f} isLast={i === facts.length - 1} />
        ))}
      </Card>
    </div>
  );
}

function FactRow({ fact, isLast }) {
  return (
    <div style={{
      padding: "14px 18px",
      borderBottom: isLast ? "none" : "1px solid var(--pw-border)",
      display: "flex", gap: 14, alignItems: "flex-start",
      opacity: fact.is_stale ? 0.6 : 1,
    }}>
      <div style={{ flex: "0 0 90px", paddingTop: 2 }}>
        {fact.is_stale ? (
          <Tag tone="wait">STALE</Tag>
        ) : (
          <Tag tone="pass">ACTIVE</Tag>
        )}
      </div>
      <div style={{ flex: 1 }}>
        <div style={{
          fontFamily: "var(--pw-font-sans)", fontSize: 14, lineHeight: 1.5,
          color: "var(--pw-fg)", textDecoration: fact.is_stale ? "line-through" : "none",
        }}>{fact.content}</div>
        <div style={{ display: "flex", gap: 12, marginTop: 6 }}>
          <Mono muted style={{ fontSize: 10 }}>source <span style={{ color: "var(--pw-fg)" }}>{fact.source}</span></Mono>
          <Mono muted style={{ fontSize: 10 }}>added by <span style={{ color: "var(--pw-fg)" }}>{fact.added_by}</span></Mono>
          <Mono muted style={{ fontSize: 10 }}>{relTime(fact.created_at)}</Mono>
        </div>
      </div>
      <div style={{ display: "flex", gap: 6 }}>
        <Button variant="ghost" size="sm">Edit</Button>
        <Button variant="ghost" size="sm">Archive</Button>
      </div>
    </div>
  );
}

Object.assign(window, { Memory });
