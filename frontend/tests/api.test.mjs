import assert from 'node:assert/strict'
import { test } from 'node:test'
import { API_BASE_URL, createBoard } from '../src/services/api.ts'

test('browser creates a blank board through the canonical JSON endpoint', async (t) => {
  const key = 'a'.repeat(64)
  const fetch = t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(url, `${API_BASE_URL}/write`)
    assert.equal(options.method, 'POST')
    assert.equal(options.headers['Content-Type'], 'application/json')
    assert.match(options.headers['Idempotency-Key'],
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
    assert.deepEqual(JSON.parse(options.body), { content: '' })
    assert.ok(options.signal instanceof AbortSignal)
    return Response.json({ key }, { status: 200 })
  })
  assert.equal(await createBoard(), key)
  assert.equal(fetch.mock.callCount(), 1)
})

test('creation preserves rate-limit guidance and rejects malformed returned keys', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({}, { status: 429 }))
  await assert.rejects(createBoard(), /Too many requests/)
  fetch.mock.mockImplementation(async () => Response.json({ key: 'not-a-board' }))
  await assert.rejects(createBoard(), /Could not create the board/)
})

test('uncertain creation failures are surfaced without automatically creating another board', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async () => { throw new TypeError('connection lost') })
  await assert.rejects(createBoard(), /connection lost/)
  assert.equal(fetch.mock.callCount(), 1)
})
