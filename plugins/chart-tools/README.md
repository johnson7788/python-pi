# chart-tools

把工作区的数据画成**可交付的 ECharts 6 HTML**。四个工具，外加 45 个渲染过的模板：

| 工具 | 输入 | 输出 |
|---|---|---|
| `chart_templates` | 可选 id / family / 数据文件 | 45 种图的完整 spec；可按族列出、可校验列名 |
| `chart_data_profile` | 数据文件（CSV/TSV/XLSX/JSON） | 列类型/缺失/基数/范围 + 选型建议 + 可改的 spec 骨架 |
| `chart_check` | spec 路径 | error/warning 列表，不写文件 |
| `chart_render_html` | spec 路径 | `charts/<名字>.html`（交互图 + PNG/CSV 导出 + 页脚溯源） |

## 为什么值得包一层

**图的最大风险不是难看，是"看着对、数错了"。** 只要允许模型写 `series.data`，就会出现
好看的图配错的数，而且没人看得出来。这里的做法是：

- spec 里**禁止** `dataset.source`（写了直接报 error），数据只能从 `bind.path` 来；
- 渲染时由工具把 CSV/XLSX 的行绑进 `dataset.source`，并把文件 sha256、行数、列名写进页脚；
- 手写 `series.data` 数字会被警告——它可能是对的，但无法回溯。

第二个风险是**手写 HTML 等于执行任意代码**：产物面板的 iframe 给了 `allow-scripts`
（不给的话图是白的），所以 HTML 必须来自固定模板——模型只提供 option（纯 JSON），
不能插脚本、不能写 `renderItem`。

第三个是**"图型选错"比"画得丑"更伤**：校验器会拦 custom、未加载 GL 引擎的 3D 系列、
饼图超 6 类、双轴没说明、encode 引用了未绑定的列。

## spec

```jsonc
{
  "schema": "pi.chart/1",
  "page": { "title": "…", "subtitle": "口径", "columns": 1, "theme": "auto" },
  "bind": { "path": "data/sales.csv", "sheet": null, "columns": ["month", "sales"],
            "aggregate": { "by": "region", "op": "sum" } },   // aggregate 可选
  "charts": [{
    "title": "写结论",
    "option": { "dataset": {}, "xAxis": {}, "yAxis": {}, "series": [{ "type": "line", "encode": {} }] },
    "note": "怎么读 / 哪里不能比"
  }],
  "notes": { "unit": "万元", "source": "财务月报", "caveat": "12 月为预估" }
}
```

## 边界

- 支持 `.csv/.tsv/.xlsx/.json`；旧版 `.xls` 请另存（会明确报错）；
- 单文件 ≤20MB、扫描 ≤20 万行、**绑定后 ≤5000 行**；超了要求 `bind.aggregate` 或明确 `bind.limit`；
- 生成的 HTML **≤480KB**（产物面板在 512KB 处截断预览，超了图不渲染）；
- 3D/GL 由 `echarts-gl@2.1.0` 提供：6 个模板 `bar3D/line3D/scatter3D/surface/globe/graphGL`；
  需要浏览器支持 WebGL，无 GPU/离屏环境可能空白。GL 引擎是独立静态资源，不占 480KB HTML 预算；
- 2D 地图提供 `china-map`/`world-map` 两个模板，地图数据是 `web/public/vendor/maps/*.js`
  里的 `echarts.registerMap`，只在使用地图时加载；地区名必须与 GeoJSON 的 `properties.name` 一致；
- 地图数据来自 ECharts 4.9.0（Apache-2.0），**不是正式审图版本**；对外的中国/世界地图需要
  换成有审图号的标准地图数据并做合规审查；
- 不做 custom 系列（`renderItem` 是函数）；
- 导出 PNG 在浏览器里完成（`getDataURL`），没有服务端截图。

## 本地验证

```bash
python3 plugin-kit/pikit.py check plugins/chart-tools
.venv/bin/python -m pytest tests/test_chart_tools.py -q
```

手测：

```bash
python3 plugin-kit/pikit.py dev plugins/chart-tools --tool chart_data_profile \
  --args '{"path":"data/sales.csv"}'
```

## 安装

应用声明了本插件，team 部署下 `./apps/chart/install.sh --team` 装到 `<data-root>/plugins/`
后重启即自动加载（不需要再点「启用」）。其他环境用插件页上传 zip。

产物 HTML 依赖 `web/public/vendor/echarts.min.js`（见该目录 README）；没有它时页面会尝试
从 unpkg 兜底，离线环境会显示一句明确的提示而不是白屏。3D/GL 模板额外依赖
`web/public/vendor/echarts-gl.min.js`（2.1.0，约 625KB），只在 spec 用到 GL 系列时加载；
本地没有时尝试 BootCDN 兜底。地图模板依赖 `web/public/vendor/maps/china.js` 和
`world.js`（ECharts 4.9.0 map data，Apache-2.0），只在 spec 用到地图时加载。
