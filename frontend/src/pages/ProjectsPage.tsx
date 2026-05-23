import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { projectsApi, Project } from '@/api/client'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'

export default function ProjectsPage() {
  const navigate = useNavigate()
  const { data: projects, isLoading, error } = useQuery({
    queryKey: ['projects'],
    queryFn: projectsApi.list,
  })

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <p className="text-muted-foreground">Loading projects...</p>
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex items-center justify-center h-64">
        <p className="text-red-500">
          Failed to load projects. Is the backend running on port 8001?
        </p>
      </div>
    )
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h2 className="text-2xl font-bold">Projects</h2>
          <p className="text-muted-foreground text-sm mt-1">
            Select a project to run the pipeline
          </p>
        </div>
        <Button onClick={() => navigate('/projects/new')}>
          New Project
        </Button>
      </div>

      {projects?.length === 0 && (
        <div className="text-center py-16 border rounded-lg">
          <p className="text-muted-foreground mb-4">
            No projects yet. Create your first one.
          </p>
          <Button onClick={() => navigate('/projects/new')}>
            Create Project
          </Button>
        </div>
      )}

      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        {projects?.map((project: Project) => (
          <Card
            key={project.id}
            className="cursor-pointer hover:border-primary transition-colors"
            onClick={() => navigate(`/projects/${project.id}`)}
          >
            <CardHeader>
              <div className="flex items-start justify-between">
                <CardTitle className="text-base">{project.name}</CardTitle>
                {project.github_repo && (
                  <Badge className="text-xs bg-blue-100 text-blue-800">
                    GitHub
                  </Badge>
                )}
              </div>
              <CardDescription className="text-xs font-mono truncate">
                {project.repo_path}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <p className="text-xs text-muted-foreground">
                Branch: {project.branch}
              </p>
              <p className="text-xs text-muted-foreground mt-1 font-mono truncate">
                {project.test_command}
              </p>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  )
}
