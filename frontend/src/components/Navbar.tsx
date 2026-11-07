import type { ReactNode, Ref } from 'react'
import { Link } from 'react-router-dom'
import { Github, History, Moon, Sun } from 'lucide-react'
import { useTheme } from '@/contexts/theme-context'
import GenerateKeyButton from '@/components/GenerateKeyButton'

interface NavbarProps {
  copyActionRef: Ref<HTMLSpanElement>
  actions?: ReactNode
}

function Navbar({ copyActionRef, actions }: NavbarProps) {
  const { theme, toggleTheme } = useTheme()

  return (
    <nav className="mx-auto h-16 w-full max-w-5xl shrink-0 flex items-center gap-4 px-6 shadow-xs bg-background">
      <Link
        to="/"
        aria-label="bb home"
        className="block h-12 w-12 shrink-0 rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <span
          aria-hidden="true"
          className="block h-12 w-12 shrink-0 bg-foreground"
          style={{
            maskImage: 'url(/images/bb-logo.png)',
            maskSize: 'contain',
            maskRepeat: 'no-repeat',
            maskPosition: 'center',
            WebkitMaskImage: 'url(/images/bb-logo.png)',
            WebkitMaskSize: 'contain',
            WebkitMaskRepeat: 'no-repeat',
            WebkitMaskPosition: 'center',
          }}
        />
      </Link>

      <div className="navbar-actions ml-auto flex shrink-0 items-center gap-2">
        {actions}
        <Link to="/recent" aria-label="Recent bboards" title="Recent bboards" className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
          <History className="h-4 w-4" aria-hidden="true" />
        </Link>
        <span ref={copyActionRef} className="block h-10 w-10 shrink-0" />
        <GenerateKeyButton />
        <button
          type="button"
          onClick={toggleTheme}
          className="flex h-10 w-10 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          aria-label={`Switch to ${theme === 'light' ? 'dark' : 'light'} mode`}
          title={`Switch to ${theme === 'light' ? 'dark' : 'light'} mode`}
        >
          {theme === 'light' ? <Moon className="h-4 w-4" /> : <Sun className="h-4 w-4" />}
        </button>
        {/* Hidden on phones, where board pages already fill the toolbar. */}
        <a
          href="https://github.com/hh36000/bboard"
          target="_blank"
          rel="noopener noreferrer"
          aria-label="bboard source code on GitHub"
          title="Source code on GitHub"
          className="hidden h-10 w-10 shrink-0 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:flex"
        >
          <Github className="h-4 w-4" aria-hidden="true" />
        </a>
      </div>
    </nav>
  )
}

export default Navbar
