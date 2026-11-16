// Only scopes referenced by current output/checkpoints live here; evicted incarnations never return.
export class RawScopes {
  private entries = new Map<string, { incarnation: number; revision: number; boundary: number; value: string; bytes: number; group: string }>()
  private serial = 0
  private bytes = 0
  private expansions = new Set<string>()
  beginRoot() { this.expansions.clear() }
  clear() { this.entries.clear(); this.bytes = 0 }
  get(key: string, group: string, boundary: number) {
    let entry = this.entries.get(key)
    if (!entry) {
      while (this.entries.size >= 8) this.remove(this.entries.keys().next().value!)
      entry = { incarnation: ++this.serial, revision: 1, boundary: 0, value: '', bytes: 0, group }
    }
    this.entries.delete(key); this.entries.set(key, entry)
    return { ...entry, value: entry.boundary <= boundary ? entry.value : '', boundary: entry.boundary <= boundary ? entry.boundary : 0 }
  }
  private remove(key: string) {
    const entry = this.entries.get(key)
    if (entry) this.bytes -= entry.bytes
    this.entries.delete(key)
  }
  revise(key: string, group: string) {
    if (this.expansions.has(key) || this.expansions.size >= 8) throw Error('REPLAY_DEPENDENCY_PLAN_INVALID')
    this.expansions.add(key)
    for (const [id, entry] of this.entries) {
      // Without an independence proof, consumers in the same source/symbol group rebuild too.
      if (entry.group !== group) continue
      this.bytes -= entry.bytes; entry.bytes = 0; entry.value = ''; entry.boundary = 0; entry.revision++
      this.entries.set(id, entry)
    }
    return this.get(key, group, 0)
  }
  put(key: string, token: { incarnation: number; revision: number }, boundary: number, value: string) {
    const entry = this.entries.get(key)
    if (!entry || entry.incarnation !== token.incarnation || entry.revision !== token.revision) throw Error('REPLAY_SCOPE_RETIRED')
    const bytes = value.length * 2
    if (bytes > 1024 * 1024) return
    this.bytes -= entry.bytes; entry.value = ''; entry.bytes = 0
    while (this.bytes + bytes > 1024 * 1024) {
      const victim = [...this.entries.keys()].find(id => id !== key)
      if (!victim) break
      this.remove(victim)
    }
    entry.value = value; entry.bytes = bytes; entry.boundary = boundary; this.bytes += bytes
  }
  usage() { return { bytes: this.bytes, entries: this.entries.size } }
}
