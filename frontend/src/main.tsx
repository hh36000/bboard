import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { flushSync } from 'react-dom'
import './index.css'
import App from '@/App.tsx'
import { BrowserRouter } from 'react-router-dom'
import { readTheme } from './services/theme-storage'

// Match the server's first paint before React mounts (also covers the static entry).
const theme = readTheme()
document.documentElement.classList.remove('light', 'dark')
document.documentElement.classList.add(theme)

// Keep the readable page styled until React has actually replaced its markup.
const root = createRoot(document.getElementById('root')!)
flushSync(() => root.render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
))
document.getElementById('page-fallback-style')?.remove()
