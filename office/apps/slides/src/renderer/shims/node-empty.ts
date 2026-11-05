/**
 * Empty stand-in for node:fs / node:stream/promises. The only engine code that
 * imports them is savePptxToFile (writes a deck straight to a disk path), which
 * the web build never calls — the adapter saves through the HTTP blob store.
 * ponytail: throw if ever reached rather than silently no-op.
 */
export function createWriteStream(): never {
  throw new Error('node:fs not available in web build')
}
export function pipeline(): never {
  throw new Error('node:stream/promises not available in web build')
}
