export interface MemoryStatusDisplay {
  label: string
  tooltip: string
  className: string
}

const memoryStatusDisplays: Record<string, MemoryStatusDisplay> = {
  active: {
    label: 'In use',
    tooltip: 'Injected into AI prompts for this project.',
    className: 'border-green-200 bg-green-100 text-green-700',
  },
  stale: {
    label: 'Possibly outdated',
    tooltip:
      'Flagged as possibly outdated. Not injected. Kept for review; nothing was deleted.',
    className: 'border-yellow-200 bg-yellow-100 text-yellow-800',
  },
  archived: {
    label: 'Retired',
    tooltip: 'Manually retired and no longer injected. Kept for the record.',
    className: 'border-slate-200 bg-slate-100 text-slate-700',
  },
  historical: {
    label: 'Replaced',
    tooltip:
      'Replaced by another fact you approved. Not injected, but preserved so you can see what changed.',
    className: 'border-blue-200 bg-blue-100 text-blue-700',
  },
}

export function getMemoryStatusDisplay(status?: string | null): MemoryStatusDisplay {
  if (status && memoryStatusDisplays[status]) {
    return memoryStatusDisplays[status]
  }

  return {
    label: status || 'Unknown',
    tooltip: 'Unknown memory status.',
    className: 'border-border bg-muted text-muted-foreground',
  }
}
