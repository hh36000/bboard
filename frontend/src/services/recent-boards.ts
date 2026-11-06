export const RECENT_BOARDS_KEY = 'bboard.recent.v1'
export const RECENT_BOARDS_LIMIT = 100
const BOARD_KEY = /^[0-9a-f]{64}$/

export type RecentBoard = { key: string; visitedAt: number }
export type RecentBoardsState = { boards: RecentBoard[]; error: string | null }
type HistoryStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

function decode(raw: string | null): RecentBoard[] {
  let value: unknown
  try { value = JSON.parse(raw ?? '[]') } catch { return [] }
  if (!Array.isArray(value)) return []
  const unique = new Map<string, RecentBoard>()
  for (const entry of value) {
    if (!entry || typeof entry.key !== 'string' || !BOARD_KEY.test(entry.key) ||
        !Number.isSafeInteger(entry.visitedAt) || entry.visitedAt < 0 || entry.visitedAt > 8640000000000000) continue
    if (!unique.has(entry.key) || unique.get(entry.key)!.visitedAt < entry.visitedAt) {
      unique.set(entry.key, { key: entry.key, visitedAt: entry.visitedAt })
    }
  }
  return [...unique.values()].sort((a, b) => b.visitedAt - a.visitedAt).slice(0, RECENT_BOARDS_LIMIT)
}

export function readRecentBoards(storage?: HistoryStorage): RecentBoardsState {
  try {
    return { boards: decode((storage ?? window.localStorage).getItem(RECENT_BOARDS_KEY)), error: null }
  } catch {
    return { boards: [], error: 'Browser storage is unavailable. Recent boards cannot be saved here.' }
  }
}

export function rememberRecentBoard(key: string, storage?: HistoryStorage, visitedAt = Date.now()): boolean {
  if (!BOARD_KEY.test(key)) return false
  try {
    const target = storage ?? window.localStorage
    const boards = decode(target.getItem(RECENT_BOARDS_KEY)).filter((board) => board.key !== key)
    // Only board keys and visit times are persisted, never board text or drafts.
    target.setItem(RECENT_BOARDS_KEY, JSON.stringify([{ key, visitedAt }, ...boards].slice(0, RECENT_BOARDS_LIMIT)))
    return true
  } catch {
    // History must never prevent a board from opening or saving.
    return false
  }
}

export function clearRecentBoards(storage?: HistoryStorage): boolean {
  try {
    (storage ?? window.localStorage).removeItem(RECENT_BOARDS_KEY)
    return true
  } catch {
    return false
  }
}
