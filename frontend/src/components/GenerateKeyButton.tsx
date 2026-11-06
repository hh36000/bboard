import { useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Plus } from 'lucide-react'
import { createBoard } from '@/services/api'
import { rememberRecentBoard } from '@/services/recent-boards'

let unconfirmedIdempotencyKey: string | null = null
function idempotencyKey() {
  if (!unconfirmedIdempotencyKey) {
    try {
      const saved = window.sessionStorage.getItem('bboard.create.idempotency-key')
      if (saved && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(saved)) {
        unconfirmedIdempotencyKey = saved
      }
    } catch { /* Keep it in memory when storage is unavailable. */ }
    unconfirmedIdempotencyKey ??= crypto.randomUUID()
    try { window.sessionStorage.setItem('bboard.create.idempotency-key', unconfirmedIdempotencyKey) } catch { /* Optional. */ }
  }
  return unconfirmedIdempotencyKey
}

export default function GenerateKeyButton() {
  const navigate = useNavigate()
  const pending = useRef(false)
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState('')

  async function generateKey() {
    if (pending.current) return
    pending.current = true
    setCreating(true)
    setError('')
    try {
      const key = await createBoard(idempotencyKey())
      unconfirmedIdempotencyKey = null
      try { window.sessionStorage.removeItem('bboard.create.idempotency-key') } catch { /* Optional. */ }
      rememberRecentBoard(key)
      navigate(`/${key}`)
    } catch (error) {
      setError(error instanceof Error && error.name === 'Error' ? error.message : 'Could not create the board. Try again.')
    } finally {
      pending.current = false
      setCreating(false)
    }
  }

  return (
    <div className="relative">
      <button
        type="button"
        onClick={generateKey}
        disabled={creating}
        aria-label={creating ? 'Creating board…' : 'Create new board'}
        aria-busy={creating}
        title="Create new board"
        className="flex h-10 w-10 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
      >
        <Plus className="h-4 w-4" aria-hidden="true" />
      </button>
      {error && <p role="alert" className="absolute right-0 top-full z-50 mt-2 w-64 rounded-lg border border-border bg-background p-3 text-sm text-foreground shadow-lg">{error}</p>}
    </div>
  )
}
