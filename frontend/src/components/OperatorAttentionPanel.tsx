import type {
  OperatorAction,
  OperatorDecisionType,
  OperatorSafetyCheck,
  OperatorSafetyCheckStatus,
  OperatorState,
  OperatorWaitingOn,
} from '@/api/client'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Separator } from '@/components/ui/separator'

// Display-only operator attention panel. It mirrors the backend operator_state
// read-model (computed on chunk reads, never persisted) and renders the current
// safe-action picture. It deliberately wires NO mutations: the real, clickable
// controls remain the existing chunk/final/PR panels below. Actions here are
// informational previews of what Pipewright considers the next safe step.

const WAITING_ON: Record<
  OperatorWaitingOn,
  { label: string; className: string }
> = {
  human: {
    label: 'Waiting on you',
    className: 'border-amber-200 bg-amber-100 text-amber-900',
  },
  system: {
    label: 'Waiting on the system',
    className: 'border-blue-200 bg-blue-100 text-blue-800',
  },
  nobody: {
    label: 'Nothing needed',
    className: 'border-green-200 bg-green-100 text-green-800',
  },
}

const DECISION_TYPE: Record<
  OperatorDecisionType,
  { label: string; className: string }
> = {
  progress: { label: 'Progress', className: 'text-muted-foreground' },
  risk_decision: { label: 'Risk decision', className: 'text-amber-700' },
  none: { label: 'No decision', className: 'text-muted-foreground' },
}

// All five process-gate statuses get a distinct, honest treatment. None of these
// describe code correctness — only Pipewright's process gates.
const SAFETY_STATUS: Record<
  OperatorSafetyCheckStatus,
  { label: string; className: string }
> = {
  passed: { label: 'PASS', className: 'border-green-200 bg-green-100 text-green-800' },
  failed: { label: 'FAIL', className: 'border-red-200 bg-red-100 text-red-800' },
  weak: { label: 'WEAK', className: 'border-amber-200 bg-amber-100 text-amber-900' },
  not_evaluated: {
    label: 'N/E',
    className: 'border-slate-200 bg-slate-100 text-slate-600',
  },
  not_applicable: {
    label: 'N/A',
    className: 'border-muted bg-muted text-muted-foreground',
  },
}

function WaitingPill({ waitingOn }: { waitingOn: OperatorWaitingOn }) {
  const meta = WAITING_ON[waitingOn] ?? WAITING_ON.system
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2.5 py-0.5 font-mono text-[10px] font-medium uppercase tracking-wider ${meta.className}`}
    >
      {meta.label}
    </span>
  )
}

function DecisionTag({ decisionType }: { decisionType: OperatorDecisionType }) {
  const meta = DECISION_TYPE[decisionType] ?? DECISION_TYPE.none
  return (
    <span
      className={`font-mono text-[11px] font-medium uppercase tracking-wider ${meta.className}`}
    >
      [{meta.label}]
    </span>
  )
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <p className="mb-2 font-mono text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
      {children}
    </p>
  )
}

function SafetyChecks({ checks }: { checks: OperatorSafetyCheck[] }) {
  if (checks.length === 0) return null
  return (
    <div>
      <SectionLabel>Safety checks (process gates, not code correctness)</SectionLabel>
      <ul className="grid gap-1.5">
        {checks.map(check => {
          const meta = SAFETY_STATUS[check.status] ?? SAFETY_STATUS.not_evaluated
          return (
            <li key={check.id} className="flex items-start gap-2 text-[12.5px]">
              <span
                className={`mt-0.5 rounded border px-1.5 py-0.5 font-mono text-[10px] tracking-wide ${meta.className}`}
              >
                {meta.label}
              </span>
              <span className="leading-snug text-muted-foreground">
                <span className="font-medium text-foreground">{check.label}.</span>{' '}
                {check.detail}
              </span>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

// #35G: blocked_actions are always informational, never interactive. They are
// rendered as plain "Can't do yet: <label> — <reason>" explanations (no buttons,
// no disabled-primary styling) so the user understands why a step is unavailable.
function BlockedActions({ actions }: { actions: OperatorAction[] }) {
  if (actions.length === 0) return null
  return (
    <div>
      <SectionLabel>Not available yet</SectionLabel>
      <ul className="grid gap-2">
        {actions.map(action => (
          <li
            key={action.id}
            className="rounded border border-dashed border-muted-foreground/40 bg-muted/40 px-2.5 py-2 text-[12.5px] leading-snug text-muted-foreground"
          >
            <span className="font-medium text-foreground">
              Can&apos;t do yet: {action.label}
            </span>
            {action.blocked_reason && <span> — {action.blocked_reason}</span>}
          </li>
        ))}
      </ul>
    </div>
  )
}

function TrustFacts({ facts }: { facts: OperatorState['trust_facts'] }) {
  if (facts.length === 0) return null

  const list = (
    <dl className="grid gap-1.5">
      {facts.map(fact => (
        <div key={fact.id} className="grid grid-cols-[110px_1fr] gap-2.5 text-[12.5px]">
          <dt className="font-mono text-[11px] text-muted-foreground">
            {fact.label}
          </dt>
          <dd className="text-foreground">{fact.detail}</dd>
        </div>
      ))}
    </dl>
  )

  // Keep the panel compact: collapse longer trust-fact lists behind a details
  // toggle rather than letting them dominate the panel height.
  if (facts.length > 3) {
    return (
      <details>
        <summary className="cursor-pointer font-mono text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
          Trust facts ({facts.length})
        </summary>
        <div className="mt-2">{list}</div>
      </details>
    )
  }

  return (
    <div>
      <SectionLabel>Trust facts</SectionLabel>
      {list}
    </div>
  )
}

// Render an operator_state action as a non-interactive visual preview. The real
// controls live in the existing panels below; these never mutate anything. It is
// styled as a quiet, dashed chip — deliberately not a button — so it reads as a
// display-only mirror rather than a disabled/broken control.
function PreviewAction({
  action,
  className,
}: {
  action: OperatorAction
  className?: string
}) {
  return (
    <span
      title={action.intent}
      className={`inline-flex items-center gap-2 rounded-md border border-dashed border-muted-foreground/40 bg-muted/30 px-3 py-1.5 text-sm text-muted-foreground ${className ?? ''}`}
    >
      {action.label}
      <span className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground/70">
        preview
      </span>
    </span>
  )
}

export default function OperatorAttentionPanel({
  operatorState,
  resolvePrimaryAction,
  resolveCoEqualAction,
}: {
  operatorState?: OperatorState | null
  // #35F: optional resolver supplied by the page. Given the current
  // primary_action it returns the legacy mutation to run (and its pending
  // state), or null when the action is unmapped or its legacy control would be
  // unavailable. When null, the primary_action keeps its display-only preview.
  resolvePrimaryAction?: (
    action: OperatorAction,
  ) => { onClick: () => void; isPending: boolean } | null
  // #35G: optional resolver for neutral_actions / secondary_actions (co-equal on
  // risk decisions). Same contract as resolvePrimaryAction; null keeps the
  // action as a display-only preview. Co-equal styling is preserved regardless.
  resolveCoEqualAction?: (
    action: OperatorAction,
  ) => { onClick: () => void; isPending: boolean } | null
}) {
  // Render nothing when the backend did not attach operator_state.
  if (!operatorState) return null

  const {
    title,
    explanation,
    waiting_on,
    decision_type,
    primary_action,
    neutral_actions,
    secondary_actions,
    blocked_actions,
    safety_checks,
    trust_facts,
    out_of_app_instruction,
    unknown_state_warning,
  } = operatorState

  const isRisk = decision_type === 'risk_decision'
  const hasPreviewActions =
    Boolean(primary_action) ||
    neutral_actions.length > 0 ||
    secondary_actions.length > 0

  // #35F: a wired primary action is offered only for non-risk (PROGRESS) states
  // and only when the page resolver maps it to a legacy mutation. Risk decisions
  // never get a single recommended primary.
  const wiredPrimary =
    primary_action && !isRisk
      ? resolvePrimaryAction?.(primary_action) ?? null
      : null

  // #35G: resolve neutral/secondary (co-equal) actions. Unmapped ones resolve to
  // null and stay display-only previews. These keep equal visual weight whether
  // wired or not, so risk decisions never imply a recommended choice.
  const neutralResolved = neutral_actions.map(action => ({
    action,
    wired: resolveCoEqualAction?.(action) ?? null,
  }))
  const secondaryResolved = secondary_actions.map(action => ({
    action,
    wired: resolveCoEqualAction?.(action) ?? null,
  }))
  const hasWiredAction =
    Boolean(wiredPrimary) ||
    neutralResolved.some(item => item.wired) ||
    secondaryResolved.some(item => item.wired)
  // Co-equal options share one zone so risk choices read with equal weight.
  const coEqualResolved = [...neutralResolved, ...secondaryResolved]

  // Calm, intentional left accent that tracks who the run is waiting on, rather
  // than always signalling "attention". Purely visual.
  const accentBorder =
    waiting_on === 'nobody'
      ? 'border-l-emerald-400'
      : waiting_on === 'system'
        ? 'border-l-blue-400'
        : 'border-l-amber-400'

  return (
    <Card className={`mb-6 border-l-4 ${accentBorder}`}>
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2.5">
          <WaitingPill waitingOn={waiting_on} />
          <DecisionTag decisionType={decision_type} />
        </div>
        <CardTitle className="text-lg leading-snug tracking-tight">
          {title}
        </CardTitle>
        <p className="max-w-2xl text-sm leading-relaxed text-muted-foreground">
          {explanation}
        </p>
      </CardHeader>

      <CardContent className="grid gap-4">
        {unknown_state_warning && (
          <div className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
            <p className="font-mono text-[11px] font-semibold uppercase tracking-wider">
              Unknown state
            </p>
            <p className="mt-1">{unknown_state_warning}</p>
          </div>
        )}

        <SafetyChecks checks={safety_checks} />

        <BlockedActions actions={blocked_actions} />

        <TrustFacts facts={trust_facts} />

        {out_of_app_instruction && (
          <div>
            <SectionLabel>Do this outside Pipewright</SectionLabel>
            <pre className="overflow-x-auto whitespace-pre-wrap rounded bg-zinc-900 px-3 py-2 font-mono text-xs text-zinc-100">
              <span className="text-zinc-500">$ </span>
              {out_of_app_instruction}
            </pre>
          </div>
        )}

        {hasPreviewActions && (
          <>
            <Separator />
            <div className="grid gap-3">
              <SectionLabel>
                {isRisk ? 'Your decision' : 'Recommended next action'}
              </SectionLabel>
              {isRisk && (
                <p className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  <span className="rounded border border-amber-200 bg-amber-50 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-amber-800">
                    Equal choice
                  </span>
                  Co-equal options — Pipewright does not recommend one over the
                  other.
                </p>
              )}

              {/* Progress states surface one clear, prominent primary CTA; risk
                  decisions intentionally use co-equal options only and never a
                  recommended primary. Wired controls run the SAME mutation as
                  their twin below; unmapped ones stay display-only previews. */}
              {primary_action && !isRisk && (
                <div className="flex flex-wrap gap-2.5">
                  {wiredPrimary ? (
                    <Button
                      type="button"
                      variant="default"
                      onClick={wiredPrimary.onClick}
                      disabled={wiredPrimary.isPending}
                      title={primary_action.intent}
                    >
                      {wiredPrimary.isPending
                        ? 'Working…'
                        : primary_action.label}
                    </Button>
                  ) : (
                    <PreviewAction action={primary_action} />
                  )}
                </div>
              )}

              {coEqualResolved.length > 0 && (
                <div
                  className={
                    isRisk
                      ? 'grid gap-2.5 sm:grid-cols-2'
                      : 'flex flex-wrap gap-2.5'
                  }
                >
                  {coEqualResolved.map(({ action, wired }) =>
                    wired ? (
                      <Button
                        key={action.id}
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={wired.onClick}
                        disabled={wired.isPending}
                        title={action.intent}
                        className={isRisk ? 'w-full' : undefined}
                      >
                        {wired.isPending ? 'Working…' : action.label}
                      </Button>
                    ) : (
                      <PreviewAction
                        key={action.id}
                        action={action}
                        className={isRisk ? 'w-full justify-center' : undefined}
                      />
                    ),
                  )}
                </div>
              )}

              {hasWiredAction ? (
                <p className="text-xs text-muted-foreground">
                  Linked controls run the same step as their match below — use
                  either. Anything marked “preview” is display-only and lives in
                  a control further down.
                </p>
              ) : (
                <p className="text-xs text-muted-foreground">
                  Display-only previews — use the matching controls below to act.
                </p>
              )}
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}
