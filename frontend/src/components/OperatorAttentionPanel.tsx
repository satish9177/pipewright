import type {
  OperatorAction,
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
import {
  coEqualActionHandlerKey,
  primaryActionHandlerKey,
  type CoEqualHandlerKey,
  type LegacyPrimaryHandlerKey,
} from '@/lib/operatorPrimaryAction'

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

// #38A: the decision_type still drives layout (one primary CTA for `progress`
// vs co-equal options for `risk_decision`), but the raw `[Progress]` /
// `[Risk decision]` enum tag was removed from the headline — the Claude Design
// audit flagged it as backend-vocabulary leak. Risk decisions are now conveyed
// by the co-equal "Equal choice" framing below, not an enum badge.

// #37C: display-only "effects ledger" — a small, conservative summary of what a
// WIRED action can affect before the user clicks. It is keyed off the SAME
// handler keys that make an action clickable (operatorPrimaryAction.ts), so a
// ledger can only ever appear under an already-wired primary/co-equal action;
// unmapped preview actions never get one. It changes no behavior, enables or
// disables nothing, and never understates an effect — anything mode-dependent or
// downstream is worded "may …" rather than claimed false. "Merges: Never" states
// the standing guarantee that Pipewright never merges.
type EffectTone = 'no' | 'maybe' | 'yes' | 'grant' | 'never'

interface ActionEffect {
  label: string
  value: string
  tone: EffectTone
}

const EFFECT_TONE_CLASS: Record<EffectTone, string> = {
  no: 'border-slate-200 bg-slate-50 text-slate-600',
  never: 'border-slate-200 bg-slate-50 text-slate-600',
  maybe: 'border-amber-200 bg-amber-50 text-amber-800',
  yes: 'border-amber-200 bg-amber-50 text-amber-800',
  grant: 'border-blue-200 bg-blue-50 text-blue-800',
}

const ACTION_EFFECTS: Record<
  LegacyPrimaryHandlerKey | CoEqualHandlerKey,
  ActionEffect[]
> = {
  approve_plan: [
    { label: 'Writes files', value: 'No', tone: 'no' },
    { label: 'Commits', value: 'No', tone: 'no' },
    { label: 'Pushes / PR', value: 'No', tone: 'no' },
    { label: 'Grants', value: 'Plan approval', tone: 'grant' },
  ],
  execute_chunks: [
    { label: 'Writes files', value: 'May write during execution', tone: 'maybe' },
    { label: 'Commits', value: 'Not without your approval', tone: 'no' },
    { label: 'Pushes / PR', value: 'No', tone: 'no' },
    { label: 'Merges', value: 'Never', tone: 'never' },
  ],
  approve_final: [
    { label: 'Grants', value: 'Finalization', tone: 'grant' },
    {
      label: 'Commits',
      value: 'May commit / finish (depends on mode)',
      tone: 'maybe',
    },
    { label: 'Pushes', value: 'Not by itself', tone: 'no' },
    { label: 'Merges', value: 'Never', tone: 'never' },
  ],
  create_pr: [
    { label: 'Pushes branch', value: 'Yes (supported PR modes)', tone: 'yes' },
    { label: 'Creates PR', value: 'Yes', tone: 'yes' },
    { label: 'Merges', value: 'Never', tone: 'never' },
  ],
  approve_memory_conflict: [
    {
      label: 'Grants',
      value: 'One-time override · continues run',
      tone: 'grant',
    },
    {
      label: 'Writes files',
      value: 'May write (continues execution)',
      tone: 'maybe',
    },
    { label: 'Pushes / PR', value: 'No', tone: 'no' },
    { label: 'Merges', value: 'Never', tone: 'never' },
  ],
  reject_memory_conflict: [
    { label: 'Outcome', value: 'Rejects · stops the run', tone: 'no' },
    { label: 'Writes files', value: 'No', tone: 'no' },
    { label: 'Pushes / PR', value: 'No', tone: 'no' },
    { label: 'Merges', value: 'Never', tone: 'never' },
  ],
}

// Display-only ledger row of small mono pills. Renders only when handed an
// effects list (i.e. for a wired action); callers never pass effects for preview
// or unmapped actions.
function ActionEffectsLedger({ effects }: { effects: ActionEffect[] }) {
  return (
    <div
      className="flex flex-wrap items-center gap-1.5"
      aria-label="What this action can affect"
    >
      {effects.map(effect => (
        <span
          key={effect.label}
          className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 font-mono text-[10px] ${EFFECT_TONE_CLASS[effect.tone]}`}
        >
          {effect.label}: <span className="font-semibold">{effect.value}</span>
        </span>
      ))}
    </div>
  )
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

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <p className="mb-2 font-mono text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
      {children}
    </p>
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
    trust_facts,
    out_of_app_instruction,
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

  // #37C: effects ledger for the primary action, shown only when it is actually
  // wired. A wired primary always has a mapped handler key, so this is non-null
  // exactly when a real button renders — never for a display-only preview.
  const primaryKey =
    wiredPrimary && primary_action
      ? primaryActionHandlerKey(primary_action.id)
      : null
  const primaryEffects = primaryKey ? ACTION_EFFECTS[primaryKey] : null

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

  // #38A: cockpit "mood bar" — a calm full-width accent strip across the top of
  // the spine that tracks who the run is waiting on, rather than always
  // signalling "attention". Replaces the old left-border accent so the panel
  // reads as one cohesive instrument. Purely visual.
  const moodBar =
    waiting_on === 'nobody'
      ? 'bg-emerald-400'
      : waiting_on === 'system'
        ? 'bg-blue-400'
        : 'bg-amber-400'

  return (
    // #38A: elevated (white) surface + top mood bar make the guidance area read
    // as the page's single cockpit spine, lifted off the warm-paper background.
    // The bar uses -mt-4 to sit flush against the card's top edge (Card has its
    // own py-4); Card already clips with overflow-hidden so it follows the radius.
    <Card className="mb-6 bg-[var(--pw-bg-elev)]">
      <div
        className={`-mt-4 h-1.5 w-full ${moodBar}`}
        aria-hidden="true"
      />
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2.5">
          <WaitingPill waitingOn={waiting_on} />
        </div>
        <CardTitle className="text-lg leading-snug tracking-tight">
          {title}
        </CardTitle>
        <p className="max-w-2xl text-sm leading-relaxed text-muted-foreground">
          {explanation}
        </p>
      </CardHeader>

      <CardContent className="grid gap-4">
        {/* #37B: the unknown_state_warning banner and the operator_state
            safety_checks list moved up into the single Safety overview surface
            (RunSafetyStrip) to remove duplicate safety summaries. Both remain
            visible there with equal/greater prominence. This panel keeps the
            decision, blocked actions, trust facts, out-of-app step, and actions. */}
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
                <div className="grid gap-1.5">
                  {wiredPrimary ? (
                    <>
                      <div className="flex flex-wrap gap-2.5">
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
                      </div>
                      {primaryEffects && (
                        <ActionEffectsLedger effects={primaryEffects} />
                      )}
                    </>
                  ) : (
                    <div className="flex flex-wrap gap-2.5">
                      <PreviewAction action={primary_action} />
                    </div>
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
                  {coEqualResolved.map(({ action, wired }) => {
                    if (!wired) {
                      return (
                        <PreviewAction
                          key={action.id}
                          action={action}
                          className={isRisk ? 'w-full justify-center' : undefined}
                        />
                      )
                    }
                    // Wired co-equal action: render its effects ledger under the
                    // button. Both choices use identical ledger styling so the
                    // risk decision stays co-equal — no choice looks recommended.
                    const coEqualKey = coEqualActionHandlerKey(action.id)
                    const coEqualEffects = coEqualKey
                      ? ACTION_EFFECTS[coEqualKey]
                      : null
                    return (
                      <div
                        key={action.id}
                        className={isRisk ? 'grid w-full gap-1.5' : 'grid gap-1.5'}
                      >
                        <Button
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
                        {coEqualEffects && (
                          <ActionEffectsLedger effects={coEqualEffects} />
                        )}
                      </div>
                    )
                  })}
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
