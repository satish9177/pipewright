import { useEffect } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { projectsApi } from '@/api/client'
import ProjectMemoryPanel from '@/components/ProjectMemoryPanel'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Label } from '@/components/ui/label'

export default function MemoryPage() {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const selectedProjectId = searchParams.get('projectId') ?? ''

  const { data: projects, isLoading } = useQuery({
    queryKey: ['projects'],
    queryFn: projectsApi.list,
  })

  useEffect(() => {
    if (!selectedProjectId && projects && projects.length > 0) {
      setSearchParams({ projectId: projects[0].id }, { replace: true })
    }
  }, [projects, selectedProjectId, setSearchParams])

  const selectedProject = projects?.find(project => project.id === selectedProjectId)

  return (
    <div className="mx-auto max-w-5xl px-6 py-6">
      <div className="mb-6 flex flex-col justify-between gap-4 sm:flex-row sm:items-start">
        <div>
          <h2 className="text-2xl font-bold">Project Memory</h2>
          <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
            Manage project-scoped advisory facts and bootstrap suggestions.
            Memory is never global; source code and explicit user instructions
            always win on conflict.
          </p>
        </div>
        {selectedProject && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => navigate(`/projects/${selectedProject.id}`)}
          >
            Project Dashboard
          </Button>
        )}
      </div>

      <Card className="mb-6">
        <CardHeader>
          <CardTitle className="text-base">Project</CardTitle>
          <CardDescription>
            Select which project memory space to manage.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {isLoading && (
            <p className="text-sm text-muted-foreground">Loading projects...</p>
          )}
          {!isLoading && (!projects || projects.length === 0) && (
            <div className="rounded-lg border border-dashed p-4">
              <p className="text-sm font-medium">No projects yet</p>
              <p className="mt-1 text-sm text-muted-foreground">
                Create a project before adding memory facts.
              </p>
            </div>
          )}
          {!isLoading && projects && projects.length > 0 && (
            <div className="grid gap-2">
              <Label htmlFor="memory-project-select">Project</Label>
              <select
                id="memory-project-select"
                value={selectedProjectId}
                onChange={event =>
                  setSearchParams({ projectId: event.target.value })
                }
                className="h-8 w-full rounded-lg border border-input bg-background px-2.5 py-1 text-sm outline-none transition-colors focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
              >
                {projects.map(project => (
                  <option key={project.id} value={project.id}>
                    {project.name}
                  </option>
                ))}
              </select>
              {selectedProject && (
                <p className="text-xs text-muted-foreground font-mono">
                  {selectedProject.repo_path}
                </p>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      {selectedProject ? (
        <ProjectMemoryPanel projectId={selectedProject.id} />
      ) : (
        !isLoading && (
          <Card className="border-dashed">
            <CardContent className="py-6">
              <p className="text-sm text-muted-foreground">
                Select a project to manage memory.
              </p>
            </CardContent>
          </Card>
        )
      )}
    </div>
  )
}
