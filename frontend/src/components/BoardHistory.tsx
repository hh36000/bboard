import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { readBoardRevision, readHistoryWindow } from '@/services/api'
import type { BoardEvent, BoardEvents } from '@/services/api'

function SavedEvent({ event, boardId }: { event: BoardEvent; boardId: string }) {
  return <article className="border-t border-border py-5" aria-label={`Revision ${event.revision}`}>
    <header className="mb-3 flex flex-wrap items-center gap-x-3 text-xs text-muted-foreground">
      <Link to={`/${boardId}?revision=${event.revision}`} className="text-foreground hover:underline" aria-label={`Read revision ${event.revision}`}>#{event.revision}</Link>
      <time dateTime={event.recorded_at} title={event.recorded_at}>{event.recorded_at.replace(/\.\d+Z$/, 'Z').replace('T', ' ').replace('Z', ' UTC')}</time>
    </header>
    <pre aria-label="Saved text" className="whitespace-pre-wrap wrap-anywhere font-inherit">{event.content || '(empty)'}</pre>
    {event.change && event.kind !== 'created' && (event.change.removed || event.change.inserted) && <div className="mt-3 text-xs text-muted-foreground">
      {event.change.removed && <pre aria-label="Removed text" className="whitespace-pre-wrap wrap-anywhere">{event.change.removed.split('\n').map(line => `- ${line}`).join('\n')}</pre>}
      {event.change.inserted && <pre aria-label="Inserted text" className="whitespace-pre-wrap wrap-anywhere">{event.change.inserted.split('\n').map(line => `+ ${line}`).join('\n')}</pre>}
    </div>}
    {event.kind === 'baseline' && <p className="mt-3 text-xs text-muted-foreground">Earlier revisions were not recorded.</p>}
  </article>
}

export default function BoardHistory({ boardId, revision }: { boardId: string; revision?: number }) {
  const navigate = useNavigate()
  const [page, setPage] = useState<BoardEvents | null>(null)
  const [selected, setSelected] = useState<BoardEvent | null>(null)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(15000)])
    readHistoryWindow(boardId, signal).then(setPage)
      .catch(error => { if (!controller.signal.aborted) setError(error instanceof Error ? error.message : 'Could not load history.') })
    return () => controller.abort()
  }, [boardId, attempt])

  const cachedRevision = page?.events.find(event => event.revision === revision)
  useEffect(() => {
    if (revision === undefined || cachedRevision) return
    const controller = new AbortController()
    // Scrubbing cached events is immediate; fetch older states when the handle pauses.
    const timer = setTimeout(() => {
      const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(15000)])
      readBoardRevision(boardId, revision, signal).then(setSelected)
        .catch(error => { if (!controller.signal.aborted) setError(error instanceof Error ? error.message : 'Could not load history.') })
    }, 200)
    return () => { clearTimeout(timer); controller.abort() }
  }, [boardId, revision, cachedRevision, attempt])

  function chooseRevision(value: number) {
    if (!page || !Number.isSafeInteger(value) || value < page.first_revision || value > page.through_revision) return
    setError('')
    navigate(`/${boardId}?revision=${value}`, { replace: true })
  }

  async function older() {
    if (!page || busy || !page.events.length) return
    setBusy(true)
    setError('')
    try {
      const previous = await readHistoryWindow(boardId, AbortSignal.timeout(15000), page.events[0].revision - 1)
      setPage({ ...page, events: [...previous.events, ...page.events] })
    } catch (error) { setError(error instanceof Error ? error.message : 'Could not load history.') }
    finally { setBusy(false) }
  }

  const current = cachedRevision ?? (selected?.revision === revision ? selected : null)
  const events = revision === undefined ? [...(page?.events ?? [])].reverse() : current ? [current] : []
  const sliderRevision = revision ?? page?.through_revision ?? 0
  return <div className="min-h-full p-6 pb-12 font-mono text-sm leading-6">
    <form className="mb-5 text-xs" onSubmit={event => {
      event.preventDefault()
      const value = new FormData(event.currentTarget).get('revision')
      chooseRevision(Number(value))
    }}>
      <div className="flex justify-end text-muted-foreground">
        <input key={sliderRevision} name="revision" aria-label="Revision number" type="number"
          min={page?.first_revision ?? 0} max={page?.through_revision ?? 0} step="1" required disabled={!page}
          defaultValue={page ? sliderRevision : ''}
          onBlur={event => {
            if (!event.currentTarget.checkValidity()) event.currentTarget.value = String(sliderRevision)
            else if (event.currentTarget.valueAsNumber !== sliderRevision) chooseRevision(event.currentTarget.valueAsNumber)
          }}
          className="revision-number w-20 bg-transparent text-right tabular-nums" />
      </div>
      <input aria-label="Revision timeline" aria-valuetext={`Revision ${sliderRevision}`}
        type="range" min={page?.first_revision ?? 0} max={page?.through_revision ?? 0} step="1"
        value={sliderRevision} disabled={!page || page.first_revision === page.through_revision}
        onChange={event => chooseRevision(event.currentTarget.valueAsNumber)} className="revision-slider" />
    </form>
    {!events.length && !error && <p role="status">Loading history…</p>}
    {error && <p role="alert" className="my-4 text-xs">{error} <button className="underline" onClick={() => { setError(''); if (page && revision === undefined) void older(); else setAttempt(value => value + 1) }}>retry</button></p>}
    {events.map(event => <SavedEvent key={event.revision} event={event} boardId={boardId} />)}
    {revision === undefined && page && page.events[0]?.revision > page.first_revision && <button disabled={busy} onClick={() => void older()} className="mt-4 text-xs text-muted-foreground underline underline-offset-4">{busy ? 'loading…' : 'older'}</button>}
  </div>
}
