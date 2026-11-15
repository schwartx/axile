import { expect, test } from 'bun:test'
import { RawScopes } from './rawScopes'

test('scope revisions retire related checkpoints, preserve independent groups, and never reuse incarnations', () => {
  const scopes = new RawScopes(), a = scopes.get('a','S',10), b = scopes.get('b','T',10)
  scopes.put('a',a,10,'old'); scopes.put('b',b,10,'independent')
  expect(scopes.get('a','S',9).value).toBe('')
  expect(scopes.get('a','S',10).value).toBe('old')
  const revised = scopes.revise('a','S')
  expect(() => scopes.put('a',a,11,'late')).toThrow('REPLAY_SCOPE_RETIRED')
  expect(scopes.get('b','T',11).value).toBe('independent')
  expect(() => scopes.revise('a','S')).toThrow('REPLAY_DEPENDENCY_PLAN_INVALID')
  scopes.beginRoot(); scopes.put('a',revised,12,'fresh')
  for (let i=0;i<12;i++) { const key=String(i), token=scopes.get(key,'T',1); scopes.put(key,token,1,'x'.repeat(160_000)) }
  expect(scopes.usage().entries).toBeLessThanOrEqual(8)
  expect(scopes.usage().bytes).toBeLessThanOrEqual(1024*1024)
  expect(scopes.get('a','S',12).incarnation).toBeGreaterThan(a.incarnation)
})
