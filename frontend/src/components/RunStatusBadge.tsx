import { Badge } from '@/components/ui/badge'

interface Props {
  status: string
}

const statusConfig: Record<string, {
  label: string
  className: string
}> = {
  running:  { label: 'Running',  className: 'bg-blue-500 text-white' },
  paused:   { label: 'Waiting',  className: 'bg-yellow-500 text-white' },
  complete: { label: 'Complete', className: 'bg-green-500 text-white' },
  failed:   { label: 'Failed',   className: 'bg-red-500 text-white' },
  rejected: { label: 'Rejected', className: 'bg-gray-500 text-white' },
}

export default function RunStatusBadge({ status }: Props) {
  const config = statusConfig[status] ?? {
    label: status,
    className: 'bg-gray-400 text-white'
  }
  return (
    <Badge className={config.className}>
      {config.label}
    </Badge>
  )
}
