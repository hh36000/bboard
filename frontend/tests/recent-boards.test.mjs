import assert from 'node:assert/strict'
import { test } from 'node:test'
import { clearRecentBoards, readRecentBoards, rememberRecentBoard, RECENT_BOARDS_KEY } from '../src/services/recent-boards.ts'

const key = (number) => number.toString(16).padStart(64, '0')
function storage() {
  const values = new Map()
  return {
    values,
    getItem: (name) => values.get(name) ?? null,
    setItem: (name, value) => values.set(name, value),
    removeItem: (name) => values.delete(name),
  }
}

test('visits persist across readers with newest first; a revisit moves to the top without duplicates', () => {
  const store = storage()
  rememberRecentBoard(key(1), store, 10)
  rememberRecentBoard(key(2), store, 20)
  rememberRecentBoard(key(1), store, 30)
  assert.deepEqual(readRecentBoards(store).boards, [
    { key: key(1), visitedAt: 30 }, { key: key(2), visitedAt: 20 },
  ])
  assert.equal(JSON.parse(store.getItem(RECENT_BOARDS_KEY)).length, 2)
})

test('keeps exactly the 100 most recently visited unique boards in storage', () => {
  const store = storage()
  for (let i = 1; i <= 150; i++) rememberRecentBoard(key(i), store, i)
  const boards = readRecentBoards(store).boards
  assert.equal(boards.length, 100)
  assert.equal(boards[0].key, key(150))
  assert.equal(boards[99].key, key(51))
  rememberRecentBoard(key(51), store, 151)
  assert.equal(readRecentBoards(store).boards[0].key, key(51))
  assert.equal(JSON.parse(store.getItem(RECENT_BOARDS_KEY)).length, 100)
})

test('non-board routes and foreign or malformed URLs are never stored', () => {
  const store = storage()
  for (const value of ['/', '/recent', '/help', key(1).toUpperCase().replace('1', 'A'), '/'+key(2), 'https://elsewhere.test/'+key(1), 'javascript:alert(1)']) {
    assert.equal(rememberRecentBoard(value, store), false)
  }
  assert.equal(store.values.size, 0)
})

test('corrupt storage recovers and read data is validated, deduplicated, and stripped of extra fields', () => {
  const store = storage()
  store.setItem(RECENT_BOARDS_KEY, 'not JSON')
  assert.deepEqual(readRecentBoards(store).boards, [])
  rememberRecentBoard(key(1), store, 10)
  assert.equal(readRecentBoards(store).boards.length, 1)
  store.setItem(RECENT_BOARDS_KEY, JSON.stringify([
    { key: key(1), visitedAt: 10, content: 'do not store board text' },
    { key: key(1), visitedAt: 20 },
    { key: key(2), visitedAt: -1 },
    { key: key(3), visitedAt: 1e20 },
    { key: 'https://elsewhere.test', visitedAt: 30 }, null,
  ]))
  assert.deepEqual(readRecentBoards(store).boards, [{ key: key(1), visitedAt: 20 }])
  rememberRecentBoard(key(4), store, 40)
  assert.ok(!store.getItem(RECENT_BOARDS_KEY).includes('content'))
})

test('unavailable or full storage never prevents the board from opening', () => {
  const unavailable = { getItem() { throw new Error('blocked') }, setItem() { throw new Error('full') }, removeItem() { throw new Error('blocked') } }
  assert.equal(rememberRecentBoard(key(1), unavailable), false)
  assert.equal(clearRecentBoards(unavailable), false)
  assert.match(readRecentBoards(unavailable).error, /storage is unavailable/)
  assert.deepEqual(readRecentBoards(unavailable).boards, [])
})

test('clearing history preserves other browser data and drafts', () => {
  const store = storage()
  store.setItem('theme', 'dark')
  store.setItem('bboard.board.draft', 'unsaved draft')
  rememberRecentBoard(key(1), store, 1)
  assert.equal(clearRecentBoards(store), true)
  assert.equal(store.getItem(RECENT_BOARDS_KEY), null)
  assert.equal(store.getItem('theme'), 'dark')
  assert.equal(store.getItem('bboard.board.draft'), 'unsaved draft')
})
