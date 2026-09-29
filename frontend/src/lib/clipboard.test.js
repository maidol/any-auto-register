import { test } from 'node:test'
import assert from 'node:assert/strict'

import { copyTextToClipboard } from './clipboard.js'

function fakeDocument({ copied = true } = {}) {
  const state = { value: '', selected: false, appended: false, removed: false }
  const textarea = {
    value: '',
    style: {},
    setAttribute() {},
    select() { state.selected = true },
  }
  return {
    state,
    document: {
      body: {
        appendChild() { state.appended = true },
        removeChild() { state.removed = true },
      },
      createElement(tag) {
        assert.equal(tag, 'textarea')
        return textarea
      },
      execCommand(command) {
        assert.equal(command, 'copy')
        state.value = textarea.value
        return copied
      },
    },
  }
}

test('uses the Clipboard API when it succeeds', async () => {
  const writes = []
  const result = await copyTextToClipboard('task log', {
    clipboard: { writeText: async text => writes.push(text) },
    document: fakeDocument().document,
  })

  assert.equal(result, true)
  assert.deepEqual(writes, ['task log'])
})

test('falls back to a temporary textarea when Clipboard API is unavailable', async () => {
  const fake = fakeDocument()
  const result = await copyTextToClipboard('task log', {
    clipboard: undefined,
    document: fake.document,
  })

  assert.equal(result, true)
  assert.equal(fake.state.value, 'task log')
  assert.equal(fake.state.selected, true)
  assert.equal(fake.state.appended, true)
  assert.equal(fake.state.removed, true)
})

test('falls back when Clipboard API rejects and reports failure if fallback fails', async () => {
  const fake = fakeDocument({ copied: false })
  const result = await copyTextToClipboard('task log', {
    clipboard: { writeText: async () => { throw new Error('permission denied') } },
    document: fake.document,
  })

  assert.equal(result, false)
  assert.equal(fake.state.value, 'task log')
  assert.equal(fake.state.removed, true)
})
