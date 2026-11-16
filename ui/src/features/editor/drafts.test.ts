import { expect, test } from 'bun:test'
import { canRestoreDraft, draftKey, type EditorDraft } from './drafts'

const draft: EditorDraft = { key: 'portfolio:/portfolios/1/edit', kind: 'portfolio', path: '/portfolios/1/edit', title: '组合函数',
  code: 'new', baseline: 'old', revision: 1, updatedAt: 1 }

test('only a matching server baseline permits automatic draft recovery', () => {
  expect(canRestoreDraft(draft, 'old')).toBe(true)
  expect(canRestoreDraft(draft, 'changed on server')).toBe(false)
  expect(canRestoreDraft({ ...draft, code: 'old' }, 'old')).toBe(false)
  expect(draftKey('account_notification', '/accounts/1/edit/notification')).not.toBe(draftKey('account_notification', '/accounts/2/edit/notification'))
  expect(draftKey('system_notification', '/')).not.toBe(draftKey('portfolio', '/'))
})
