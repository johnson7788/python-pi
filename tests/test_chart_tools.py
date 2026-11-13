"""chart-tools：数据画像、spec 校验、HTML 渲染。

这些测试盯住的是「数字不许编」这条线：spec 里写数据集要报错、没绑数据要报错、
超出预览上限先报错而不是生成一个白屏页面。
"""

from __future__ import annotations

import asyncio
import json
import re
import zipfile
from pathlib import Path

import pytest

from pi_coding_agent.extensions import load_extensions

PLUGIN = Path(__file__).resolve().parent.parent / "plugins" / "chart-tools" / "main.py"


def _tools(tmp_path: Path):
    result = load_extensions([str(PLUGIN)], discover_entry_points=False, cwd=str(tmp_path))
    assert result.diagnostics == [], result.diagnostics
    api = result.apis[0]
    return api


def _call(api, name: str, args: dict) -> str:
    tool = api.tools[name]
    result = asyncio.run(tool.execute("call-1", args, None, None))
    return "\n".join(getattr(block, "text", "") for block in result.content)


def _write_csv(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    (data / "sales.csv").write_text(
        "month,sales,margin\n"
        "1月,120,0.32\n"
        "2月,180,0.28\n"
        "3月,150,0.35\n",
        encoding="utf-8",
    )
    return data / "sales.csv"


def _write_xlsx(path: Path) -> None:
    """写一个最小可读的 xlsx：一张表、inlineStr，不依赖 openpyxl。"""
    sheet = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
        '<row r="1"><c r="A1" t="inlineStr"><is><t>region</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>amount</t></is></c></row>'
        '<row r="2"><c r="A2" t="inlineStr"><is><t>华东</t></is></c><c r="B2"><v>320</v></c></row>'
        '<row r="3"><c r="A3" t="inlineStr"><is><t>华北</t></is></c><c r="B3"><v>180</v></c></row>'
        "</sheetData></worksheet>"
    )
    workbook = (
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="销量" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    rels = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)


def _spec(**overrides) -> dict:
    spec = {
        "schema": "pi.chart/1",
        "page": {"title": "月度销售额", "subtitle": "口径：含税", "columns": 1, "theme": "auto"},
        "bind": {"path": "data/sales.csv", "columns": ["month", "sales"]},
        "charts": [
            {
                "title": "3 月回落但未转负",
                "option": {
                    "dataset": {},
                    "xAxis": {"type": "category", "name": "月份"},
                    "yAxis": {"type": "value", "name": "销售额（万元）"},
                    "series": [{"type": "bar", "encode": {"x": "month", "y": "sales"}}],
                },
                "note": "2 月含春节",
            }
        ],
        "notes": {"unit": "万元", "source": "财务月报", "caveat": "无"},
    }
    spec.update(overrides)
    return spec


def test_profile_reports_types_and_a_usable_skeleton(tmp_path):
    _write_csv(tmp_path)
    api = _tools(tmp_path)
    text = _call(api, "chart_data_profile", {"path": "data/sales.csv"})
    assert "数据画像" in text
    assert "sales" in text and "number" in text
    skeleton = text[text.index("{\n  \"schema\"") :]
    parsed = json.loads(skeleton)
    assert parsed["schema"] == "pi.chart/1"
    assert parsed["bind"]["path"] == "data/sales.csv"


def test_profile_reads_xlsx_without_extra_dependencies(tmp_path):
    path = tmp_path / "销量.xlsx"
    _write_xlsx(path)
    api = _tools(tmp_path)
    text = _call(api, "chart_data_profile", {"path": "销量.xlsx"})
    assert "XLSX" in text
    assert "region" in text and "amount" in text
    assert "华东" in text


def test_check_rejects_hardcoded_data_and_unavailable_series(tmp_path):
    _write_csv(tmp_path)
    api = _tools(tmp_path)
    spec = _spec()
    spec["charts"][0]["option"]["dataset"] = {"source": [["month", "sales"], ["1月", 120]]}
    specs = tmp_path / "charts"
    specs.mkdir()
    (specs / "a.spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    text = _call(api, "chart_check", {"path": "charts/a.spec.json"})
    assert "dataset.source" in text and "错误" in text

    spec = _spec()
    spec["charts"][0]["option"]["series"] = [{"type": "bar3D", "encode": {"x": "month", "y": "sales"}}]
    (specs / "b.spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    text = _call(api, "chart_check", {"path": "charts/b.spec.json"})
    assert "echarts-gl" in text

    spec = _spec()
    spec["charts"][0]["option"]["series"] = [{"type": "custom", "renderItem": "x"}]
    (specs / "c.spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    text = _call(api, "chart_check", {"path": "charts/c.spec.json"})
    assert "renderItem" in text


def test_check_rejects_encode_columns_outside_bind(tmp_path):
    _write_csv(tmp_path)
    api = _tools(tmp_path)
    spec = _spec()
    spec["charts"][0]["option"]["series"] = [{"type": "bar", "encode": {"x": "month", "y": "profit"}}]
    specs = tmp_path / "charts"
    specs.mkdir()
    (specs / "a.spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    text = _call(api, "chart_check", {"path": "charts/a.spec.json"})
    assert "profit" in text and "bind.columns" in text


def test_render_writes_html_with_bound_data_and_lineage(tmp_path):
    _write_csv(tmp_path)
    api = _tools(tmp_path)
    specs = tmp_path / "charts"
    specs.mkdir()
    (specs / "sales.spec.json").write_text(json.dumps(_spec(), ensure_ascii=False), encoding="utf-8")

    text = _call(api, "chart_render_html", {"path": "charts/sales.spec.json"})
    assert "charts/sales.html" in text
    html = (specs / "sales.html").read_text(encoding="utf-8")
    assert "/vendor/echarts.min.js" in html
    assert "data/sales.csv" in html
    assert "sha256" in html
    assert '"sales"' in html

    # 数据来自文件，不是模型：3 行数据 + 表头
    spec_blob = html.split('id="pi-chart" type="application/json">', 1)[1].split("</script>", 1)[0]
    payload = json.loads(spec_blob.replace("<\\/", "</"))
    assert payload["__data"] == [["month", "sales"], ["1月", 120], ["2月", 180], ["3月", 150]]
    assert payload["__meta"]["rows"] == 3


def test_render_aggregates_instead_of_truncating(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "raw.csv").write_text(
        "region,amount\n华东,10\n华东,20\n华北,5\n", encoding="utf-8"
    )
    api = _tools(tmp_path)
    spec = _spec(bind={"path": "data/raw.csv", "columns": ["region", "amount"],
                       "aggregate": {"by": "region", "op": "sum"}})
    spec["charts"][0]["option"]["series"] = [{"type": "bar", "encode": {"x": "region", "y": "amount"}}]
    specs = tmp_path / "charts"
    specs.mkdir()
    (specs / "r.spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")

    text = _call(api, "chart_render_html", {"path": "charts/r.spec.json"})
    assert "聚合" in text
    html = (specs / "r.html").read_text(encoding="utf-8")
    spec_blob = html.split('id="pi-chart" type="application/json">', 1)[1].split("</script>", 1)[0]
    payload = json.loads(spec_blob.replace("<\\/", "</"))
    assert payload["__data"] == [["region", "amount"], ["华东", 30], ["华北", 5]]


def test_render_refuses_pages_that_cannot_be_previewed(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    rows = "\n".join(f"row{i},{i}" for i in range(6000))
    (data / "big.csv").write_text("name,value\n" + rows + "\n", encoding="utf-8")
    api = _tools(tmp_path)
    spec = _spec(bind={"path": "data/big.csv", "columns": ["name", "value"]})
    spec["charts"][0]["option"]["series"] = [{"type": "bar", "encode": {"x": "name", "y": "value"}}]
    specs = tmp_path / "charts"
    specs.mkdir()
    (specs / "big.spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")

    text = _call(api, "chart_render_html", {"path": "charts/big.spec.json"})
    assert "超过上限" in text
    assert not (specs / "big.html").exists()


def test_paths_cannot_escape_the_workspace(tmp_path):
    api = _tools(tmp_path)
    text = _call(api, "chart_data_profile", {"path": "../../etc/passwd"})
    assert "参数错误" in text


@pytest.mark.parametrize("missing", ["bind", "charts"])
def test_check_requires_the_basics(tmp_path, missing):
    _write_csv(tmp_path)
    api = _tools(tmp_path)
    spec = _spec()
    spec.pop(missing)
    specs = tmp_path / "charts"
    specs.mkdir()
    (specs / "a.spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    text = _call(api, "chart_check", {"path": "charts/a.spec.json"})
    assert "错误" in text


ROOT = Path(__file__).resolve().parent.parent


def test_html_preview_runs_scripts_without_granting_same_origin():
    """产物 iframe 的沙箱策略是产品约束，不是实现细节。

    只给 ``allow-scripts`` 时文档落在不透明源上：图能画，但读不到父页面。
    一旦有人为了"图不显示"把 ``allow-same-origin`` 加回来，srcdoc 会继承工作台的源，
    等于把整个 UI 交给任意 HTML——包括模型生成的页面。
    """
    source = (ROOT / "web/src/workbench/Workbench.tsx").read_text(encoding="utf-8")
    match = re.search(
        r'className="wb-html-preview"[\s\S]{0,600}?sandbox="([^"]+)"',
        source,
    )
    assert match, "找不到 HTML 预览的 iframe"
    flags = set(match.group(1).split())
    assert "allow-scripts" in flags, "没有 allow-scripts，ECharts 不会执行，图是白的"
    assert "allow-same-origin" not in flags, "allow-scripts + allow-same-origin 会击穿隔离"


def test_chart_engine_is_vendored_where_the_html_expects_it():
    """产物 HTML 只引用 ``/vendor/echarts.min.js``；这个文件没了，所有图都白。"""
    vendor = ROOT / "web/public/vendor/echarts.min.js"
    assert vendor.is_file()
    text = vendor.read_text(encoding="utf-8", errors="replace")
    assert "echarts" in text.lower()
    assert "6.1.0" in text, "版本要和 web/public/vendor/README.md 对齐"
    assert vendor.stat().st_size > 500_000


# -- 模板库 ---------------------------------------------------------------------------

def _plugin_module():
    """直接加载插件模块，用来测模板库这类不需要 runtime 的纯函数。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("chart_tools_under_test", PLUGIN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _encode_columns(option: dict) -> list[str]:
    out: list[str] = []
    series = option.get("series")
    if not isinstance(series, list):
        return out
    for item in series:
        if not isinstance(item, dict):
            continue
        encode = item.get("encode")
        if not isinstance(encode, dict):
            continue
        for value in encode.values():
            for name in value if isinstance(value, list) else [value]:
                if isinstance(name, str):
                    out.append(name)
    return out


def test_template_library_is_well_formed_and_large():
    module = _plugin_module()
    templates = module.load_templates()
    assert len(templates) >= 30, f"模板库只有 {len(templates)} 个，目标是 30+"
    ids = [item["id"] for item in templates]
    assert len(ids) == len(set(ids))
    for item in templates:
        assert item.get("schema") == "pi.chart-template/1", item["id"]
        assert item.get("name") and item.get("use") and item.get("avoid"), item["id"]
        assert item.get("family") in module.FAMILIES, item["id"]
        assert isinstance(item.get("option"), dict), item["id"]
        assert item.get("pitfalls"), f"{item['id']} 没有写坑"
        bind = item.get("bind")
        if isinstance(bind, dict):
            columns = bind.get("columns") or []
            assert columns, item["id"]
            sample = item.get("sample") or {}
            assert sample.get("columns") == columns, f"{item['id']} 的 sample 列和 bind 不一致"
            assert all(len(row) == len(columns) for row in sample.get("rows") or []), item["id"]
            for name in _encode_columns(item["option"]):
                assert name in columns, f"{item['id']} 的 encode 引用了未绑定列 {name}"


def test_every_template_renders_with_its_sample(tmp_path, ):
    """模板是资产不是文档：每个都要真的能渲染出 HTML，且不超过预览上限。"""
    module = _plugin_module()
    templates = module.load_templates()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    charts = tmp_path / "charts"
    charts.mkdir()

    rendered = 0
    for item in templates:
        bind = item.get("bind")
        if isinstance(bind, dict):
            sample = item["sample"]
            rows = "\n".join(",".join(str(cell) for cell in row) for row in sample["rows"])
            (data_dir / f"{item['id']}.csv").write_text(
                ",".join(sample["columns"]) + "\n" + rows + "\n", encoding="utf-8"
            )
            spec = module.template_spec(item, data_path=f"data/{item['id']}.csv")
        else:
            spec = module.template_spec(item)

        errors, _warnings = module.check_spec(spec, base_dir=charts, api=_FakeApi(tmp_path))
        assert errors == [], f"{item['id']}: {errors}"

        bound = module.bind_data(spec, api=_FakeApi(tmp_path))
        html = module.render_html(spec, bound=bound)
        size = len(html.encode("utf-8"))
        assert size < module.MAX_HTML_BYTES, f"{item['id']} 渲染出 {size} 字节，超过预览上限"
        assert "/vendor/echarts.min.js" in html
        assert "__data" in html
        rendered += 1

    assert rendered == len(templates)


class _FakeApi:
    """只提供 workspace_path 需要的 cwd。"""

    def __init__(self, root: Path) -> None:
        self.cwd = str(root)
        self.config: dict = {}


def test_chart_templates_tool_lists_and_expands(tmp_path):
    api = _tools(tmp_path)
    listed = _call(api, "chart_templates", {})
    assert "可用模板" in listed and listed.count("- ") >= 30

    detail = _call(api, "chart_templates", {"id": "waterfall"})
    assert "瀑布图" in detail and "bind" in detail
    payload = json.loads(detail[detail.index("{") :])
    assert payload["schema"] == "pi.chart/1"
    assert payload["bind"]["columns"] == ["step", "base", "delta"]

    family = _call(api, "chart_templates", {"family": "distribution"})
    assert "直方图" in family and "箱线图" in family
