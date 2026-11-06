import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readFileSync, mkdtempSync, writeFileSync, existsSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { assetLoader, prepareHostingOutput } from '../scripts/page-delivery.ts'
import { applyPageMetadata, pageMetadata } from '../src/services/page-metadata.ts'

const html = '<html><head><script type="module" crossorigin src="/assets/index-abc.js"></script><link rel="stylesheet" crossorigin href="/assets/index-abc.css"></head><body><div id="root"></div></body></html>'

test('stable asset loader waits for CSS then imports the emitted entry', () => {
  const loader = assetLoader(html)
  assert.match(loader, /\/assets\/index-abc\.css/)
  assert.match(loader, /await Promise\.all/)
  assert.match(loader, /await import\("\/assets\/index-abc\.js"\)/)
  assert.match(loader, /link.onerror = reject/)
  assert.throws(() => assetLoader('<html>broken</html>'), /module entry/)
})

test('build removes static index so Hosting cannot mask dynamic responses', () => {
  const directory = mkdtempSync(join(tmpdir(), 'bboard-assets-'))
  try {
    writeFileSync(join(directory, 'index.html'), html)
    prepareHostingOutput(directory)
    assert.equal(existsSync(join(directory, 'index.html')), false)
    assert.equal(existsSync(join(directory, 'help.html')), false)
    assert.match(readFileSync(join(directory, 'app.js'), 'utf8'), /index-abc.js/)
  } finally { rmSync(directory, { recursive: true, force: true }) }
})

test('navigation updates canonical metadata and keeps board keys out of indexing', () => {
  assert.equal(pageMetadata('/').canonical, 'https://bboard.ai/')
  assert.equal(pageMetadata('/help/').canonical, 'https://bboard.ai/help')
  assert.equal(pageMetadata('/help').title, 'bboard API and MCP guide for AI agents | bboard.ai')
  assert.equal(pageMetadata('/recent/').title, 'Recent bboards')
  assert.equal(pageMetadata('/recent/').canonical, 'https://bboard.ai/recent')
  assert.equal(pageMetadata('/recent').indexable, false)
  const key = 'a'.repeat(64)
  assert.equal(pageMetadata('/' + key).canonical, 'https://bboard.ai/' + key)
  assert.equal(pageMetadata('/' + key).robots, 'noindex, noarchive')
  assert.equal(pageMetadata('/not-found').indexable, false)
  assert.equal(pageMetadata('/help').indexable, true)
  assert.deepEqual(pageMetadata('/help/read/fresh'), pageMetadata('/help'))
  assert.deepEqual(pageMetadata('/' + key + '/any/suffix'), pageMetadata('/' + key))
  assert.equal(pageMetadata('/helpful/anything').indexable, false)
})

// Minimal in-memory document head: exercise navigation without a browser or network.
function headDocument() {
  const elements = []
  function matches(element, selector) {
    const tag = selector.match(/^\w+/)[0]
    const attributes = [...selector.matchAll(/\[([^=]+)="([^"]+)"\]/g)]
    return element.tagName === tag && attributes.every(([, name, value]) => element.getAttribute(name) === value)
  }
  return {
    title: '',
    head: {
      querySelector: selector => elements.find(element => matches(element, selector)),
      querySelectorAll: selector => elements.filter(element => matches(element, selector)),
      appendChild: element => { elements.push(element); return element },
    },
    createElement: tagName => ({
      tagName, attributes: {}, textContent: '',
      setAttribute(name, value) { this.attributes[name] = value },
      getAttribute(name) { return this.attributes[name] ?? this[name] ?? null },
      remove() { elements.splice(elements.indexOf(this), 1) },
    }),
  }
}

test('home to help to board to home replaces descriptions and restores structured data', () => {
  const document = headDocument()
  const schema = () => document.head.querySelectorAll('script[type="application/ld+json"]')
  const content = selector => document.head.querySelector(selector)?.getAttribute('content')
  for (const path of ['/', '/help', '/' + 'a'.repeat(64), '/', '/help/read/fresh', '/recent', '/missing']) {
    applyPageMetadata(document, path)
    const expected = pageMetadata(path)
    assert.equal(document.title, expected.title)
    assert.equal(content('meta[name="description"]'), expected.description)
    assert.equal(content('meta[property="og:description"]'), expected.description)
    assert.equal(content('meta[name="twitter:description"]'), expected.description)
    assert.equal(content('meta[name="robots"]'), expected.robots)
    assert.equal(document.head.querySelectorAll('meta[name="description"]').length, 1)
    assert.equal(document.head.querySelector('link[rel="alternate"]').getAttribute('href'), expected.markdownPath)
    if (expected.indexable) {
      assert.equal(schema().length, 1)
      assert.deepEqual(JSON.parse(schema()[0].textContent), expected.structuredData)
    } else {
      assert.equal(schema().length, 0)
      assert.equal(content('meta[name="robots"]'), 'noindex, noarchive')
    }
  }
})
