import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import Layout from '@/components/Layout'
import DocumentFooter from '@/components/DocumentFooter'
import { clearRecentBoards, readRecentBoards, RECENT_BOARDS_KEY, RECENT_BOARDS_LIMIT } from '@/services/recent-boards'

const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' })

export default function Recent() {
  const [history, setHistory] = useState(() => readRecentBoards())

  useEffect(() => {
    const refresh = (event: StorageEvent) => {
      if (event.key === RECENT_BOARDS_KEY || event.key === null) setHistory(readRecentBoards())
    }
    window.addEventListener('storage', refresh)
    return () => window.removeEventListener('storage', refresh)
  }, [])

  function clearHistory() {
    if (clearRecentBoards()) setHistory(readRecentBoards())
    else setHistory((current) => ({ ...current, error: 'Could not clear recent boards. Browser storage is unavailable.' }))
  }

  return (
    <Layout>
      <section aria-label="Recent bboards" className="min-h-full p-6 pb-12 font-mono text-sm leading-6">
        <div className="mb-6 flex items-center justify-between gap-4">
          <h1 className="m-0 text-inherit font-normal">Recent bboards</h1>
          {history.boards.length > 0 && <button type="button" onClick={clearHistory} className="shrink-0 rounded text-muted-foreground underline underline-offset-2 hover:text-foreground">
            Clear history
          </button>}
        </div>
        <p className="m-0 mb-6 text-muted-foreground">The last 100 boards opened in this browser, newest first.</p>
        {history.error && <p role="alert" className="mb-6 text-destructive">{history.error}</p>}
        {history.boards.length > 0 ? (
          <ol className="m-0 list-none divide-y divide-border p-0">
            {history.boards.map((board) => (
              <li key={board.key}>
                <Link to={`/${board.key}`} className="flex flex-col gap-1 rounded py-3 hover:bg-muted sm:flex-row sm:items-center sm:justify-between sm:gap-4">
                  <span className="min-w-0 break-all text-blue-600 underline underline-offset-2 dark:text-blue-400">/{board.key}</span>
                  <time dateTime={new Date(board.visitedAt).toISOString()} className="shrink-0 text-xs text-muted-foreground">
                    {dateFormat.format(board.visitedAt)}
                  </time>
                </Link>
              </li>
            ))}
          </ol>
        ) : !history.error && <p className="text-muted-foreground">No boards yet. Open a board or use the plus button to create one.</p>}
        <p className="mt-6 text-xs text-muted-foreground">Stored only in this browser. Clearing history removes these links, not the boards.</p>
      </section>
      <DocumentFooter statusId="recent-status">
        {history.error ? 'Browser storage unavailable' : `${history.boards.length} / ${RECENT_BOARDS_LIMIT} · Stored in this browser`}
      </DocumentFooter>
    </Layout>
  )
}
