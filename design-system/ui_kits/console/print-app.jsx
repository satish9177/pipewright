// Print app — renders each main Console screen as a separate page.
// Sidebar nav is decorative (not interactive). Each screen is its own A-sized page.

function PrintApp() {
  const data = window.PW_DATA;
  const pendingGateCount = data.runs.filter(r => r.status === "paused").length;
  const pausedRun = data.runs.find(r => r.status === "paused");
  const completedRun = data.runs.find(r => r.status === "complete");

  // Static routes — clicks are no-ops in print
  const noop = () => {};

  const pages = [
    {
      label: "01 Runs list",
      route: { screen: "runs" },
      body: <RunsList runs={data.runs} onOpen={noop} onOpenGate={noop} />,
    },
    {
      label: "02 Run detail",
      route: { screen: "runs", runId: pausedRun.id },
      body: <RunDetail run={pausedRun} onOpenGate={noop} onBack={noop} />,
    },
    {
      label: "03 Approval queue",
      route: { screen: "gates" },
      body: (
        <ApprovalQueueStatic
          gates={data.runs.filter(r => r.status === "paused")}
        />
      ),
    },
    {
      label: "04 Gate inspector — diff",
      route: { screen: "gates", gateId: data.gate.id },
      body: (
        <GateInspector
          gate={data.gate}
          run={pausedRun}
          decision="pending"
          onApprove={noop}
          onReject={noop}
          onBack={noop}
        />
      ),
    },
    {
      label: "05 Memory",
      route: { screen: "memory" },
      body: <Memory facts={data.memory} />,
    },
    {
      label: "06 Start a run",
      route: { screen: "start" },
      body: <StartRun onCancel={noop} onLaunch={noop} />,
    },
  ];

  return (
    <>
      {pages.map((p, i) => (
        <div key={i} className="print-page" data-screen-label={p.label}>
          <AppShell
            route={p.route}
            onNavigate={noop}
            pendingGateCount={pendingGateCount}
          >
            {p.body}
          </AppShell>
        </div>
      ))}
    </>
  );
}

// Static approval queue without the in-app react state callbacks
function ApprovalQueueStatic({ gates }) {
  const gateDetail = window.PW_DATA.gate;
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
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {gates.map(r => (
          <Card key={r.id} style={{ padding: "16px 20px" }}>
            <div style={{ display: "flex", alignItems: "flex-start", gap: 16 }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
                  <Tag tone="copper">APPROVAL · PAUSED</Tag>
                  <RiskBadge level={r.risk_level} />
                  <Mono muted style={{ fontSize: 11 }}>gate {shortId(r.gate_id)}</Mono>
                </div>
                <div style={{
                  fontFamily: "var(--pw-font-sans)", fontSize: 16, fontWeight: 600,
                  color: "var(--pw-fg)", marginBottom: 4, lineHeight: 1.3,
                }}>{r.feature_description}</div>
                <div style={{ display: "flex", gap: 16, fontSize: 11 }}>
                  <Mono muted>tests <span style={{ color: "var(--pw-pass-600)" }}>{r.tests_passed}/{r.tests_total}</span></Mono>
                  <Mono muted>{r.files_changed.length} files</Mono>
                  <Mono muted>{r.duration_seconds}s</Mono>
                  <Mono muted>{relTime(r.created_at)}</Mono>
                </div>
              </div>
              <Button variant="primary" size="sm">Review →</Button>
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}

const rootEl = document.getElementById("root");
ReactDOM.createRoot(rootEl).render(<PrintApp />);

// Auto-print after fonts and React mount have settled.
(async function autoPrint() {
  try {
    if (document.fonts && document.fonts.ready) {
      await document.fonts.ready;
    }
  } catch (e) {}
  // Wait for Babel-transpiled scripts and React mount to finish painting.
  await new Promise(r => setTimeout(r, 800));
  window.print();
})();
