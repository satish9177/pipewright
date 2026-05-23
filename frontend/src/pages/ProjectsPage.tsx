import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { projectsApi, Project } from '@/api/client'

export default function ProjectsPage() {
  const navigate = useNavigate()
  const { data: projects, isLoading, error } = useQuery({
    queryKey: ['projects'],
    queryFn: projectsApi.list,
  })

  return (
    <div style={{ padding: '24px 28px', maxWidth: 1200, margin: '0 auto' }}>
      <div style={{
        display: 'flex',
        alignItems: 'flex-end',
        justifyContent: 'space-between',
        marginBottom: 24,
      }}>
        <div>
          <div style={{
            fontSize: 10,
            fontFamily: 'IBM Plex Mono, monospace',
            letterSpacing: '0.08em',
            color: '#B7531C',
            textTransform: 'uppercase',
            marginBottom: 4,
          }}>
            WORKSPACE
          </div>
          <h1 style={{
            fontFamily: 'IBM Plex Sans, sans-serif',
            fontSize: 28,
            fontWeight: 600,
            letterSpacing: '-0.01em',
            margin: 0,
            color: '#0E1116',
          }}>
            Projects
          </h1>
        </div>
        <button
          onClick={() => navigate('/projects/new')}
          style={{
            backgroundColor: '#B7531C',
            color: 'white',
            border: 'none',
            padding: '8px 16px',
            borderRadius: 2,
            fontSize: 13,
            fontFamily: 'IBM Plex Sans, sans-serif',
            fontWeight: 500,
            cursor: 'pointer',
          }}
        >
          New project
        </button>
      </div>

      {isLoading && (
        <p style={{ color: '#6B7280', fontSize: 13 }}>
          Loading projects...
        </p>
      )}

      {error && (
        <p style={{ color: '#991B1B', fontSize: 13 }}>
          Failed to load. Is backend running on port 8001?
        </p>
      )}

      {projects?.length === 0 && (
        <div style={{
          border: '1px dashed #D6D9DE',
          borderRadius: 4,
          padding: '48px 24px',
          textAlign: 'center',
        }}>
          <p style={{ color: '#6B7280', fontSize: 13, marginBottom: 16 }}>
            No projects yet. Start one with New project.
          </p>
          <button
            onClick={() => navigate('/projects/new')}
            style={{
              backgroundColor: '#B7531C',
              color: 'white',
              border: 'none',
              padding: '8px 16px',
              borderRadius: 2,
              fontSize: 13,
              fontFamily: 'IBM Plex Sans, sans-serif',
              cursor: 'pointer',
            }}
          >
            New project
          </button>
        </div>
      )}

      {projects && projects.length > 0 && (
        <div style={{
          border: '1px solid #D6D9DE',
          borderRadius: 4,
          overflow: 'hidden',
        }}>
          <table style={{
            borderCollapse: 'collapse',
            width: '100%',
          }}>
            <thead>
              <tr style={{ backgroundColor: '#EFEBE0' }}>
                {['Name', 'Repo', 'Test command', 'Branch', 'GitHub'].map(h => (
                  <th key={h} style={{
                    padding: '8px 14px',
                    textAlign: 'left',
                    fontSize: 10,
                    fontFamily: 'IBM Plex Mono, monospace',
                    letterSpacing: '0.06em',
                    color: '#6B7280',
                    textTransform: 'uppercase',
                    fontWeight: 500,
                    borderBottom: '1px solid #D6D9DE',
                  }}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {projects.map((project: Project, i: number) => (
                <tr
                  key={project.id}
                  onClick={() => navigate(`/projects/${project.id}`)}
                  style={{
                    cursor: 'pointer',
                    backgroundColor: i % 2 === 0 ? '#F6F4EE' : '#FAF8F4',
                    borderBottom: i < projects.length - 1
                      ? '1px solid #D6D9DE' : 'none',
                    transition: 'background-color 120ms',
                  }}
                  onMouseEnter={e => {
                    (e.currentTarget as HTMLTableRowElement)
                      .style.backgroundColor = '#EFEBE0'
                  }}
                  onMouseLeave={e => {
                    (e.currentTarget as HTMLTableRowElement)
                      .style.backgroundColor = i % 2 === 0
                        ? '#F6F4EE' : '#FAF8F4'
                  }}
                >
                  <td style={{
                    padding: '10px 14px',
                    fontSize: 13,
                    fontWeight: 500,
                    color: '#0E1116',
                  }}>
                    {project.name}
                  </td>
                  <td style={{
                    padding: '10px 14px',
                    fontSize: 11,
                    fontFamily: 'IBM Plex Mono, monospace',
                    color: '#6B7280',
                    maxWidth: 200,
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}>
                    {project.repo_path.split(/[\\/]/).pop()}
                  </td>
                  <td style={{
                    padding: '10px 14px',
                    fontSize: 11,
                    fontFamily: 'IBM Plex Mono, monospace',
                    color: '#6B7280',
                  }}>
                    {project.test_command}
                  </td>
                  <td style={{
                    padding: '10px 14px',
                    fontSize: 11,
                    fontFamily: 'IBM Plex Mono, monospace',
                    color: '#6B7280',
                  }}>
                    {project.branch}
                  </td>
                  <td style={{ padding: '10px 14px' }}>
                    {project.github_repo ? (
                      <span style={{
                        fontSize: 10,
                        fontFamily: 'IBM Plex Mono, monospace',
                        backgroundColor: '#D1FAE5',
                        color: '#065F46',
                        padding: '2px 8px',
                        borderRadius: 999,
                      }}>
                        [CONNECTED]
                      </span>
                    ) : (
                      <span style={{
                        fontSize: 10,
                        fontFamily: 'IBM Plex Mono, monospace',
                        color: '#9CA3AF',
                      }}>
                        -
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div style={{
        marginTop: 12,
        fontSize: 10,
        fontFamily: 'IBM Plex Mono, monospace',
        color: '#9CA3AF',
      }}>
        GET /projects · {projects?.length ?? 0} result{projects?.length === 1 ? '' : 's'}
      </div>
    </div>
  )
}
