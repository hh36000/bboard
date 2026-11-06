import { Link } from 'react-router-dom'
import Layout from '@/components/Layout'

export default function NotFound() {
  return (
    <Layout>
      <div className="p-6 font-mono text-sm leading-6">
        <h1 className="m-0 mb-6 text-inherit">Not found (404)</h1>
        <p>Check the URL.</p>
        <Link to="/help" className="underline underline-offset-2">Help</Link>
      </div>
    </Layout>
  )
}
