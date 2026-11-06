import { Link } from 'react-router-dom'
import { Info } from 'lucide-react'

export default function HelpInfoButton() {
  return (
    <Link
      to="/help"
      aria-label="Help"
      title="Help"
      className="flex h-10 w-10 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <Info className="h-4 w-4" aria-hidden="true" />
    </Link>
  )
}
