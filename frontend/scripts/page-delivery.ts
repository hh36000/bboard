import { readFileSync, writeFileSync, unlinkSync } from 'node:fs'
import { resolve } from 'node:path'

/** A stable entry URL lets backend HTML work with frontend-only asset releases. */
export function assetLoader(html: string): string {
  const script = html.match(/<script[^>]*type="module"[^>]*src="([^"]+)"/)
  if (!script) throw new Error('Build did not emit a module entry')
  const css = Array.from(html.matchAll(/<link[^>]*rel="stylesheet"[^>]*href="([^"]+)"/g), (match) => match[1])
  return `const entry = document.createElement('link');\nentry.rel = 'modulepreload'; entry.href = ${JSON.stringify(script[1])};\ndocument.head.append(entry);\nconst styles = ${JSON.stringify(css)};\nawait Promise.all(styles.map(href => new Promise((resolve, reject) => {\n  const link = document.createElement('link');\n  link.rel = 'stylesheet'; link.href = new URL(href, import.meta.url).href;\n  link.onload = resolve; link.onerror = reject; document.head.append(link);\n})));\nawait import(${JSON.stringify(script[1])});\n`
}

export function prepareHostingOutput(output: string) {
  const path = resolve(output, 'index.html')
  writeFileSync(resolve(output, 'app.js'), assetLoader(readFileSync(path, 'utf8')))
  // Static index.html would take precedence over Hosting's dynamic rewrite.
  unlinkSync(path)
}
