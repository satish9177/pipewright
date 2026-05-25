import { Outlet, NavLink } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { gatesApi } from '@/api/client'

export default function Layout() {
  const { data: gates } = useQuery({
    queryKey: ['gates'],
    queryFn: gatesApi.list,
    refetchInterval: 5000,
  })

  const pendingCount = gates?.filter(
    (g: any) => g.status === 'pending'
  ).length ?? 0

  return (
    <div style={{
      display: 'flex',
      height: '100vh',
      backgroundColor: '#F6F4EE',
      color: '#0E1116',
    }}>
      <aside style={{
        width: 220,
        borderRight: '1px solid #D6D9DE',
        display: 'flex',
        flexDirection: 'column',
        padding: '20px 0',
        flexShrink: 0,
      }}>
        <div style={{ padding: '0 16px 20px', borderBottom: '1px solid #D6D9DE' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <img
              src="/pipewright-mark.svg"
              alt="Pipewright"
              style={{ height: 24, width: 'auto' }}
              onError={(e) => { e.currentTarget.style.display = 'none' }}
            />
            <span style={{
              fontFamily: 'IBM Plex Sans, sans-serif',
              fontWeight: 600,
              fontSize: 16,
              letterSpacing: '-0.01em',
              color: '#0E1116',
            }}>
              pipewright
            </span>
          </div>
        </div>

        <nav style={{ padding: '12px 0', flex: 1 }}>
          <div style={{
            padding: '4px 16px 8px',
            fontSize: 10,
            fontFamily: 'IBM Plex Mono, monospace',
            letterSpacing: '0.08em',
            color: '#6B7280',
            textTransform: 'uppercase',
          }}>
            WORKSPACE
          </div>

          <SidebarLink to="/projects" label="Projects" />

          <SidebarLink
            to="/approval"
            label="Approval queue"
            badge={pendingCount > 0 ? pendingCount : undefined}
          />

          <SidebarLink to="/memory" label="Memory" />
        </nav>

        <div style={{
          padding: '12px 16px',
          borderTop: '1px solid #D6D9DE',
          fontSize: 11,
          fontFamily: 'IBM Plex Mono, monospace',
          color: '#6B7280',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <span style={{
              width: 6,
              height: 6,
              borderRadius: '50%',
              backgroundColor: '#4CAF50',
              display: 'inline-block',
            }} />
            localhost:8001
          </div>
        </div>
      </aside>

      <main style={{
        flex: 1,
        overflow: 'auto',
        backgroundColor: '#F6F4EE',
      }}>
        <Outlet />
      </main>
    </div>
  )
}

function SidebarLink({
  to,
  label,
  badge,
}: {
  to: string
  label: string
  badge?: number
}) {
  return (
    <NavLink
      to={to}
      style={({ isActive }) => ({
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '6px 16px',
        fontSize: 13,
        fontFamily: 'IBM Plex Sans, sans-serif',
        color: isActive ? '#0E1116' : '#6B7280',
        backgroundColor: isActive ? '#EFEBE0' : 'transparent',
        textDecoration: 'none',
        borderLeft: isActive ? '2px solid #B7531C' : '2px solid transparent',
        transition: 'all 120ms ease-out',
      })}
    >
      <span>{label}</span>
      {badge !== undefined && (
        <span style={{
          backgroundColor: '#B7531C',
          color: 'white',
          fontSize: 10,
          fontFamily: 'IBM Plex Mono, monospace',
          padding: '1px 6px',
          borderRadius: 999,
          minWidth: 18,
          textAlign: 'center',
        }}>
          {badge}
        </span>
      )}
    </NavLink>
  )
}
