import type { Theme } from '../contexts/theme-context'

export function readTheme(): Theme {
  try {
    return localStorage.getItem('theme') === 'light' ? 'light' : 'dark'
  } catch {
    return 'dark'
  }
}

export function saveTheme(theme: Theme) {
  try {
    localStorage.setItem('theme', theme)
  } catch { /* The current theme still works when storage is unavailable. */ }
}
