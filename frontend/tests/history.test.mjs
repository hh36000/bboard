import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readHistoryWindow, readBoardRevision } from '../src/services/api.ts'

const key = 'a'.repeat(64)

test('history opens the latest 20 saved states with a fixed upper revision', async (t) => {
  const requests = []
  t.mock.method(globalThis, 'fetch', async (url) => {
    requests.push(new URL(url))
    if (requests.length === 1) return Response.json({ key, content: 'current', revision: 28 })
    return Response.json({ key, first_revision: 0, through_revision: 28, events: [], next_after_revision: null })
  })
  await readHistoryWindow(key, new AbortController().signal)
  assert.equal(requests[1].pathname, `/${key}/events`)
  assert.equal(requests[1].searchParams.get('after_revision'), '8')
  assert.equal(requests[1].searchParams.get('through_revision'), '28')
  assert.equal(requests[1].searchParams.get('limit'), '20')
})

test('older history keeps its boundary even if the live board changes', async (t) => {
  const requests = []
  t.mock.method(globalThis, 'fetch', async (url) => {
    requests.push(new URL(url))
    return Response.json({ key, first_revision: 7, through_revision: 8, events: [], next_after_revision: null })
  })
  await readHistoryWindow(key, new AbortController().signal, 8)
  assert.equal(requests.length, 1)
  assert.equal(requests[0].searchParams.get('after_revision'), '-1')
  assert.equal(requests[0].searchParams.get('through_revision'), '8')
})

test('a missing historical revision fails explicitly instead of returning live text', async (t) => {
  t.mock.method(globalThis, 'fetch', async (url) => {
    assert.equal(new URL(url).pathname, `/${key}/revisions/3`)
    return Response.json({ detail: 'revision_not_recorded' }, { status: 404 })
  })
  await assert.rejects(readBoardRevision(key, 3, new AbortController().signal), /revision was not recorded/)
})
