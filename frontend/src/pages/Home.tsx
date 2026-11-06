import { Link, useLocation, useParams } from 'react-router-dom'
import NotFound from './NotFound'
import Layout from '@/components/Layout'
import TextEditor from '@/components/TextEditor'
import BoardHistory from '@/components/BoardHistory'
import ReadOnlyMessage from '@/components/ReadOnlyMessage'
import { getInitialMessage, isInitialNotFound } from '@/services/initial-help'
import { getHome } from '@/services/api'
import HelpInfoButton from '@/components/HelpInfoButton'

function Home() {
  const location = useLocation()
  const { boardKey } = useParams()
  const query = new URLSearchParams(location.search)
  const revision = query.has('revision') ? Number(query.get('revision')) : undefined
  const historyOpen = query.get('history') === '1' || revision !== undefined
  if (isInitialNotFound(location.pathname) || (boardKey !== undefined && !/^[0-9a-f]{64}$/.test(boardKey))) return <NotFound />
  if (revision !== undefined && (!Number.isSafeInteger(revision) || revision < 0)) return <NotFound />

  return (
    <Layout navbarActions={boardKey ? (
      <div className="mr-2 flex items-center gap-3 font-mono text-xs" role="navigation" aria-label="Board views">
        <Link to={`/${boardKey}`} aria-current={!historyOpen ? 'page' : undefined}
          className={!historyOpen ? 'text-foreground' : 'text-muted-foreground hover:text-foreground'}>live</Link>
        <Link to={`/${boardKey}?history=1`} aria-current={historyOpen ? 'page' : undefined}
          className={historyOpen ? 'text-foreground' : 'text-muted-foreground hover:text-foreground'}>history</Link>
      </div>
    ) : location.pathname === '/' ? <HelpInfoButton /> : undefined}>
      {boardKey ? (
        historyOpen ? <BoardHistory key={boardKey} boardId={boardKey} revision={revision} />
          : <TextEditor key={boardKey} boardId={boardKey} />
      ) : (
        <ReadOnlyMessage load={getHome} initialMessage={getInitialMessage('/')} label="Welcome message" statusId="home-status" />
      )}
    </Layout>
  )
}

export default Home
