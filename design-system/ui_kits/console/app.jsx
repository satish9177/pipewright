// app.jsx — Pipewright Console root. Mounts AppShell + screens with simple state routing.

function App() {
  const [data, setData] = useState(() => JSON.parse(JSON.stringify(window.PW_DATA)));
  const [route, setRoute] = useState({ screen: "runs" });
  const [decision, setDecision] = useState("pending"); // pending | approved | rejected

  useEffect(() => {
    if (window.lucide) window.lucide.createIcons();
  });

  const pendingGateCount = data.runs.filter(r => r.status === "paused").length;
  const selectedRun = route.runId ? data.runs.find(r => r.id === route.runId) : null;
  const gate = data.gate;

  const handleApprove = () => {
    setDecision("approved");
    setData(prev => ({
      ...prev,
      runs: prev.runs.map(r =>
        r.id === gate.run_id ? { ...r, status: "complete", current_step: "done" } : r
      ),
    }));
  };

  const handleReject = (reason) => {
    setDecision("rejected");
    setData(prev => ({
      ...prev,
      runs: prev.runs.map(r =>
        r.id === gate.run_id ? { ...r, status: "rejected", current_step: "approval", rejection_reason: reason } : r
      ),
    }));
  };

  const handleLaunch = (feature) => {
    // Demo: prepend a synthetic run, take user back to runs list
    const newRun = {
      id: crypto.randomUUID(),
      feature_description: feature,
      status: "paused",
      current_step: "approval",
      created_at: new Date().toISOString(),
      gate_id: gate.id,
      risk_level: "medium",
      duration_seconds: 0,
      tests_total: 0, tests_passed: 0, tests_failed: 0,
      files_changed: [],
      goal: feature,
    };
    setData(prev => ({ ...prev, runs: [newRun, ...prev.runs] }));
    setRoute({ screen: "runs" });
  };

  let screen;
  if (route.screen === "runs" && route.runId) {
    screen = (
      <RunDetail
        run={selectedRun}
        onOpenGate={(gateId) => setRoute({ screen: "gates", gateId })}
        onBack={() => setRoute({ screen: "runs" })}
      />
    );
  } else if (route.screen === "runs") {
    screen = (
      <RunsList
        runs={data.runs}
        onOpen={(runId) => setRoute({ screen: "runs", runId })}
        onOpenGate={(gateId) => setRoute({ screen: "gates", gateId })}
      />
    );
  } else if (route.screen === "gates" && route.gateId) {
    const gateRun = data.runs.find(r => r.id === gate.run_id);
    screen = (
      <GateInspector
        gate={gate}
        run={gateRun}
        decision={decision}
        onApprove={handleApprove}
        onReject={handleReject}
        onBack={() => setRoute({ screen: "gates" })}
      />
    );
  } else if (route.screen === "gates") {
    const paused = data.runs.filter(r => r.status === "paused");
    screen = <ApprovalQueue gates={paused} gateDetail={gate}
                            onOpen={(gateId) => setRoute({ screen: "gates", gateId })} />;
  } else if (route.screen === "memory") {
    screen = <Memory facts={data.memory} />;
  } else if (route.screen === "start") {
    screen = <StartRun onCancel={() => setRoute({ screen: "runs" })} onLaunch={handleLaunch} />;
  }

  return (
    <AppShell route={route} onNavigate={setRoute} pendingGateCount={pendingGateCount}>
      {screen}
    </AppShell>
  );
}

function ApprovalQueue({ gates, gateDetail, onOpen }) {
  return (
    <div style={{ padding: "24px 28px", maxWidth: 1100, margin: "0 auto" }}>
      <Eyebrow>APPROVAL QUEUE</Eyebrow>
      <h1 style={{
        fontFamily: "var(--pw-font-sans)", fontSize: 28, fontWeight: 600,
        letterSpacing: "-0.01em", margin: "4px 0 4px",
      }}>Pending decisions</h1>
      <p style={{ fontSize: 14, color: "var(--pw-fg-muted)", margin: "0 0 22px" }}>
        Pipeline runs paused at the human approval gate. Review each diff before merging.
      </p>

      {gates.length === 0 ? (
        <div style={{
          border: "1px dashed var(--pw-steel-300)", borderRadius: 4,
          padding: "32px 28px", textAlign: "left",
        }}>
          <Tag tone="muted">EMPTY</Tag>
          <h3 style={{ fontFamily: "var(--pw-font-sans)", fontSize: 16, fontWeight: 600, margin: "6px 0 4px" }}>
            No pending gates
          </h3>
          <p style={{ fontSize: 13, color: "var(--pw-fg-muted)", margin: 0, maxWidth: 480, lineHeight: 1.5 }}>
            All runs are complete or in-progress.
          </p>
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {gates.map(r => (
            <GateQueueCard key={r.id} run={r} gate={gateDetail} onOpen={() => onOpen(r.gate_id)} />
          ))}
        </div>
      )}
    </div>
  );
}

function GateQueueCard({ run, gate, onOpen }) {
  return (
    <Card style={{ padding: "16px 20px", cursor: "pointer" }} >
      <div style={{ display: "flex", alignItems: "flex-start", gap: 16 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
            <Tag tone="copper">APPROVAL · PAUSED</Tag>
            <RiskBadge level={run.risk_level} />
            <Mono muted style={{ fontSize: 11 }}>gate {shortId(run.gate_id)}</Mono>
          </div>
          <div style={{
            fontFamily: "var(--pw-font-sans)", fontSize: 16, fontWeight: 600,
            color: "var(--pw-fg)", marginBottom: 4, lineHeight: 1.3,
          }}>{run.feature_description}</div>
          <div style={{ display: "flex", gap: 16, fontSize: 11 }}>
            <Mono muted>tests <span style={{ color: "var(--pw-pass-600)" }}>{run.tests_passed}/{run.tests_total}</span></Mono>
            <Mono muted>{run.files_changed.length} files</Mono>
            <Mono muted>{run.duration_seconds}s</Mono>
            <Mono muted>{relTime(run.created_at)}</Mono>
          </div>
        </div>
        <Button variant="primary" size="sm" onClick={onOpen}>Review →</Button>
      </div>
    </Card>
  );
}

const rootEl = document.getElementById("root");
ReactDOM.createRoot(rootEl).render(<App />);
