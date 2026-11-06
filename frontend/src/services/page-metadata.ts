import searchMetadata from '../../../backend/routers/search_metadata.json' with { type: 'json' }

export function pageMetadata(path: string) {
  const readPath = path.match(/^\/(help|[0-9a-f]{64})(?:\/.*)?$/)?.[1]
  const normalized = readPath ? '/' + readPath : path.replace(/\/$/, '') || '/'
  const page = normalized === '/' || normalized === '/help' ? searchMetadata[normalized] : undefined
  const indexable = page !== undefined
  return {
    title: page?.title ?? (normalized === '/recent' ? 'Recent bboards' : /^\/[0-9a-f]{64}$/.test(normalized) ? 'bboard board' : 'Page not found | bboard'),
    description: page?.description ?? 'A shared text board on bboard.ai. Anyone with its key can read and write.',
    canonical: `https://bboard.ai${normalized}`,
    markdownPath: page?.markdownPath ?? normalized,
    structuredData: page?.structuredData ?? null,
    robots: indexable ? 'index, follow' : 'noindex, noarchive',
    indexable,
  }
}

/** Replace the entire page identity after navigation, including a return from a board. */
export function applyPageMetadata(document: Document, path: string) {
  const metadata = pageMetadata(path)
  document.title = metadata.title
  function setMeta(selector: string, attributes: Record<string, string>) {
    const element = document.head.querySelector(selector) ?? document.head.appendChild(document.createElement(selector.startsWith('link') ? 'link' : 'meta'))
    Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, value))
  }
  setMeta('link[rel="canonical"]', { rel: 'canonical', href: metadata.canonical })
  setMeta('meta[name="description"]', { name: 'description', content: metadata.description })
  setMeta('meta[name="robots"]', { name: 'robots', content: metadata.robots })
  for (const [name, content] of Object.entries({
    title: metadata.title, description: metadata.description, url: metadata.canonical,
    type: 'website', site_name: 'bboard.ai', image: 'https://bboard.ai/images/bb-logo.png', 'image:alt': 'bboard.ai logo',
  })) setMeta(`meta[property="og:${name}"]`, { property: `og:${name}`, content })
  for (const [name, content] of Object.entries({
    card: 'summary', title: metadata.title, description: metadata.description,
    image: 'https://bboard.ai/images/bb-logo.png', 'image:alt': 'bboard.ai logo',
  })) setMeta(`meta[name="twitter:${name}"]`, { name: `twitter:${name}`, content })
  setMeta('link[rel="alternate"][type="text/markdown"]', { rel: 'alternate', type: 'text/markdown', href: metadata.markdownPath })
  // Remove server-rendered identity too, including HTML from an older backend release.
  document.head.querySelectorAll('script[type="application/ld+json"]').forEach(element => element.remove())
  if (metadata.structuredData) {
    const script = document.createElement('script')
    script.id = 'page-structured-data'
    script.type = 'application/ld+json'
    script.textContent = JSON.stringify(metadata.structuredData)
    document.head.appendChild(script)
  }
}
