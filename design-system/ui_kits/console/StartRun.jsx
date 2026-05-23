// Start a run — the launch form.

function StartRun({ onCancel, onLaunch }) {
  const [feature, setFeature] = useState("");
  return (
    <div style={{ padding: "32px 28px", maxWidth: 720, margin: "0 auto" }}>
      <Eyebrow>POST /run</Eyebrow>
      <h1 style={{
        fontFamily: "var(--pw-font-sans)", fontSize: 28, fontWeight: 600,
        letterSpacing: "-0.01em", margin: "6px 0 22px",
      }}>Start a pipeline run</h1>

      <Card style={{ padding: "20px 22px" }}>
        <Eyebrow style={{ marginBottom: 6 }}>FEATURE DESCRIPTION</Eyebrow>
        <textarea
          value={feature}
          onChange={e => setFeature(e.target.value)}
          rows={4}
          placeholder="Describe what the pipeline should build. One sentence is best."
          style={{
            width: "100%", boxSizing: "border-box",
            fontFamily: "var(--pw-font-sans)", fontSize: 14, lineHeight: 1.5,
            padding: "10px 12px",
            border: "1px solid var(--pw-border)",
            borderRadius: 2, outline: "none", resize: "vertical",
          }}
        />
        <div style={{ marginTop: 6, display: "flex", justifyContent: "space-between" }}>
          <Mono muted style={{ fontSize: 10 }}>SENT TO PLANNER AS-IS</Mono>
          <Mono muted style={{ fontSize: 10 }}>{feature.length} chars</Mono>
        </div>

        <div style={{
          marginTop: 18, paddingTop: 16, borderTop: "1px solid var(--pw-border)",
          display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16,
        }}>
          <div>
            <Eyebrow style={{ marginBottom: 6 }}>TARGET REPO</Eyebrow>
            <Mono style={{ fontSize: 12 }}>/Users/sat/code/ai-workflow-platform</Mono>
            <Mono muted style={{ fontSize: 10, display: "block", marginTop: 2 }}>.env · TARGET_REPO_PATH</Mono>
          </div>
          <div>
            <Eyebrow style={{ marginBottom: 6 }}>MODEL</Eyebrow>
            <Mono style={{ fontSize: 12 }}>gemini-2.5-flash</Mono>
            <Mono muted style={{ fontSize: 10, display: "block", marginTop: 2 }}>temperature 0.2 · timeout 60s</Mono>
          </div>
        </div>

        <div style={{
          marginTop: 18, padding: "10px 12px",
          background: "var(--pw-wait-50)",
          border: "1px solid var(--pw-wait-100)",
          borderRadius: 2,
          display: "flex", alignItems: "flex-start", gap: 10,
          fontSize: 12, color: "var(--pw-wait-700)",
        }}>
          <Tag tone="wait" style={{ fontSize: 10 }}>NOTICE</Tag>
          <div style={{ flex: 1, lineHeight: 1.5 }}>
            Pipeline will pause at the approval gate. You'll need to review and approve before changes merge.
            Tests must pass or rollback is automatic.
          </div>
        </div>

        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 18 }}>
          <Button variant="ghost" onClick={onCancel}>Cancel</Button>
          <Button variant="primary" onClick={() => onLaunch(feature)} disabled={feature.trim().length < 10}>
            Start run →
          </Button>
        </div>
      </Card>
    </div>
  );
}

Object.assign(window, { StartRun });
