# Office P0 (DOCX)

- Build: `cd office && npm install && npm run build` (outputs `office-dist/`)
- Serve: FastAPI mounts `office-dist` at `/office`
- Workbench: `.docx` artifact → 在线编辑 → iframe `/office/?embed=1&doc=<opaque-id>`
- API: `/api/office/documents/{id}` and `/blob` (GET/PUT, If-Match)
- P0 scope: open/edit/save a real `.docx`; no xlsx/pptx yet; no lock/version UI yet.

![DOCX editor](p0-word-editor.png)
