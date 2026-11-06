import { useId, useRef, useState, type KeyboardEvent } from 'react'
import { Check, Copy, Link, TriangleAlert } from 'lucide-react'

function focusFirstMenuItem(node: HTMLDivElement | null) {
  node?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus()
}

export default function CopyButton({ content }: { content?: string }) {
  const pending = useRef(false)
  const trigger = useRef<HTMLButtonElement>(null)
  const menuId = useId()
  const [open, setOpen] = useState(false)
  const [copying, setCopying] = useState(false)
  const [result, setResult] = useState<{ value: string; kind: 'content' | 'url'; status: 'copied' | 'error' } | null>(null)
  const status = result?.value === (result?.kind === 'url' ? window.location.href : content) ? result?.status : undefined
  const feedback = status === 'copied' ? `${result?.kind === 'url' ? 'URL' : 'Content'} copied` : status === 'error' ? 'Could not copy. Try again.' : ''

  function closeMenu() {
    setOpen(false)
    trigger.current?.focus()
  }

  function navigateMenu(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === 'Escape') {
      event.preventDefault()
      closeMenu()
      return
    }
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
    event.preventDefault()
    const items = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')]
    const index = items.indexOf(document.activeElement as HTMLButtonElement)
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
      : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length
    items[next]?.focus()
  }

  async function copy(kind: 'content' | 'url') {
    const value = kind === 'url' ? window.location.href : content
    if (value === undefined || pending.current) return
    closeMenu()
    pending.current = true
    setCopying(true)
    setResult(null)
    try {
      await navigator.clipboard.writeText(value)
      setResult({ value, kind, status: 'copied' })
    } catch {
      setResult({ value, kind, status: 'error' })
    } finally {
      pending.current = false
      setCopying(false)
    }
  }

  return (
    <div className="relative" onMouseLeave={() => setResult(null)} onBlur={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget)) {
        setOpen(false)
        setResult(null)
      }
    }}>
      <button
        ref={trigger}
        type="button"
        onClick={() => { if (!pending.current) { setResult(null); setOpen(!open) } }}
        onKeyDown={(event) => {
          if (event.key === 'ArrowDown' && !pending.current) {
            event.preventDefault()
            setOpen(true)
          }
        }}
        aria-label="Copy"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-busy={copying}
        title="Copy"
        className="flex h-10 w-10 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
      >
        {status === 'copied' ? <Check className="h-4 w-4" aria-hidden="true" />
          : status === 'error' ? <TriangleAlert className="h-4 w-4" aria-hidden="true" />
            : <Copy className="h-4 w-4" aria-hidden="true" />}
      </button>
      {open && <>
        <div aria-hidden="true" className="fixed inset-0 z-40" onClick={closeMenu} />
        <div
          id={menuId}
          role="menu"
          aria-label="Copy options"
          ref={focusFirstMenuItem}
          onKeyDown={navigateMenu}
          className="absolute right-0 top-full z-50 mt-2 w-44 rounded-lg border border-border bg-background p-1 shadow-lg"
        >
          <button type="button" role="menuitem" tabIndex={-1} disabled={content === undefined} onPointerMove={(event) => event.currentTarget.focus()} onClick={() => copy('content')} className="flex w-full items-center gap-2 rounded px-3 py-2 text-left text-sm text-foreground focus:bg-muted focus:outline-none disabled:opacity-50">
            <Copy className="h-4 w-4" aria-hidden="true" /> Copy content
          </button>
          <button type="button" role="menuitem" tabIndex={-1} onPointerMove={(event) => event.currentTarget.focus()} onClick={() => copy('url')} className="flex w-full items-center gap-2 rounded px-3 py-2 text-left text-sm text-foreground focus:bg-muted focus:outline-none">
            <Link className="h-4 w-4" aria-hidden="true" /> Copy URL
          </button>
        </div>
      </>}
      <span role="status" className={feedback ? 'absolute top-full right-0 mt-2 whitespace-nowrap rounded bg-background px-2 py-1 text-xs text-foreground shadow-sm' : 'sr-only'}>
        {feedback}
      </span>
    </div>
  )
}
