import { useEffect } from 'react'
import { useLocation } from 'react-router-dom'
import { applyPageMetadata } from '@/services/page-metadata'

export default function PageMetadata() {
  const { pathname } = useLocation()
  useEffect(() => {
    applyPageMetadata(document, pathname)
  }, [pathname])
  return null
}
