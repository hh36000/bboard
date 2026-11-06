import { fileURLToPath, URL } from 'node:url'
import { resolve } from 'node:path'
import { pipeline } from 'node:stream/promises'
import { defineConfig } from 'vite'
import type { Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { prepareHostingOutput } from './scripts/page-delivery'

function readablePages(): Plugin {
  let output = ''
  let building = false
  return {
    name: 'readable-pages',
    configResolved(config) {
      output = resolve(config.root, config.build.outDir)
      building = config.command === 'build'
    },
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        const path = (req.url ?? '/').split('?')[0]
        if (!['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE'].includes(req.method ?? '') ||
            /^\/(?:@|src\/|node_modules\/|assets\/|images\/)/.test(path)) return next()
        const controller = new AbortController()
        const disconnect = () => { if (!res.writableEnded) controller.abort() }
        res.once('close', disconnect)
        try {
          const headers: Record<string, string> = {
            Accept: req.headers.accept ?? '*/*', 'X-FH-Requested-Host': 'bboard.ai',
          }
          for (const name of ['purpose', 'sec-purpose', 'x-purpose', 'x-moz', 'sec-fetch-dest', 'api-version', 'content-type', 'idempotency-key',
            'mcp-method', 'mcp-name', 'mcp-protocol-version', 'mcp-session-id', 'last-event-id', 'origin']) {
            const value = req.headers[name]
            if (typeof value === 'string') headers[name] = value
          }
          let payload: Buffer | undefined
          if (['POST', 'PUT', 'PATCH'].includes(req.method ?? '')) {
            const chunks: Buffer[] = []
            let size = 0
            for await (const chunk of req) {
              const bytes = Buffer.from(chunk)
              size += bytes.length
              if (size > 65536) { res.statusCode = 413; res.end('Request body exceeds 64 KiB.'); return }
              chunks.push(bytes)
            }
            payload = Buffer.concat(chunks)
          }
          const upstream = await fetch(`http://127.0.0.1:9900${req.url ?? '/'}`, {
            method: req.method, redirect: 'manual', signal: path === '/mcp' && req.method === 'GET'
              ? controller.signal : AbortSignal.any([controller.signal, AbortSignal.timeout(35000)]),
            headers, body: payload,
          })
          if (upstream.headers.get('content-type')?.includes('text/event-stream') && upstream.body) {
            res.statusCode = upstream.status
            upstream.headers.forEach((value, name) => {
              if (!['content-length', 'content-encoding', 'connection', 'transfer-encoding'].includes(name)) res.setHeader(name, value)
            })
            res.flushHeaders()
            const reader = upstream.body.getReader()
            await pipeline(async function* () {
              try {
                while (true) {
                  const chunk = await reader.read()
                  if (chunk.done) break
                  yield chunk.value
                }
              } finally { reader.releaseLock() }
            }, res, { signal: controller.signal })
            return
          }
          let body = await upstream.text()
          if (upstream.headers.get('content-type')?.includes('text/html')) {
            body = await server.transformIndexHtml(req.url ?? '/', body.replace('src="/app.js"', 'src="/src/main.tsx"'))
          }
          res.statusCode = upstream.status
          upstream.headers.forEach((value, name) => {
            if (!['content-length', 'content-encoding', 'connection', 'transfer-encoding'].includes(name)) res.setHeader(name, value)
          })
          res.end(req.method === 'HEAD' ? undefined : body)
        } catch {
          if (res.destroyed) return
          res.statusCode = 503
          res.setHeader('Content-Type', 'text/plain; charset=utf-8')
          res.setHeader('Cache-Control', 'no-store')
          res.end('Page server unavailable. Start the backend on port 9900.\n')
        } finally {
          res.off('close', disconnect)
        }
      })
    },
    closeBundle() {
      if (building) prepareHostingOutput(output)
    },
  }
}

export default defineConfig({
  plugins: [react(), tailwindcss(), readablePages()],
  server: {
    host: '127.0.0.1', port: 6600, strictPort: true,
    allowedHosts: ['.app.github.dev'],  // GitHub Codespaces' forwarded address
    proxy: { '/ws/boards': { target: 'ws://127.0.0.1:9900', ws: true } },
  },
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
})
