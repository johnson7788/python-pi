/**
 * Browser shim for node:os. The only consumer was the fs-based temp-dir save
 * path (xlsx-package-io), which the web build replaces with the backend /sheets
 * save API — so tmpdir() is never actually reached. Kept as a harmless stub.
 */
export function tmpdir(): string {
  return '/tmp'
}

export default { tmpdir }
