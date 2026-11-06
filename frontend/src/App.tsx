import { Navigate, Routes, Route } from 'react-router-dom'
import PageMetadata from './components/PageMetadata'
import Home from './pages/Home.tsx'
import Help from './pages/Help.tsx'
import NotFound from './pages/NotFound.tsx'
import Recent from './pages/Recent.tsx'
import { ThemeProvider } from './contexts/ThemeContext'

function App() {
  return (
    <ThemeProvider>
      <PageMetadata />
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/api" element={<Navigate to="/" replace />} />
        <Route path="/help/*" element={<Help />} />
        <Route path="/recent" element={<Recent />} />
        <Route path="/:boardKey/*" element={<Home />} />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </ThemeProvider>
  )
}

export default App
