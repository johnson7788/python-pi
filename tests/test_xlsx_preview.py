"""Read-only XLSX preview with the standard library."""

from __future__ import annotations

import io
import zipfile

from fastapi.testclient import TestClient
from pi_coding_agent.bootstrap import BootstrapOptions
from pi_server.app import create_app
from pi_server.auth import ensure_admin
from pi_server.config import ServerConfig


def _xlsx_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            """<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
                        xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
                 <sheets><sheet name="证据表" sheetId="1" r:id="rId1"/></sheets>
               </workbook>""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                 <Relationship Id="rId1" Target="worksheets/sheet1.xml"/>
               </Relationships>""",
        )
        archive.writestr(
            "xl/sharedStrings.xml",
            """<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
                 <si><t>研究</t></si>
               </sst>""",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            """<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
                 <sheetData>
                   <row r="1">
                     <c r="A1" t="s"><v>0</v></c>
                     <c r="B1" t="inlineStr"><is><t>样本</t></is></c>
                   </row>
                   <row r="2"><c r="A2"><v>42</v></c></row>
                 </sheetData>
               </worksheet>""",
        )
    return buffer.getvalue()


def test_xlsx_preview_reads_shared_and_inline_strings(tmp_path):
    config = ServerConfig(data_root=str(tmp_path), auth_mode="team", admin_password="pw123456")
    app = create_app(BootstrapOptions(cwd=str(tmp_path), agent_dir=str(tmp_path / "agent")), config=config)
    ensure_admin(app.state.db, "admin", "pw123456")

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "pw123456"})
        client.put(
            "/api/workspace/file?path=/evidence.xlsx",
            content=_xlsx_bytes(),
            headers={"content-type": "application/octet-stream"},
        )
        payload = client.get("/api/artifacts/xlsx?path=/evidence.xlsx").json()
        assert payload["sheets"][0]["name"] == "证据表"
        assert payload["sheets"][0]["rows"][0] == ["研究", "样本"]
        assert payload["sheets"][0]["rows"][1] == ["42"]

        client.put(
            "/api/workspace/file?path=/bad.xlsx",
            content=b"not a zip",
            headers={"content-type": "application/octet-stream"},
        )
        assert client.get("/api/artifacts/xlsx?path=/bad.xlsx").status_code == 422
