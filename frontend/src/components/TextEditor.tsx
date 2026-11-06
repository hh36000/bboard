import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { createDocumentSync } from '../services/shared-document'
import BoardTextArea from './BoardTextArea'
import { MAX_CHARACTERS } from '@/services/document-sync'
import DocumentFooter from './DocumentFooter'
import { rememberRecentBoard } from '@/services/recent-boards'

const labels = {
  connecting: 'Connecting…',
  saved: 'Saved',
  saving: 'Saving…',
  offline: 'Offline · edits queued',
  error: 'Sync unavailable',
}

export default function TextEditor({ boardId }: { boardId: string }) {
  const [sharedDocument] = useState(() => createDocumentSync(boardId))
  const state = useSyncExternalStore(sharedDocument.subscribe, sharedDocument.getSnapshot)
  const recorded = useRef(false)
  useEffect(() => {
    if (state.loaded && state.status !== 'error' && !recorded.current) {
      recorded.current = true
      rememberRecentBoard(boardId)
    }
  }, [boardId, state.loaded, state.status])
  return (
    <>
      <BoardTextArea
        aria-label="Document content"
        aria-describedby="document-sync-status"
        aria-busy={!state.loaded && state.status !== 'error'}
        disabled={!state.loaded || state.status === 'error'}
        value={state.content}
        onChange={(event) => sharedDocument.edit(event.target.value)}
        placeholder={state.loaded && state.status !== 'error' ? 'Start typing...' : undefined}
      />
      <DocumentFooter statusId="document-sync-status" content={state.loaded ? state.content : undefined}>
        {state.loaded && (
          <span className="hidden sm:inline">
            {`${Array.from(state.content).length.toLocaleString()} / ${MAX_CHARACTERS.toLocaleString()} · `}
          </span>
        )}
        {state.error ?? (!state.loaded
          ? state.status === 'offline' ? 'Could not load the board. Retrying…' : 'Loading…'
          : labels[state.status])}
      </DocumentFooter>
    </>
  )
}
