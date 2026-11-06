import Layout from '@/components/Layout'
import ReadOnlyMessage from '@/components/ReadOnlyMessage'
import { getHelp } from '@/services/api'
import { getInitialHelp } from '@/services/initial-help'

export default function Help() {
  return (
    <Layout>
      <ReadOnlyMessage load={getHelp} label="Help instructions" statusId="help-status" initialMessage={getInitialHelp()} />
    </Layout>
  )
}
