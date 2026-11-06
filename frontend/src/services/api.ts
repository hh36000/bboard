export const API_BASE_URL = import.meta.env?.VITE_API_URL || 'http://localhost:9900'

export interface HelpResponse {
  title: string
  content: string
  revision?: number
}

export async function getHelp(signal: AbortSignal): Promise<HelpResponse> {
  const response = await fetch(`${API_BASE_URL}/help`, { signal, cache: 'no-store', headers: { Accept: 'application/json' } })
  if (!response.ok) throw new Error('Could not load the instructions. Please try again.')
  return response.json()
}

export async function getHome(signal: AbortSignal): Promise<HelpResponse> {
  const response = await fetch(`${API_BASE_URL}/`, { signal, cache: 'no-store', headers: { Accept: 'application/json' } })
  if (!response.ok) throw new Error('Could not load the welcome message. Please try again.')
  return response.json()
}

export async function createBoard(idempotencyKey: string = crypto.randomUUID()): Promise<string> {
  const response = await fetch(`${API_BASE_URL}/write`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey },
    body: JSON.stringify({ content: '' }),
    signal: AbortSignal.timeout(10000),
  })
  if (!response.ok) {
    throw new Error(response.status === 429 ? 'Too many requests. Wait a moment and try again.' : 'Could not create the board. Try again.')
  }
  const board = await response.json()
  if (typeof board.key !== 'string' || !/^[0-9a-f]{64}$/.test(board.key)) {
    throw new Error('Could not create the board. Try again.')
  }
  return board.key
}

export interface BoardSnapshot { key: string; content: string; revision: number }
export interface BoardEvent {
  revision: number
  recorded_at: string
  kind: 'baseline' | 'created' | 'append' | 'delete' | 'edit'
  content: string
  change: { offset: number; removed: string; inserted: string } | null
}
export interface BoardEvents {
  key: string
  first_revision: number
  through_revision: number
  events: BoardEvent[]
  next_after_revision: number | null
}

async function boardResponse(response: Response): Promise<BoardSnapshot> {
  if (!response.ok) {
    const wait = Math.max(1, Math.min(60, Number(response.headers.get('Retry-After')) || 5))
    throw new Error(response.status === 404 ? 'This board does not exist.'
      : response.status === 429 ? `Request limit reached. Retry in ${wait} seconds.` : 'Board unavailable. Retry shortly.')
  }
  const value = await response.json()
  if (typeof value.content !== 'string' || !Number.isSafeInteger(value.revision) || value.revision < 0) {
    throw new Error('Could not confirm the server response.')
  }
  return value
}

export async function readBoard(key: string, signal: AbortSignal): Promise<BoardSnapshot> {
  return boardResponse(await fetch(`${API_BASE_URL}/${key}?format=json`, { signal, cache: 'no-store' }))
}

export async function readBoardRevision(key: string, revision: number, signal: AbortSignal): Promise<BoardEvent> {
  const response = await fetch(`${API_BASE_URL}/${key}/revisions/${revision}`, { signal, cache: 'no-store' })
  if (response.status === 404) throw new Error('This revision was not recorded, or the board does not exist.')
  return await boardResponse(response) as BoardEvent & BoardSnapshot
}

export async function readBoardEvents(key: string, signal: AbortSignal, afterRevision = -1, throughRevision?: number): Promise<BoardEvents> {
  const query = new URLSearchParams({ after_revision: String(afterRevision), limit: '20' })
  if (throughRevision !== undefined) query.set('through_revision', String(throughRevision))
  const response = await fetch(`${API_BASE_URL}/${key}/events?${query}`, { signal, cache: 'no-store' })
  if (!response.ok) await boardResponse(response)
  return response.json()
}

export async function readHistoryWindow(key: string, signal: AbortSignal, throughRevision?: number): Promise<BoardEvents> {
  const through = throughRevision ?? (await readBoard(key, signal)).revision
  return readBoardEvents(key, signal, Math.max(-1, through - 20), through)
}
