# pi-office

Embedded DOCX editor for python-pi, forked from AIOffice's docs renderer
(Apache-2.0; see `NOTICE` and `LICENSE`).

## Build

```bash
cd office
npm install
npm run build
# or from the repo root:
./scripts/build-office.sh
```

`./start.sh` 会在首次启动时自动执行这些构建（可用 `PI_SKIP_OFFICE_BUILD=1`
跳过编辑器构建）。

Output goes to `office-dist/` at the repo root. FastAPI serves it at `/office`
(override with `PI_OFFICE_DIST`).

## Embed contract

```
/office/?embed=1&doc=<opaque-id>
```

- `doc` is the base64url envelope produced by the workbench
  (`{workspace, path}`); the server decodes and validates it.
- The editor uses the same-origin session cookie; it does not use AIOffice
  login/Bearer tokens.
- Save sends `office:saved` to the parent window.

## Scope

- `docs/` (DOCX): open, edit, save, lock, versions.
- `slides/` (PPTX): open, edit, save.
- `sheets/` (XLSX): Univer + Rust sidecar; open, edit, save.

Build all three with `npm run build` (writes `office-dist/{,slides,sheets}`).
The XLSX sidecar is built separately: `./scripts/build-xlsx-sidecar.sh`.

Not yet: editor ticket, unsaved-close guard, AI bridge.
