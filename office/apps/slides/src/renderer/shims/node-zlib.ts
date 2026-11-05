/**
 * Browser shim for node:zlib's deflateSync (engine uses it for PNG IDAT of the
 * generated media poster). pako.deflate produces the same zlib-wrapped stream.
 */
// @ts-expect-error pako ships no types; deflate(Uint8Array)->Uint8Array is all we use
import { deflate } from 'pako'

export function deflateSync(data: Uint8Array): Uint8Array {
  return deflate(data)
}
