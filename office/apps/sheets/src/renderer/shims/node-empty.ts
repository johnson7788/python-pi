/**
 * Empty stand-in for node:fs/promises. The fs helpers are only reached by the
 * legacy whole-file save path in xlsx-gateway (writeFile/rename/readFile) and
 * the temp-dir dance in xlsx-package-io — neither runs on web: the adapter saves
 * through the backend /sheets API (sidecar reassembles the zip on the server).
 * ponytail: throw if ever reached rather than silently corrupt a workbook.
 */
function unavailable(): never {
  throw new Error('node:fs/promises not available in web build (save goes through /sheets API)')
}

export const open = unavailable
export const readFile = unavailable
export const writeFile = unavailable
export const rename = unavailable
export const rm = unavailable
export const mkdir = unavailable
export const mkdtemp = unavailable

export default { open, readFile, writeFile, rename, rm, mkdir, mkdtemp }
