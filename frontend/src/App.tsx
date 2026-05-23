import { Routes, Route, Navigate } from 'react-router-dom'
import Layout from './components/Layout'
import ProjectsPage from './pages/ProjectsPage'
import NewProjectPage from './pages/NewProjectPage'
import ProjectDashboard from './pages/ProjectDashboard'
import RunDetailPage from './pages/RunDetailPage'

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Layout />}>
        <Route index element={<Navigate to="/projects" replace />} />
        <Route path="projects" element={<ProjectsPage />} />
        <Route path="projects/new" element={<NewProjectPage />} />
        <Route path="projects/:projectId" element={<ProjectDashboard />} />
        <Route path="runs/:runId" element={<RunDetailPage />} />
      </Route>
    </Routes>
  )
}
