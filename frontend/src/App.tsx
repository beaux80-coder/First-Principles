import { Routes, Route } from 'react-router-dom'
import BenchmarkPage from './pages/BenchmarkPage'
import BenchmarkResultPage from './pages/BenchmarkResultPage'
import DashboardPage from './pages/DashboardPage'
import BrokerPage from './pages/BrokerPage'
import LoginPage from './pages/LoginPage'
import Layout from './components/Layout'

export default function App() {
  return (
    <Routes>
      {/* Public routes — zero login required */}
      <Route element={<Layout />}>
        <Route path="/" element={<BenchmarkPage />} />
        <Route path="/benchmark/:queryId" element={<BenchmarkResultPage />} />
        <Route path="/broker" element={<BrokerPage />} />

        {/* Authenticated routes */}
        <Route path="/login" element={<LoginPage />} />
        <Route path="/dashboard" element={<DashboardPage />} />
      </Route>
    </Routes>
  )
}
