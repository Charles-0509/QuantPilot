import { lazy, Suspense } from 'react'
import { Route, Routes } from 'react-router-dom'
import Shell from './components/Shell'
import AuthGate from './components/AuthGate'
import { Loading } from './components/UI'

const Dashboard = lazy(() => import('./pages/Dashboard'))
const Market = lazy(() => import('./pages/Market'))
const Strategies = lazy(() => import('./pages/Strategies'))
const StrategyEditor = lazy(() => import('./pages/StrategyEditor'))
const Backtests = lazy(() => import('./pages/Backtests'))
const Trading = lazy(() => import('./pages/Trading'))
const Risk = lazy(() => import('./pages/Risk'))
const SettingsPage = lazy(() => import('./pages/Settings'))
const Users = lazy(() => import('./pages/Users'))

export default function App() {
  return (
    <AuthGate>
      <Suspense fallback={<Loading label="正在加载控制模块" />}>
        <Routes>
          <Route element={<Shell />}>
            <Route index element={<Dashboard />} />
            <Route path="market" element={<Market />} />
            <Route path="strategies" element={<Strategies />} />
            <Route path="strategies/new" element={<StrategyEditor />} />
            <Route path="strategies/:id" element={<StrategyEditor />} />
            <Route path="backtests" element={<Backtests />} />
            <Route path="trading" element={<Trading />} />
            <Route path="risk" element={<Risk />} />
            <Route path="settings" element={<SettingsPage />} />
            <Route path="users" element={<Users />} />
          </Route>
        </Routes>
      </Suspense>
    </AuthGate>
  )
}
