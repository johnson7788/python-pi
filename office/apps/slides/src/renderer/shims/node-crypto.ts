/**
 * Browser shim for the two node:crypto entry points the pptx-engine touches.
 * ponytail: `createHash` only feeds PackageArchive.originalHash, which nothing
 * in the engine ever reads back — so a cheap FNV-1a hex stand-in is enough; swap
 * for a real WebCrypto SHA-256 (async) only if dirty-detection ever consumes it.
 * `randomUUID` maps straight to the platform crypto (GUIDs must be real).
 */
export function createHash(_algo: string) {
  let h = 0x811c9dc5
  return {
    update(data: Uint8Array | string) {
      const bytes = typeof data === 'string' ? new TextEncoder().encode(data) : data
      for (let i = 0; i < bytes.length; i++) {
        h ^= bytes[i]!
        h = Math.imul(h, 0x01000193)
      }
      return this
    },
    digest(_enc: string) {
      return (h >>> 0).toString(16).padStart(8, '0')
    },
  }
}

export function randomUUID(): string {
  return crypto.randomUUID()
}
