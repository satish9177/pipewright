import { Outlet, NavLink } from 'react-router-dom'
import { Separator } from '@/components/ui/separator'

export default function Layout() {
  return (
    <div className="flex h-screen bg-background">
      <aside className="w-56 border-r flex flex-col p-4 gap-2">
        <div className="mb-4">
          <h1 className="text-lg font-bold tracking-tight">
            Pipewright
          </h1>
          <p className="text-xs text-muted-foreground">
            AI Engineering Pipeline
          </p>
        </div>
        <Separator className="mb-2" />
        <nav className="flex flex-col gap-1">
          <NavLink
            to="/projects"
            end
            className={({ isActive }) =>
              `text-sm px-3 py-2 rounded-md transition-colors ${
                isActive
                  ? 'bg-primary text-primary-foreground'
                  : 'hover:bg-muted text-muted-foreground'
              }`
            }
          >
            Projects
          </NavLink>
          <NavLink
            to="/projects/new"
            className={({ isActive }) =>
              `text-sm px-3 py-2 rounded-md transition-colors ${
                isActive
                  ? 'bg-primary text-primary-foreground'
                  : 'hover:bg-muted text-muted-foreground'
              }`
            }
          >
            New Project
          </NavLink>
        </nav>
      </aside>
      <main className="flex-1 overflow-auto p-6">
        <Outlet />
      </main>
    </div>
  )
}
