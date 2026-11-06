import assert from 'node:assert/strict'
import { test } from 'node:test'
import { DocumentSync } from '../src/services/document-sync.ts'

class FakeSocket {
  sent = []
  send(raw) { this.sent.push(JSON.parse(raw)) }
  close() { this.onclose?.({}) }
  receive(message) { this.onmessage?.({ data: JSON.stringify(message) }) }
}
function setup(t, storage) {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'] })
  const sockets = []
  const sync = new DocumentSync('ws://example.test/ws/boards/test-board', {
    storage, draftKey: 'bboard.board.test-board.v1', now: () => Date.now(), connect: () => { const socket = new FakeSocket(); sockets.push(socket); return socket },
  })
  const stop = sync.subscribe(() => {})
  t.after(stop)
  return { sync, sockets, stop, tick: (ms = 120) => t.mock.timers.tick(ms) }
}
const snapshot = (content, revision) => ({ type: 'snapshot', content, revision })
const ack = (content, revision, sequence) => ({ type: 'ack', content, revision, sequence })

test('initial snapshot loads, typing is immediate and bursts coalesce', (t) => {
  const { sync, sockets: [socket], tick } = setup(t)
  socket.receive(snapshot('saved text', 2))
  assert.equal(sync.getSnapshot().content, 'saved text')
  sync.edit('a'); sync.edit('ab'); sync.edit('abc')
  assert.equal(sync.getSnapshot().content, 'abc')
  assert.equal(socket.sent.length, 0)
  tick()
  assert.equal(socket.sent.length, 1)
  assert.equal(socket.sent[0].content, 'abc')
  sync.edit('abcd'); sync.edit('abcde'); tick()
  assert.equal(socket.sent.length, 1)
  socket.receive(ack('abc', 3, socket.sent[0].sequence))
  assert.equal(sync.getSnapshot().content, 'abcde')
  tick()
  assert.equal(socket.sent.length, 2)
  socket.receive(ack('abcde', 4, socket.sent[1].sequence))
  assert.equal(sync.getSnapshot().status, 'saved')
})

test('remote snapshots cannot erase queued typing; stale acks cannot rewind revision', (t) => {
  const { sync, sockets: [socket], tick } = setup(t)
  socket.receive(snapshot('', 0))
  sync.edit('local'); tick()
  socket.receive(snapshot('other writer', 3))
  assert.equal(sync.getSnapshot().content, 'local')
  socket.receive(ack('local', 2, socket.sent[0].sequence))
  assert.equal(sync.getSnapshot().content, 'other writer')
  socket.receive(snapshot('stale', 1))
  assert.equal(sync.getSnapshot().content, 'other writer')
})

test('disconnect replays the same write identity, then sends the newest queued edit', (t) => {
  const { sync, sockets, tick } = setup(t)
  sockets[0].receive(snapshot('', 0))
  sync.edit('first'); tick()
  const original = sockets[0].sent[0]
  sockets[0].close()
  sync.edit('latest offline edit')
  tick(1500)
  sockets[1].receive(snapshot('remote while offline', 5)); tick()
  assert.deepEqual(sockets[1].sent[0], original)
  sockets[1].receive(ack('remote while offline', 5, original.sequence)); tick()
  assert.equal(sockets[1].sent[1].content, 'latest offline edit')
})

test('empty text is a real save and edits over 5,000 characters are rejected', (t) => {
  const { sync, sockets: [socket], tick } = setup(t)
  socket.receive(snapshot('erase this', 1))
  sync.edit(''); tick()
  assert.equal(socket.sent[0].content, '')
  sync.edit('é'.repeat(5001))
  assert.equal(sync.getSnapshot().content, '')
  assert.match(sync.getSnapshot().error, /5,000 characters/)
})

test('5,000 emoji count as 5,000 characters and the next is rejected', (t) => {
  const { sync, sockets: [socket], tick } = setup(t)
  socket.receive(snapshot('', 0))
  const content = '😀'.repeat(5000)
  sync.edit(content); tick()
  assert.equal(socket.sent[0].content, content)
  sync.edit(content + '😀'); tick()
  assert.equal(sync.getSnapshot().content, content)
  assert.equal(socket.sent.length, 1)
  assert.match(sync.getSnapshot().error, /5,000/)
})

test('old oversized drafts survive reload without retrying until shortened', (t) => {
  const key = 'bboard.board.test-board.v1'
  const old = { type: 'write', client_id: crypto.randomUUID(), sequence: 1, content: 'a'.repeat(6000) }
  const values = new Map([[key, JSON.stringify({ sequence: 1, flight: old, pending: null })]])
  const storage = { getItem: (key) => values.get(key), setItem: (key, value) => values.set(key, value) }
  const { sync, sockets: [socket], tick } = setup(t, storage)
  socket.receive(snapshot('remote', 2)); tick()
  assert.equal(sync.getSnapshot().content, old.content)
  assert.match(sync.getSnapshot().error, /Shorten/)
  assert.equal(socket.sent.length, 0)
  sync.edit('a'.repeat(5000)); tick()
  assert.equal(socket.sent[0].content.length, 5000)
  assert.equal(socket.sent[0].sequence, 2)
})

test('typing before first connection is kept when initial snapshot arrives', (t) => {
  const { sync, sockets: [socket], tick } = setup(t)
  sync.edit('new draft')
  socket.receive(snapshot('existing remote text', 10)); tick()
  assert.equal(sync.getSnapshot().content, 'new draft')
  assert.equal(socket.sent[0].content, 'new draft')
})

test('server rate-limit pause preserves the queue and respects retry delay', (t) => {
  const { sync, sockets, tick } = setup(t)
  sockets[0].receive(snapshot('', 0))
  sync.edit('draft'); tick()
  sockets[0].receive({ type: 'error', code: 'rate_limit_exceeded', retry_after: 10 })
  tick(9000)
  assert.equal(sockets.length, 1)
  assert.equal(sync.getSnapshot().content, 'draft')
  tick(1500)
  assert.equal(sockets.length, 2)
})

test('unsaved text and receipt identity survive reload using tab storage', (t) => {
  const values = new Map()
  const storage = { getItem: (key) => values.get(key), setItem: (key, value) => values.set(key, value) }
  const { sync, sockets: [socket], tick, stop } = setup(t, storage)
  socket.receive(snapshot('', 0)); sync.edit('first'); tick()
  sync.edit('later'); stop()
  const nextSocket = new FakeSocket()
  const next = new DocumentSync('ws://example.test', { storage, draftKey: 'bboard.board.test-board.v1', connect: () => nextSocket })
  const nextStop = next.subscribe(() => {})
  t.after(nextStop)
  assert.equal(next.getSnapshot().content, 'later')
  nextSocket.receive(snapshot('', 0)); tick()
  assert.deepEqual(nextSocket.sent[0], socket.sent[0])
})

test('duplicated tab has a fresh writer identity while replaying old writes unchanged', (t) => {
  const values = new Map()
  const storage = { getItem: (key) => values.get(key), setItem: (key, value) => values.set(key, value) }
  const { sync, sockets: [socket], tick } = setup(t, storage)
  socket.receive(snapshot('', 0)); sync.edit('first'); tick()
  const copied = new Map(values)
  const duplicateSocket = new FakeSocket()
  const duplicate = new DocumentSync('ws://example.test', {
    draftKey: 'bboard.board.test-board.v1',
    storage: { getItem: (key) => copied.get(key), setItem: (key, value) => copied.set(key, value) },
    connect: () => duplicateSocket,
  })
  t.after(duplicate.subscribe(() => {}))
  duplicateSocket.receive(snapshot('first', 1)); tick()
  assert.deepEqual(duplicateSocket.sent[0], socket.sent[0])
  duplicateSocket.receive(ack('first', 1, socket.sent[0].sequence))
  duplicate.edit('from duplicate'); tick()
  assert.notEqual(duplicateSocket.sent[1].client_id, socket.sent[0].client_id)
})

test('idle edits send on the next tick without a debounce pause', (t) => {
  const { sync, sockets: [socket], tick } = setup(t)
  socket.receive(snapshot('', 0))
  sync.edit('first keystroke'); tick(0)
  assert.equal(socket.sent.length, 1)
})

test('queued text sends immediately after an ack when the interval has elapsed', (t) => {
  const { sync, sockets: [socket], tick } = setup(t)
  socket.receive(snapshot('', 0))
  sync.edit('first'); tick(120)
  sync.edit('newest'); tick(150)
  socket.receive(ack('first', 1, socket.sent[0].sequence)); tick(0)
  assert.equal(socket.sent.length, 2)
  assert.equal(socket.sent[1].content, 'newest')
})

test('board drafts and in-flight writes are isolated in tab storage', (t) => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'] })
  const saved = new Map()
  const storage = { getItem: key => saved.get(key), setItem: (key,value) => saved.set(key,value) }
  const aSocket = new FakeSocket(), bSocket = new FakeSocket()
  const a = new DocumentSync('ws://example.test/ws/boards/a', {storage, draftKey:'board-a', connect:()=>aSocket})
  const stopA = a.subscribe(()=>{})
  aSocket.receive(snapshot('',0)); a.edit('private to A'); t.mock.timers.tick(120); stopA()
  const b = new DocumentSync('ws://example.test/ws/boards/b', {storage, draftKey:'board-b', connect:()=>bSocket})
  t.after(b.subscribe(()=>{}))
  assert.equal(b.getSnapshot().content,'')
  bSocket.receive(snapshot('B from server',3))
  assert.equal(b.getSnapshot().content,'B from server')
  assert.equal(bSocket.sent.length,0)
  const replaySocket = new FakeSocket()
  const replay = new DocumentSync('ws://example.test/ws/boards/a', {storage, draftKey:'board-a', connect:()=>replaySocket})
  t.after(replay.subscribe(()=>{}))
  assert.equal(replay.getSnapshot().content,'private to A')
  replaySocket.receive(snapshot('',0)); t.mock.timers.tick(120)
  assert.equal(replaySocket.sent[0].content,'private to A')
})

test('unknown board is a terminal error instead of an endless reconnect loop', (t) => {
  const {sync,sockets,tick} = setup(t)
  sockets[0].receive({type:'error',code:'board_not_found'})
  tick(60000)
  assert.equal(sockets.length,1)
  assert.equal(sync.getSnapshot().status,'error')
  assert.match(sync.getSnapshot().error,/Board not found/)
})
