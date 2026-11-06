/** One committed write at a time, plus one coalesced replacement. */
export type SyncState = {
  content: string
  loaded: boolean
  status: 'connecting' | 'saved' | 'saving' | 'offline' | 'error'
  error?: string
}
type Write = { type: 'write'; client_id: string; sequence: number; content: string }
type Remote = { content: string; revision: number }
type Socket = Pick<WebSocket, 'send' | 'close' | 'onmessage' | 'onclose' | 'onerror'>
const MAX_BYTES = 65536
export const MAX_CHARACTERS = 5000
const LIMIT_MESSAGE = 'Boards can hold up to 5,000 characters, without null characters.'
const contentFits = (content: string) => Array.from(content).length <= MAX_CHARACTERS && !content.includes('\0')
const MIN_SEND_INTERVAL_MS = 120

export class DocumentSync {
  private state: SyncState = { content: '', loaded: false, status: 'connecting' }
  private listeners = new Set<() => void>()
  private clientId = crypto.randomUUID()
  private sequence = 0
  private pending: Write | null = null
  private flight: Write | null = null
  private remote: Remote = { content: '', revision: -1 }
  private socket: Socket | null = null
  private ready = false
  private sent = false
  private stopped = true
  private retry = 0
  private retryDelay = 0
  private timer: ReturnType<typeof setTimeout> | undefined
  private reconnect: ReturnType<typeof setTimeout> | undefined
  private watchdog: ReturnType<typeof setTimeout> | undefined
  private lastSentAt = -Infinity
  private now: () => number
  private fatal = false
  private draftKey: string
  private storage: Storage | undefined

  constructor(privateUrl: string, options: { storage?: Storage; connect?: (url: string) => Socket; now?: () => number; draftKey: string; initialContent?: string }) {
    this.draftKey = options.draftKey
    this.now = options.now ?? (() => performance.now())
    this.url = privateUrl
    this.connectSocket = options.connect ?? ((url) => new WebSocket(url))
    this.storage = options.storage
    if (options.initialContent !== undefined) {
      this.state.content = options.initialContent
      this.state.loaded = true
    }
    try {
      const saved = JSON.parse(this.storage?.getItem(this.draftKey) ?? 'null')
      if (saved && Number.isSafeInteger(saved.sequence) &&
          saved.sequence >= 0 && this.validDraft(saved.flight, saved.sequence) &&
          this.validDraft(saved.pending, saved.sequence)) {
        // New page instances get a fresh writer identity, including duplicated tabs.
        // Restored writes retain their original identity for idempotent replay.
        this.sequence = saved.sequence
        this.flight = saved.flight
        this.pending = saved.pending
        this.state.content = this.pending?.content ?? this.flight?.content ?? this.state.content
      }
    } catch { /* A blocked or corrupt session store must not prevent editing. */ }
  }
  private url: string
  private connectSocket: (url: string) => Socket

  private validDraft(value: Write | null, sequence: number) {
    return value === null || (value?.type === 'write' && typeof value.client_id === 'string' &&
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value.client_id) &&
      Number.isSafeInteger(value.sequence) && value.sequence > 0 && value.sequence <= sequence &&
      typeof value.content === 'string' && !value.content.includes('\0') && new TextEncoder().encode(value.content).length <= MAX_BYTES)
  }

  getSnapshot = () => this.state
  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    if (this.stopped && !this.fatal) {
      this.stopped = false
      this.connect()
    }
    return () => {
      this.listeners.delete(listener)
      if (this.listeners.size === 0) this.stop()
    }
  }

  edit = (content: string) => {
    if (this.fatal) return
    if (!contentFits(content)) {
      this.publish({ ...this.state, error: LIMIT_MESSAGE })
      return
    }
    // Replace an unsent draft from before the limit changed once the user edits it.
    if (this.flight && !this.sent && !contentFits(this.flight.content)) this.flight = null
    this.pending = { type: 'write', client_id: this.clientId, sequence: ++this.sequence, content }
    this.persist()
    this.publish({ content, loaded: this.state.loaded, status: this.ready ? 'saving' : 'offline' })
    this.scheduleFlush()
  }

  private publish(state: SyncState) {
    this.state = state
    this.listeners.forEach((listener) => listener())
  }

  private persist() {
    try {
      this.storage?.setItem(this.draftKey, JSON.stringify({ sequence: this.sequence,
        pending: this.pending, flight: this.flight }))
    } catch { /* The live queue still retains unsaved edits in memory. */ }
  }

  private scheduleFlush() {
    if (!this.timer && this.ready && !this.sent) {
      this.timer = setTimeout(() => {
        this.timer = undefined
        this.flush()
      }, Math.max(0, MIN_SEND_INTERVAL_MS - (this.now() - this.lastSentAt)))
    }
  }

  private flush() {
    if (!this.socket || !this.ready || this.sent) return
    if (this.flight && !contentFits(this.flight.content) && this.pending && contentFits(this.pending.content)) {
      this.flight = null
    }
    if (!this.flight) {
      this.flight = this.pending
      this.pending = null
    }
    if (!this.flight) return
    // Keep old oversized drafts visible and stored, but never send them repeatedly.
    if (!contentFits(this.flight.content)) {
      this.publish({ ...this.state, status: 'offline', error: LIMIT_MESSAGE + ' Shorten this draft to save it.' })
      return
    }
    this.persist()
    this.sent = true
    this.lastSentAt = this.now()
    this.watchdog = setTimeout(() => this.socket?.close(), 10000)
    try {
      this.socket.send(JSON.stringify(this.flight))
    } catch {
      this.socket.close()
    }
  }

  private connect() {
    if (this.stopped) return
    this.ready = false
    this.sent = false
    // A restart may use another instance or a newly initialized local database.
    this.remote = { content: '', revision: -1 }
    let socket: Socket
    try {
      socket = this.connectSocket(this.url)
    } catch {
      this.disconnected()
      return
    }
    this.socket = socket
    this.watchdog = setTimeout(() => socket.close(), 10000)
    socket.onmessage = (event) => {
      if (socket !== this.socket || this.stopped) return
      let message
      try { message = JSON.parse(String(event.data)) } catch { socket.close(); return }
      if (message?.type === 'error') {
        if (message.code === 'board_not_found') {
          this.fatal = true
          this.publish({ ...this.state, status: 'error', error: 'Board not found. Check the link or create a new board.' })
          socket.close()
          return
        }
        if (message.code === 'rate_limit_exceeded') {
          this.retryDelay = Math.min(60000, Math.max(1000, Number(message.retry_after) * 1000 || 1000))
        }
        this.publish({ ...this.state, status: 'offline', error: message.code === 'rate_limit_exceeded'
          ? 'Sync paused briefly. Your edits are queued.' : 'Sync unavailable. Your edits are queued.' })
        socket.close()
        return
      }
      if (!message || !['snapshot', 'ack'].includes(message.type) ||
          !Number.isSafeInteger(message.revision) || message.revision < 0 || typeof message.content !== 'string') {
        socket.close()
        return
      }
      if (message.revision > this.remote.revision) this.remote = message
      if (!this.ready) {
        clearTimeout(this.watchdog)
        this.ready = true
      }
      if (message.type === 'ack' && this.flight?.sequence === message.sequence) {
        clearTimeout(this.watchdog)
        this.flight = null
        this.sent = false
        this.retry = 0
        this.persist()
      }
      if (!this.flight && !this.pending) {
        this.publish({ content: this.remote.content, loaded: true, status: 'saved' })
      } else {
        this.publish({ content: this.state.content, loaded: true, status: 'saving' })
        this.scheduleFlush()
      }
    }
    socket.onerror = () => socket.close()
    socket.onclose = () => {
      if (socket !== this.socket) return
      this.socket = null
      this.disconnected()
    }
  }

  private disconnected() {
    clearTimeout(this.watchdog)
    clearTimeout(this.timer)
    this.timer = undefined
    this.ready = false
    this.sent = false
    if (this.stopped || this.fatal) return
    this.publish({ ...this.state, status: 'offline' })
    const delay = Math.max(this.retryDelay, Math.min(30000, 1000 * 2 ** Math.min(this.retry++, 5)))
    this.retryDelay = 0
    this.reconnect = setTimeout(() => this.connect(), delay + Math.random() * 250)
  }

  private stop() {
    this.stopped = true
    clearTimeout(this.timer)
    clearTimeout(this.reconnect)
    clearTimeout(this.watchdog)
    this.timer = undefined
    this.ready = false
    this.sent = false
    const socket = this.socket
    this.socket = null
    socket?.close()
    this.persist()
  }
}
