import { useContext, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { CopyActionContext } from '@/contexts/copy-action-context'
import CopyButton from './CopyButton'

export default function DocumentFooter({ content, statusId, children }: {
  content?: string
  statusId: string
  children: ReactNode
}) {
  const copyActionTarget = useContext(CopyActionContext)
  return (
    <>
      {copyActionTarget && createPortal(<CopyButton content={content} />, copyActionTarget)}
      <div id={statusId} role="status" className="document-footer fixed bottom-0 right-0 max-w-full bg-background px-5 pb-3 pt-1 text-right text-xs text-muted-foreground">
        {children}
      </div>
    </>
  )
}
