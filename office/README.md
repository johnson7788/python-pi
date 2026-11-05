# pi-office

Embedded DOCX editor for python-pi, forked from AIOffice's docs renderer
(Apache-2.0; see `NOTICE` and `LICENSE`).

## Build

```bash
cd office
npm install
npm run build
```

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

P0/P1 DOCX: open, edit, save, lock, version list/restore.

Not yet: xlsx/pptx, editor ticket, unsaved-close guard, AI bridge.
