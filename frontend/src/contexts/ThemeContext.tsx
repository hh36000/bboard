import React, { useEffect, useState } from 'react'
import { ThemeContext } from './theme-context'
import { readTheme, saveTheme } from '../services/theme-storage'

interface ThemeProviderProps {
  children: React.ReactNode
}

export function ThemeProvider({ children }: ThemeProviderProps) {
  const [theme, setThemeState] = useState(readTheme)

  useEffect(() => {
    const root = window.document.documentElement
    root.classList.remove('light', 'dark')
    root.classList.add(theme)
    
    saveTheme(theme)
  }, [theme])

  const toggleTheme = () => {
    setThemeState(prev => prev === 'light' ? 'dark' : 'light')
  }

  return (
    <ThemeContext.Provider value={{ theme, toggleTheme }}>
      {children}
    </ThemeContext.Provider>
  )
}
