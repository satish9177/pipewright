// Gate inspector — the marquee approval screen.
// Diff, summary, tests, plan, approve/reject actions.

function GateInspector({ gate, run, decision, onApprove, onReject, onBack }) {
  const [tab, setTab] = useState("diff");
  const [rejectOpen, setRejectOpen] = useState(false);
  const [reason, setReason] = useState("");

  if (!gate || !run) return null;
  const decided = decision !== "pending";

  return (
    <div style={{ padding: "0", display: "flex", flexDirection: "column", height: "100%" }}>
      {/* Sticky gate header */}
      <div style={{
        padding: "20px 28px 18px",
        borderBottom: "1px solid var(--pw-border)",
        background: "var(--pw-bg)",
        position: "sticky", top: 0, zIndex: 2,
      }}>
        <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 18 }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <Eyebrow tone="copper" style={{ marginBottom: 6 }}>
              APPROVAL · PIPELINE PAUSED · HUMAN DECISION NEEDED
            </Eyebrow>
            <h1 style={{
              fontFamily: "var(--pw-font-sans)", fontSize: 22, fontWeight: 600,
              letterSpacing: "-0.01em", margin: "0 0 6px", lineHeight: 1.25,
            }}>{run.feature_description}</h1>
            <div style={{ display: "flex", gap: 14, alignItems: "center", fontSize: 11 }}>
              <Mono muted>gate_id <span style={{ color: "var(--pw-fg)" }}>{shortId(gate.id)}</span></Mono>
              <Mono muted>run_id <span style={{ color: "var(--pw-fg)" }}>{shortId(run.id)}</span></Mono>
              <Mono muted>paused {relTime(run.created_at)}</Mono>
            </div>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <RiskBadge level={gate.risk_level} />
            <Button variant="ghost" size="sm" onClick={onBack}>← Back</Button>
          </div>
        </div>

        {/* Stats strip */}
        <div style={{
          display: "grid", gridTemplateColumns: "repeat(4, 1fr)",
          gap: 0, marginTop: 18,
          border: "1px solid var(--pw-border)", borderRadius: 4,
          background: "var(--pw-bg-elev)",
        }}>
          <StatCell label="TESTS"    value={`${gate.tests.passed}/${gate.tests.total}`}
                    sub="passed" tone={gate.tests.failed > 0 ? "fail" : "pass"} />
          <StatCell label="DURATION" value={`${gate.tests.duration_seconds}s`} sub="test run" />
          <StatCell label="FILES"    value={String(run.files_changed.length)} sub="changed" />
          <StatCell label="ROLLBACK" value="available" sub="pre-patch hash stored" />
        </div>
      </div>

      {/* Tabs */}
      <div style={{
        display: "flex", gap: 0, padding: "0 28px",
        borderBottom: "1px solid var(--pw-border)",
        background: "var(--pw-bg)",
        position: "sticky", top: 0, zIndex: 1,
      }}>
        {["diff", "summary", "plan", "log"].map(t => (
          <TabBtn key={t} active={tab === t} onClick={() => setTab(t)} label={t.toUpperCase()} />
        ))}
        <div style={{ flex: 1 }} />
        <button style={{
          fontFamily: "var(--pw-font-mono)", fontSize: 11, letterSpacing: "0.06em",
          color: "var(--pw-fg-subtle)", padding: "12px 0",
          background: "transparent", border: "none", cursor: "pointer",
        }}>COPY RAW DIFF</button>
      </div>

      {/* Tab content — scrollable */}
      <div style={{ flex: 1, overflow: "auto" }}>
        {tab === "diff" && <DiffView diff={gate.diff} />}
        {tab === "summary" && <SummaryView gate={gate} run={run} />}
        {tab === "plan" && <PlanView plan={gate.plan} />}
        {tab === "log" && <LogView log={gate.log} runId={run.id} />}
      </div>

      {/* Sticky decision bar */}
      <div style={{
        borderTop: "1px solid var(--pw-border)",
        background: "var(--pw-paper-2)",
        padding: "14px 28px",
        display: "flex", alignItems: "center", gap: 14,
      }}>
        {decided ? (
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <Tag tone={decision === "approved" ? "pass" : "fail"}>
              {decision === "approved" ? "APPROVED" : "REJECTED"}
            </Tag>
            <span style={{ fontSize: 13, color: "var(--pw-fg)" }}>
              {decision === "approved"
                ? "Changes merged. Checkpoint saved."
                : "Rollback triggered. No changes merged."}
            </span>
          </div>
        ) : (
          <>
            <div style={{ flex: 1, fontSize: 13, color: "var(--pw-fg-muted)" }}>
              <strong style={{ color: "var(--pw-fg)", fontWeight: 600 }}>Approve and merge?</strong>{" "}
              Tests pass, rollback available, risk medium.
            </div>
            <Button variant="danger" onClick={() => setRejectOpen(true)}>Reject</Button>
            <Button variant="primary" onClick={onApprove}>Approve and merge</Button>
          </>
        )}
      </div>

      {rejectOpen && (
        <RejectModal
          reason={reason}
          setReason={setReason}
          onCancel={() => setRejectOpen(false)}
          onConfirm={() => {
            onReject(reason || "No reason provided");
            setRejectOpen(false);
          }}
        />
      )}
    </div>
  );
}

function StatCell({ label, value, sub, tone = "default" }) {
  const valueColor = tone === "fail" ? "var(--pw-fail-600)" : tone === "pass" ? "var(--pw-pass-600)" : "var(--pw-fg)";
  return (
    <div style={{
      padding: "14px 18px",
      borderRight: "1px solid var(--pw-border)",
    }}>
      <Eyebrow style={{ marginBottom: 4 }}>{label}</Eyebrow>
      <div style={{
        fontFamily: "var(--pw-font-sans)", fontSize: 22, fontWeight: 600,
        color: valueColor, letterSpacing: "-0.01em", lineHeight: 1,
      }}>{value}</div>
      <Mono muted style={{ fontSize: 10, marginTop: 4, display: "block" }}>{sub}</Mono>
    </div>
  );
}

function TabBtn({ active, onClick, label }) {
  return (
    <button onClick={onClick} style={{
      fontFamily: "var(--pw-font-mono)", fontSize: 11,
      letterSpacing: "0.08em", textTransform: "uppercase",
      padding: "12px 16px",
      background: "transparent",
      border: "none",
      borderBottom: `2px solid ${active ? "var(--pw-ink-900)" : "transparent"}`,
      color: active ? "var(--pw-ink-900)" : "var(--pw-fg-subtle)",
      cursor: "pointer",
      marginBottom: -1,
    }}>{label}</button>
  );
}

function DiffView({ diff }) {
  const lines = diff.split("\n");
  return (
    <div style={{
      background: "var(--pw-term-bg)",
      minHeight: "100%",
      padding: "16px 0",
      fontFamily: "var(--pw-font-mono)", fontSize: 12.5, lineHeight: 1.65,
    }}>
      {lines.map((line, i) => {
        let color = "#E6E0D0";
        let bg = "transparent";
        if (line.startsWith("---") || line.startsWith("+++")) { color = "#E6E0D0"; }
        else if (line.startsWith("@@")) { color = "#C5A24A"; }
        else if (line.startsWith("+"))  { color = "#5FB37A"; bg = "rgba(95,179,122,0.08)"; }
        else if (line.startsWith("-"))  { color = "#E08F8F"; bg = "rgba(224,143,143,0.08)"; }
        else { color = "#8A929C"; }
        return (
          <div key={i} style={{ display: "flex", background: bg }}>
            <div style={{
              flex: "0 0 50px",
              textAlign: "right",
              padding: "0 12px 0 16px",
              color: "#3D4550",
              userSelect: "none",
            }}>{i + 1}</div>
            <div style={{ color, flex: 1, paddingRight: 16, whiteSpace: "pre" }}>
              {line || " "}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function SummaryView({ gate, run }) {
  return (
    <div style={{ padding: "24px 28px", maxWidth: 800 }}>
      <Eyebrow tone="info" style={{ marginBottom: 8 }}>AI SUMMARY</Eyebrow>
      <p style={{
        fontFamily: "var(--pw-font-sans)", fontSize: 16, lineHeight: 1.55,
        color: "var(--pw-fg)", margin: "0 0 22px",
      }}>{gate.ai_summary}</p>

      <Eyebrow style={{ marginBottom: 8 }}>FILES CHANGED</Eyebrow>
      <Card style={{ overflow: "hidden", marginBottom: 22 }}>
        {run.files_changed.map((f, i) => (
          <div key={f} style={{
            padding: "10px 14px", display: "flex", alignItems: "center", gap: 12,
            borderTop: i === 0 ? "none" : "1px solid var(--pw-border)",
          }}>
            <Tag tone="info" style={{ fontSize: 9 }}>MOD</Tag>
            <Mono style={{ fontSize: 12, flex: 1 }}>{f}</Mono>
            <Mono muted style={{ fontSize: 10 }}>backed up</Mono>
          </div>
        ))}
      </Card>

      <Eyebrow style={{ marginBottom: 8 }}>POTENTIAL MEMORY ENTRIES</Eyebrow>
      <Card style={{ padding: "12px 14px", borderStyle: "dashed", background: "transparent" }}>
        <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, lineHeight: 1.7, color: "var(--pw-fg-muted)" }}>
          <li>Gemini free tier 429 retries should wait 60s — confirmed working in production.</li>
          <li>asyncio.sleep is always required inside async handlers (never time.sleep).</li>
        </ul>
        <div style={{ marginTop: 10, display: "flex", gap: 8 }}>
          <Button size="sm" variant="secondary">Add to memory</Button>
          <Button size="sm" variant="ghost">Dismiss</Button>
        </div>
      </Card>
    </div>
  );
}

function PlanView({ plan }) {
  return (
    <div style={{ padding: "24px 28px", maxWidth: 800 }}>
      <Eyebrow tone="info" style={{ marginBottom: 6 }}>PLANNER HANDOFF · GOAL</Eyebrow>
      <p style={{
        fontFamily: "var(--pw-font-sans)", fontSize: 17, lineHeight: 1.5,
        color: "var(--pw-fg)", margin: "0 0 22px",
      }}>{plan.goal}</p>

      <Eyebrow style={{ marginBottom: 8 }}>STEPS</Eyebrow>
      <ol style={{ margin: "0 0 22px", paddingLeft: 22, fontSize: 14, lineHeight: 1.7, color: "var(--pw-fg)" }}>
        {plan.steps.map((s, i) => <li key={i} style={{ marginBottom: 4 }}>{s}</li>)}
      </ol>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 18 }}>
        <div>
          <Eyebrow style={{ marginBottom: 8 }}>OUT OF SCOPE</Eyebrow>
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, lineHeight: 1.7, color: "var(--pw-fg-muted)" }}>
            {plan.out_of_scope.map((s, i) => <li key={i}>{s}</li>)}
          </ul>
        </div>
        <div>
          <Eyebrow style={{ marginBottom: 8, color: "var(--pw-wait-600)" }}>RISKS</Eyebrow>
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, lineHeight: 1.7, color: "var(--pw-fg-muted)" }}>
            {plan.risks.map((s, i) => <li key={i}>{s}</li>)}
          </ul>
        </div>
      </div>
    </div>
  );
}

function LogView({ log, runId }) {
  return (
    <div style={{
      background: "var(--pw-term-bg)",
      minHeight: "100%",
      padding: "20px 28px",
      fontFamily: "var(--pw-font-mono)", fontSize: 12.5, lineHeight: 1.7,
    }}>
      <div style={{ color: "#6B7480", marginBottom: 8 }}>{`# pipeline log · run_id=${shortId(runId)}`}</div>
      {log.map((line, i) => {
        const colorMap = { muted: "#6B7480", info: "#C5A24A", pass: "#5FB37A", fail: "#E08F8F" };
        return (
          <div key={i} style={{ color: colorMap[line.level] || "#E6E0D0" }}>
            [{line.tag}] {line.text}
          </div>
        );
      })}
    </div>
  );
}

function RejectModal({ reason, setReason, onCancel, onConfirm }) {
  return (
    <div style={{
      position: "fixed", inset: 0, background: "rgba(14,17,22,0.5)",
      display: "flex", alignItems: "center", justifyContent: "center", zIndex: 10,
    }}>
      <Card style={{ width: 460, padding: "20px 22px" }}>
        <Eyebrow tone="copper" style={{ marginBottom: 6 }}>REJECT · REASON REQUIRED</Eyebrow>
        <h3 style={{ fontFamily: "var(--pw-font-sans)", fontSize: 18, fontWeight: 600, margin: "0 0 12px" }}>
          Why are you rejecting this gate?
        </h3>
        <p style={{ fontSize: 13, color: "var(--pw-fg-muted)", margin: "0 0 12px", lineHeight: 1.5 }}>
          Rollback will be triggered. The pipeline marks the run as rejected and stores your reason.
        </p>
        <textarea
          value={reason}
          onChange={e => setReason(e.target.value)}
          placeholder="Touches checkpoint logic. Discuss before merging."
          rows={3}
          style={{
            width: "100%", fontFamily: "var(--pw-font-sans)", fontSize: 13,
            padding: "8px 10px",
            border: "1px solid var(--pw-border)", borderRadius: 2,
            outline: "none", resize: "vertical",
            boxSizing: "border-box",
          }}
        />
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 12 }}>
          <Button variant="ghost" onClick={onCancel}>Cancel</Button>
          <Button variant="danger" onClick={onConfirm} disabled={!reason.trim()}>Reject and rollback</Button>
        </div>
      </Card>
    </div>
  );
}

Object.assign(window, { GateInspector });
