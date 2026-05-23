import { Routes, Route, Navigate } from 'react-router-dom'
import Layout from './components/Layout'
import ProjectsPage from './pages/ProjectsPage'
import NewProjectPage from './pages/NewProjectPage'
import ProjectDashboard from './pages/ProjectDashboard'
import RunDetailPage from './pages/RunDetailPage'
import ApprovalQueuePage from './pages/ApprovalQueuePage'

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Layout />}>
        <Route index element={<Navigate to="/projects" replace />} />
        <Route path="projects" element={<ProjectsPage />} />
        <Route path="projects/new" element={<NewProjectPage />} />
        <Route path="projects/:projectId" element={<ProjectDashboard />} />
        <Route path="runs/:runId" element={<RunDetailPage />} />
        <Route path="approval" element={<ApprovalQueuePage />} />
        <Route path="memory" element={
          <div style={{ padding: 28, color: '#6B7280', fontFamily: 'IBM Plex Mono' }}>
            Memory manager — coming soon
          </div>
        } />
      </Route>
    </Routes>
  )
}
