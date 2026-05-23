// Run detail — pipeline stages, plan, files, terminal log.

const PIPELINE_STAGES = [
  { id: "plan",     label: "PLAN",     hint: "planner.py" },
  { id: "code",     label: "CODE",     hint: "coder.py" },
  { id: "patch",    label: "PATCH",    hint: "patch_applier.py" },
  { id: "test",     label: "TEST",     hint: "tester.py" },
  { id: "approval", label: "APPROVAL", hint: "approval_gate.py" },
];

function stageState(run, stageId) {
  const stageIdx = PIPELINE_STAGES.findIndex(s => s.id === stageId);
  const currentIdx = PIPELINE_STAGES.findIndex(s => s.id === run.current_step);
  if (run.current_step === "done") return "done";
  if (run.status === "failed" && stageIdx === currentIdx) return "failed";
  if (run.status === "rejected" && stageIdx === currentIdx) return "failed";
  if (stageIdx < currentIdx) return "done";
  if (stageIdx === currentIdx) return run.status === "paused" ? "active" : "active";
  return "todo";
}

function RunDetail({ run, onOpenGate, onBack }) {
  if (!run) return null;
  const showGate = run.status === "paused";

  return (
    <div style={{ padding: "24px 28px", maxWidth: 1200, margin: "0 auto" }}>
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", marginBottom: 22 }}>
        <div>
          <Eyebrow>RUN · <span style={{ fontFamily: "var(--pw-font-mono)" }}>{shortId(run.id)}</span></Eyebrow>
          <h1 style={{
            fontFamily: "var(--pw-font-sans)", fontSize: 24, fontWeight: 600,
            letterSpacing: "-0.01em", margin: "6px 0 4px", maxWidth: 720,
          }}>{run.feature_description}</h1>
          <div style={{ display: "flex", alignItems: "center", gap: 14, marginTop: 6 }}>
            <StatusPill status={run.status} />
            <RiskBadge level={run.risk_level} />
            <Mono muted style={{ fontSize: 11 }}>started {relTime(run.created_at)} · {run.duration_seconds}s</Mono>
          </div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          {showGate && (
            <Button variant="primary" onClick={() => onOpenGate(run.gate_id)}>
              Review gate →
            </Button>
          )}
          <Button variant="ghost" size="sm" onClick={onBack}>← All runs</Button>
        </div>
      </div>

      {/* Pipeline stages bar */}
      <Card style={{ marginBottom: 18, padding: "18px 22px" }}>
        <Eyebrow style={{ marginBottom: 12 }}>PIPELINE</Eyebrow>
        <div style={{ display: "flex", alignItems: "center", gap: 0 }}>
          {PIPELINE_STAGES.map((s, i) => {
            const state = stageState(run, s.id);
            return (
              <React.Fragment key={s.id}>
                <Stage state={state} label={s.label} hint={s.hint} />
                {i < PIPELINE_STAGES.length - 1 && <Connector state={state} />}
              </React.Fragment>
            );
          })}
        </div>
      </Card>

      <div style={{ display: "grid", gridTemplateColumns: "1.2fr 1fr", gap: 18 }}>
        {/* Plan card */}
        <Card style={{ padding: "18px 22px" }}>
          <Eyebrow tone="info" style={{ marginBottom: 8 }}>PLAN · PLANNER HANDOFF</Eyebrow>
          <div style={{ fontFamily: "var(--pw-font-sans)", fontSize: 15, color: "var(--pw-fg)", lineHeight: 1.5, marginBottom: 14 }}>
            {run.goal}
          </div>
          <Eyebrow style={{ marginBottom: 6 }}>STEPS</Eyebrow>
          <ol style={{ margin: 0, paddingLeft: 18, fontSize: 13, color: "var(--pw-fg-muted)", lineHeight: 1.6 }}>
            {(run.gate_id ? (window.PW_DATA.gate.plan.steps || []) : []).map((step, i) => (
              <li key={i}>{step}</li>
            ))}
            {!run.gate_id && <li style={{ color: "var(--pw-fg-subtle)" }}>Plan steps available on the run's gate.</li>}
          </ol>
        </Card>

        {/* Files & tests card */}
        <Card style={{ padding: "18px 22px" }}>
          <Eyebrow style={{ marginBottom: 8 }}>FILES CHANGED</Eyebrow>
          <div style={{ display: "flex", flexDirection: "column", gap: 4, marginBottom: 16 }}>
            {run.files_changed.map(f => (
              <div key={f} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <Tag tone="info" style={{ fontSize: 9 }}>MOD</Tag>
                <Mono style={{ fontSize: 12 }}>{f}</Mono>
              </div>
            ))}
          </div>
          <Eyebrow style={{ marginBottom: 8 }}>TESTS</Eyebrow>
          <div style={{ display: "flex", gap: 18, alignItems: "baseline" }}>
            <div>
              <div style={{ fontFamily: "var(--pw-font-sans)", fontSize: 28, fontWeight: 600,
                color: run.tests_failed > 0 ? "var(--pw-fail-600)" : "var(--pw-pass-600)" }}>
                {run.tests_passed}/{run.tests_total}
              </div>
              <Mono muted style={{ fontSize: 10 }}>PASSED</Mono>
            </div>
            <div>
              <div style={{ fontFamily: "var(--pw-font-sans)", fontSize: 20, fontWeight: 500, color: "var(--pw-fg-muted)" }}>
                {run.duration_seconds}s
              </div>
              <Mono muted style={{ fontSize: 10 }}>DURATION</Mono>
            </div>
            {run.tests_failed > 0 && (
              <div>
                <div style={{ fontFamily: "var(--pw-font-sans)", fontSize: 20, fontWeight: 500, color: "var(--pw-fail-600)" }}>
                  {run.tests_failed}
                </div>
                <Mono muted style={{ fontSize: 10 }}>FAILED</Mono>
              </div>
            )}
          </div>
        </Card>
      </div>

      {/* Terminal log */}
      <Card style={{
        marginTop: 18, padding: 0, background: "var(--pw-term-bg)",
        border: "1px solid var(--pw-ink-900)",
      }}>
        <div style={{
          display: "flex", alignItems: "center", gap: 10,
          padding: "10px 16px",
          borderBottom: "1px solid #2A3038",
        }}>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#E6E0D0" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>
          <Mono style={{ color: "#E6E0D0", fontSize: 11, letterSpacing: "0.04em" }}>terminal log</Mono>
          <span style={{ flex: 1 }} />
          <Mono style={{ color: "#6B7480", fontSize: 10 }}>run_id={shortId(run.id)}</Mono>
        </div>
        <div style={{
          padding: "12px 16px", fontFamily: "var(--pw-font-mono)",
          fontSize: 12, lineHeight: 1.65, maxHeight: 260, overflow: "auto",
        }}>
          {(window.PW_DATA.gate.log || []).map((line, i) => (
            <LogLine key={i} line={line} />
          ))}
        </div>
      </Card>
    </div>
  );
}

function Stage({ state, label, hint }) {
  let borderColor = "var(--pw-border)";
  let dotColor = "#B5BAC2";
  let bg = "#fff";
  let textColor = "var(--pw-fg)";
  let borderStyle = "solid";
  let pulse = false;
  if (state === "done")   { borderColor = "var(--pw-pass-600)"; dotColor = "var(--pw-pass-600)"; }
  if (state === "active") { borderColor = "var(--pw-ink-900)"; bg = "var(--pw-ink-900)"; textColor = "var(--pw-paper)"; dotColor = "var(--pw-copper-500)"; pulse = true; }
  if (state === "failed") { borderColor = "var(--pw-fail-600)"; dotColor = "var(--pw-fail-600)"; }
  if (state === "todo")   { borderStyle = "dashed"; borderColor = "var(--pw-steel-300)"; textColor = "var(--pw-fg-subtle)"; }

  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 8,
      padding: "8px 12px",
      border: `1px ${borderStyle} ${borderColor}`,
      background: bg, color: textColor,
      borderRadius: 2,
      fontFamily: "var(--pw-font-mono)", fontSize: 11, letterSpacing: "0.06em",
    }}>
      <span style={{
        width: 8, height: 8, borderRadius: "50%", background: dotColor,
        animation: pulse ? "pwPulse 1.2s ease-in-out infinite" : "none",
      }} />
      <div>
        <div>{label}</div>
        <div style={{ fontSize: 9, opacity: 0.6, marginTop: 1 }}>{hint}</div>
      </div>
    </div>
  );
}

function Connector({ state }) {
  return <div style={{
    flex: "0 0 18px", height: 1,
    background: state === "done" ? "var(--pw-pass-600)" : "var(--pw-border)",
  }} />;
}

function LogLine({ line }) {
  const colorMap = {
    muted: "#6B7480",
    info:  "#C5A24A",
    pass:  "#5FB37A",
    fail:  "#E08F8F",
  };
  return (
    <div style={{ color: colorMap[line.level] || "#E6E0D0" }}>
      [{line.tag}] {line.text}
    </div>
  );
}

Object.assign(window, { RunDetail });
