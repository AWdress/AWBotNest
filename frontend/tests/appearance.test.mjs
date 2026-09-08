import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'

function setup(initial = {}) {
  const storage = { 'awbotnest-theme': 'transparent', ...initial }
  const images = [], timers = new Map(), style = {}
  let id = 0
  const context = {
    localStorage: { getItem: k => storage[k], setItem: (k, v) => { storage[k] = v } },
    document: { documentElement: { dataset: {}, style: {
      setProperty: (k, v) => { style[k] = v }, getPropertyValue: k => style[k] || '',
    } } },
    Image: function () { images.push(this) },
    setTimeout: fn => { timers.set(++id, fn); return id }, clearTimeout: key => timers.delete(key),
  }
  const source = readFileSync(new URL('../src/composables/appearance.js', import.meta.url), 'utf8').replaceAll('export function', 'function')
  runInNewContext(`${source}\nglobalThis.api = { applyAppearance, disposeAppearance }`, context)
  return { ...context.api, storage, images, timers, style }
}

test('old failed request cannot erase newer successful background', () => {
  const p = setup({ 'awbotnest-bg-image': 'first.jpg' })
  p.applyAppearance(); const oldError = p.images[0].onerror
  p.storage['awbotnest-bg-image'] = 'second.jpg'; p.applyAppearance()
  p.images[1].onload(); oldError()
  assert.equal(p.style['--app-bg-image'], 'url("second.jpg")')
})
test('replacement waits until loaded and failure preserves visible image', () => {
  const p = setup({ 'awbotnest-bg-image': 'first.jpg' })
  p.applyAppearance(); p.images[0].onload()
  p.storage['awbotnest-bg-image'] = 'second.jpg'; p.applyAppearance()
  assert.equal(p.style['--app-bg-image'], 'url("first.jpg")')
  p.images[1].onerror()
  assert.equal(p.style['--app-bg-image'], 'url("first.jpg")')
})
test('startup reuses last successful default address', () => {
  const p = setup({ 'awbotnest-bg-last-success': JSON.stringify({ source: 'default', url: 'cached.jpg' }) })
  p.applyAppearance()
  assert.equal(p.style['--app-bg-image'], 'url("cached.jpg")')
  assert.equal(p.images[0].src, 'cached.jpg')
  p.images[0].onload(); p.applyAppearance()
  assert.equal(p.images.length, 1)
})
test('dark mode invalidates outstanding success callbacks', () => {
  const p = setup(); p.applyAppearance(); const oldLoad = p.images[0].onload
  p.storage['awbotnest-theme'] = 'dark'; p.applyAppearance(); oldLoad()
  assert.equal(p.style['--app-bg-image'], 'none')
  assert.equal(p.timers.size, 0)
})
test('retries are bounded and disposal cancels timers', () => {
  const p = setup(); p.applyAppearance()
  for (let i = 0; i < 3; i++) {
    p.images[i].onerror()
    const entry = [...p.timers.entries()][0]
    if (entry) { p.timers.delete(entry[0]); entry[1]() }
  }
  assert.equal(p.images.length, 3)
  assert.equal(p.timers.size, 0)
  p.applyAppearance(); p.disposeAppearance()
  assert.equal(p.timers.size, 0)
})
