import { useEffect, useState } from 'react'
import type { HelpResponse } from '@/services/api'
import DocumentFooter from './DocumentFooter'

export default function ReadOnlyMessage({ load, label, statusId, initialMessage }: {
  load: (signal: AbortSignal) => Promise<HelpResponse>
  label: string
  statusId: string
  initialMessage?: HelpResponse | null
}) {
  const [message, setMessage] = useState<HelpResponse | null>(initialMessage ?? null)
  const [error, setError] = useState(false)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), 15000)
    let active = true
    load(controller.signal)
      .then((instructions) => { if (active) setMessage(instructions) })
      .catch(() => { if (active) setError(true) })
      .finally(() => clearTimeout(timeout))
    return () => {
      active = false
      clearTimeout(timeout)
      controller.abort()
    }
  }, [attempt, load])

  return (
    <>
      <div
        role="region"
        aria-label={label}
        aria-describedby={statusId}
        className="block min-h-full w-full whitespace-pre-wrap [overflow-wrap:anywhere] bg-background p-6 pb-12 font-mono text-sm leading-6 text-foreground"
      >
        {message && <>
          <h1 className="m-0 mb-6 font-inherit text-inherit">{message.title}</h1>
          {message.content.split(/(https?:\/\/[^\s]+|(?<![/\w])bboard\.ai\/help)/).map((part, index) => (
            /^(https?:\/\/|bboard\.ai\/help$)/.test(part) ? (
              <span key={index}><a href={part.startsWith('http') ? part.replace(/[.,;:!?)]+$/, '') : '/help'}
                className="rounded text-blue-600 underline underline-offset-2 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                {part.replace(/[.,;:!?)]+$/, '')}
              </a>{part.match(/[.,;:!?)]+$/)?.[0]}</span>
            ) : part
          ))}
        </>}
      </div>
      <DocumentFooter statusId={statusId} content={message ? `${message.title}\n\n${message.content}` : undefined}>
        {error ? 'Could not load the message.' : message ? null : 'Loading…'}
        {error && (
          <button
            type="button"
            onClick={() => { setError(false); setAttempt((value) => value + 1) }}
            className="ml-2 rounded underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            Try again
          </button>
        )}
      </DocumentFooter>
    </>
  )
}
