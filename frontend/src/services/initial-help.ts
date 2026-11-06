import type { HelpResponse } from './api'

export function getInitialMessage(path: string): HelpResponse | null {
  try {
    const message = JSON.parse(document.getElementById('initial-message')?.textContent ?? 'null')
    return message?.path === path && typeof message?.title === 'string' && typeof message?.content === 'string' ? message : null
  } catch {
    return null
  }
}

export function getInitialHelp(): HelpResponse | null {
  return getInitialMessage('/help')
}

export function isInitialNotFound(path: string): boolean {
  try {
    const message = JSON.parse(document.getElementById('initial-message')?.textContent ?? 'null')
    return message?.notFoundPath === path
  } catch {
    return false
  }
}
