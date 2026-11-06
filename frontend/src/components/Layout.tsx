import { useState, type ReactNode } from 'react'
import Navbar from '@/components/Navbar'
import ContentArea from '@/components/ContentArea'
import { CopyActionContext } from '@/contexts/copy-action-context'

interface LayoutProps {
  children: ReactNode
  navbarActions?: ReactNode
}

function Layout({ children, navbarActions }: LayoutProps) {
  const [copyActionTarget, setCopyActionTarget] = useState<HTMLSpanElement | null>(null)

  return (
    <CopyActionContext.Provider value={copyActionTarget}>
      <div className="flex h-dvh min-h-0 overflow-hidden">
        <div className="relative flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
          <Navbar actions={navbarActions} copyActionRef={setCopyActionTarget} />
          <ContentArea>{children}</ContentArea>
        </div>
      </div>
    </CopyActionContext.Provider>
  )
}

export default Layout
