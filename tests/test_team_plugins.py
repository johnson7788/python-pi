"""Team starter plugins: deterministic behaviour, no network except the mock."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from pi_coding_agent.plugins import workspace_path

ROOT = Path(__file__).resolve().parent.parent


class FakeAPI:
    """The slice of PluginAPI these plugins use: config, cwd, register_tool."""

    def __init__(self, config: dict | None = None, cwd: str = "") -> None:
        self.config = config or {}
        self.cwd = cwd
        self.tools: dict[str, object] = {}

    def register_tool(self, **kwargs):
        def decorator(function):
            self.tools[kwargs["name"]] = function
            return function

        return decorator


def _load(relative: str, name: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _call(api: FakeAPI, tool: str, args: dict) -> str:
    return asyncio.run(api.tools[tool]("test", args, None, lambda _update: None))


def test_workspace_path_refuses_escapes(tmp_path):
    api = FakeAPI(cwd=str(tmp_path))
    assert workspace_path(api, "a/b.csv") == tmp_path / "a" / "b.csv"
    with pytest.raises(ValueError):
        workspace_path(api, "../outside.csv")
    with pytest.raises(ValueError):
        workspace_path(api, "/etc/passwd")


# --------------------------------------------------------------------------------------
# clinical-calc
# --------------------------------------------------------------------------------------


def test_clinical_calc_body_metrics():
    module = _load("plugins/clinical-calc/main.py", "team_clinical")
    api = FakeAPI()
    module.setup(api)
    text = _call(api, "body_metrics", {"weight_kg": 70, "height_cm": 170, "sex": "male"})
    assert "BMI 24.2" in text
    assert "超重" in text
    assert "BSA 1.82" in text
    assert "IBW 65.9" in text


def test_clinical_calc_renal_and_cockcroft():
    module = _load("plugins/clinical-calc/main.py", "team_clinical")
    api = FakeAPI()
    module.setup(api)
    text = _call(
        api,
        "renal_function",
        {"age": 58, "sex": "male", "creatinine_umol_l": 132, "weight_kg": 82},
    )
    assert "eGFR 53.9" in text
    assert "G3a" in text
    assert "Cockcroft-Gault 62.5" in text


def test_clinical_calc_risk_scores():
    module = _load("plugins/clinical-calc/main.py", "team_clinical")
    api = FakeAPI()
    module.setup(api)
    text = _call(
        api,
        "risk_scores",
        {"age": 76, "sex": "female", "hypertension": True, "diabetes": True, "stroke_tia": True},
    )
    assert "CHA₂DS₂-VASc 7 分" in text
    assert "高危" in text
    assert "HAS-BLED 3 分" in text


def test_clinical_calc_liver_scores():
    module = _load("plugins/clinical-calc/main.py", "team_clinical")
    api = FakeAPI()
    module.setup(api)
    text = _call(
        api,
        "liver_scores",
        {
            "bilirubin_umol_l": 40,
            "albumin_g_l": 30,
            "inr": 2.0,
            "ascites": "mild",
            "encephalopathy": "grade1-2",
            "creatinine_umol_l": 100,
            "sodium_mmol_l": 130,
        },
    )
    assert "Child-Pugh 10 分 → C" in text
    assert "MELD 18.6" in text
    assert "MELD-Na 23.5" in text


def test_clinical_calc_reports_bad_params_as_text():
    module = _load("plugins/clinical-calc/main.py", "team_clinical")
    api = FakeAPI()
    module.setup(api)
    assert "参数错误" in _call(api, "body_metrics", {"weight_kg": 70})
    assert "参数错误" in _call(api, "renal_function", {"age": 10, "sex": "male", "creatinine_umol_l": 80})


# --------------------------------------------------------------------------------------
# csv-tools
# --------------------------------------------------------------------------------------


def test_csv_profile_and_query(tmp_path):
    (tmp_path / "data.csv").write_text(
        "name,age,city\n张三,34,北京\n李四,72,上海\n王五,58,北京\n", encoding="utf-8-sig"
    )
    module = _load("plugins/csv-tools/main.py", "team_csv")
    api = FakeAPI(cwd=str(tmp_path))
    module.setup(api)

    profile = _call(api, "csv_profile", {"path": "data.csv"})
    assert "3 行 × 3 列" in profile
    assert "北京(2)" in profile

    matched = _call(api, "csv_query", {"path": "data.csv", "where": "age>=50 and city contains 北京"})
    assert "匹配 1 行" in matched
    assert "王五" in matched and "李四" not in matched

    assert "参数错误" in _call(api, "csv_query", {"path": "../data.csv"})
    assert "参数错误" in _call(api, "csv_query", {"path": "data.csv", "where": "nope=1"})


# --------------------------------------------------------------------------------------
# web-fetch (pure functions; no network)
# --------------------------------------------------------------------------------------


def test_web_fetch_extracts_text_without_scripts():
    module = _load("plugins/web-fetch/main.py", "team_webfetch")
    title, text = module._html_to_text(
        "<html><head><title>标题</title></head><body>"
        "<script>alert(1)</script><style>.x{}</style>"
        "<h1>小节</h1><p>Hello <b>world</b></p></body></html>"
    )
    assert title == "标题"
    assert "# 小节" in text
    assert "Hello world" in text
    assert "alert" not in text and ".x" not in text


def test_web_fetch_blocks_private_and_bad_schemes():
    module = _load("plugins/web-fetch/main.py", "team_webfetch")
    with pytest.raises(ValueError):
        module._validate_url("http://127.0.0.1/")
    with pytest.raises(ValueError):
        module._validate_url("http://localhost/")
    with pytest.raises(ValueError):
        module._validate_url("file:///etc/passwd")
    with pytest.raises(ValueError):
        module._validate_url("http://user:pass@example.com/")


# --------------------------------------------------------------------------------------
# pubmed (local mock of NCBI E-utilities)
# --------------------------------------------------------------------------------------

_SEARCH = {"esearchresult": {"idlist": ["111", "222"]}}
_SUMMARY = {
    "result": {
        "uids": ["111", "222"],
        "111": {
            "title": "Drug X in heart failure",
            "fulljournalname": "Lancet",
            "pubdate": "2024 Jan",
            "authors": [{"name": "Smith JA"}, {"name": "Doe B"}, {"name": "Lee C"}, {"name": "Park D"}],
            "articleids": [{"idtype": "doi", "value": "10.1000/x"}],
        },
        "222": {
            "title": "A second trial",
            "source": "BMJ",
            "pubdate": "2023",
            "authors": [{"name": "Wang L"}],
            "elocationid": "doi: 10.1000/y",
        },
    }
}
_ABSTRACT = """<?xml version="1.0"?>
<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>111</PMID><Article>
<ArticleTitle>Drug <i>X</i> in heart failure</ArticleTitle>
<Abstract>
<AbstractText Label="BACKGROUND">Heart failure is common.</AbstractText>
<AbstractText Label="RESULTS">Risk fell by 42%.</AbstractText>
</Abstract>
<Journal><Title>Lancet</Title><JournalIssue><PubDate><Year>2024</Year></PubDate></JournalIssue></Journal>
<AuthorList><Author><LastName>Smith</LastName><Initials>JA</Initials></Author></AuthorList>
<ELocationID EIdType="doi">10.1000/x</ELocationID>
</Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"""


class _NcbiHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/esearch"):
            body, kind = json.dumps(_SEARCH).encode(), "application/json"
        elif self.path.startswith("/esummary"):
            body, kind = json.dumps(_SUMMARY).encode(), "application/json"
        elif self.path.startswith("/efetch"):
            body, kind = _ABSTRACT.encode(), "application/xml"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence the test server
        return


def test_pubmed_search_and_abstract_against_mock():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NcbiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        module = _load("plugins/pubmed/main.py", "team_pubmed")
        api = FakeAPI(config={"baseUrl": f"http://127.0.0.1:{server.server_port}"})
        module.setup(api)

        async def exercise() -> tuple[str, str]:
            search = await api.tools["pubmed_search"](
                "t", {"query": "SGLT2 heart failure", "retmax": 2}, None, lambda _u: None
            )
            abstract = await api.tools["pubmed_abstract"](
                "t", {"pmid": "111"}, None, lambda _u: None
            )
            return search, abstract

        search, abstract = asyncio.run(exercise())
        assert "PMID 111 · Lancet 2024" in search
        assert "Smith JA, Doe B, Lee C, et al." in search
        assert "10.1000/x" in search
        assert "10.1000/y" in search
        assert "BACKGROUND: Heart failure is common." in abstract
        assert "RESULTS: Risk fell by 42%." in abstract
        assert "Drug X in heart failure" in abstract
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------------------
# market install → loader (workspace plumbing + clean copy)
# --------------------------------------------------------------------------------------


def test_market_install_copies_starter_plugin_without_pycache(tmp_path):
    from fastapi.testclient import TestClient

    from pi_coding_agent.bootstrap import BootstrapOptions
    from pi_coding_agent.extensions import load_extensions
    from pi_server.app import create_app
    from pi_server.auth import ensure_admin
    from pi_server.config import ServerConfig

    config = ServerConfig(data_root=str(tmp_path), auth_mode="team", admin_password="pw123456")
    app = create_app(
        BootstrapOptions(cwd=str(tmp_path), agent_dir=str(tmp_path / "agent")), config=config
    )
    ensure_admin(app.state.db, "admin", "pw123456")
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "pw123456"})
        response = client.post("/api/market/plugins/clinical-calc/install")
        assert response.status_code == 200, response.text

    plugin_dir = tmp_path / "plugins" / "clinical-calc"
    assert (plugin_dir / "main.py").is_file()
    assert not (plugin_dir / "__pycache__").exists()

    result = load_extensions(
        [str(plugin_dir / "main.py")], discover_entry_points=False, cwd=str(tmp_path)
    )
    assert result.diagnostics == []
    names = {tool.name for api in result.apis for tool in api.tools.values()}
    assert {"body_metrics", "renal_function", "risk_scores", "liver_scores"} <= names
