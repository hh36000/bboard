import { API_BASE_URL } from './api'
import { DocumentSync } from './document-sync'
import { getInitialMessage } from './initial-help'

export function createDocumentSync(boardId: string) {
  const path = `/ws/boards/${boardId}`
  const url = new URL(`${API_BASE_URL.replace(/\/$/, '')}${path}`, window.location.href)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  let storage: Storage | undefined
  try { storage = window.sessionStorage } catch { /* Session storage is optional. */ }
  return new DocumentSync(url.toString(), {
    storage,
    draftKey: `bboard.board.${boardId}.v1`,
    initialContent: getInitialMessage(`/${boardId}`)?.content,
  })
}
