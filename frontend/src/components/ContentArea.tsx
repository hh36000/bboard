import type { ReactNode } from 'react'

function ContentArea({ children }: { children: ReactNode }) {
  return (
    <main className="flex-1 min-h-0 overflow-x-hidden overflow-y-auto overscroll-y-contain [scrollbar-gutter:stable] bg-background">
      <div className="page-content h-full min-w-0 p-0">
        {children}
      </div>
    </main>
  )
}

export default ContentArea
