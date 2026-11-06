import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readTheme, saveTheme } from '../src/services/theme-storage.ts'

function setStorage(t, descriptor) {
  const original = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, ...descriptor })
  t.after(() => {
    if (original) Object.defineProperty(globalThis, 'localStorage', original)
    else delete globalThis.localStorage
  })
}

test('theme reads and writes preserve the preference, with dark as the default', (t) => {
  const values = new Map()
  setStorage(t, { value: { getItem: (key) => values.get(key), setItem: (key, value) => values.set(key, value) } })
  assert.equal(readTheme(), 'dark')
  saveTheme('light')
  assert.equal(readTheme(), 'light')
  saveTheme('dark')
  assert.equal(readTheme(), 'dark')
  values.set('theme', 'invalid')
  assert.equal(readTheme(), 'dark')
})

test('denied storage access cannot prevent theme initialization or toggling', (t) => {
  setStorage(t, { get() { throw new DOMException('Storage denied', 'SecurityError') } })
  assert.equal(readTheme(), 'dark')
  assert.doesNotThrow(() => saveTheme('light'))
})

test('full storage cannot prevent a theme change', (t) => {
  setStorage(t, { value: {
    getItem: () => 'light',
    setItem: () => { throw new DOMException('Storage full', 'QuotaExceededError') },
  } })
  assert.equal(readTheme(), 'light')
  assert.doesNotThrow(() => saveTheme('dark'))
})
