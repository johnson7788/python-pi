# vendor/echarts.min.js

图表产物（`*.html`）依赖它渲染。**不内联进 HTML**：产物面板按 512KB 截断预览，
内联 1.1MB 的 ECharts 会让页面直接白屏；由服务静态提供，浏览器缓存一次即可。

| 项 | 值 |
|---|---|
| 版本 | Apache ECharts **6.1.0**（2025-07 的 v6 线，含和弦图/矩阵坐标系/断轴） |
| 来源 | `https://unpkg.com/echarts@6.1.0/dist/echarts.min.js` |
| 大小 | 1,121,883 B（gzip 约 369KB） |
| sha256 | `b66b25aeb4df84e33199dc21694014d336d222cbd9deb0e5a7c14bd6aa0d0fd0` |
| 许可 | Apache-2.0，版权头保留在文件首部 |

## 怎么用

`plugins/chart-tools` 生成的 HTML 里只有一行：

```html
<script src="/vendor/echarts.min.js"></script>
```

`web/public/` 的产物由 Vite 原样复制到 `web/dist/vendor/`，服务端 SPA 兜底路由会把它当静态文件返回。

## 为什么放 public 而不是 npm 依赖

产物 HTML 是在 **iframe（不透明源）** 里执行的，只有经典 `<script src>` 能可靠加载；
走 `import`/模块需要 CORS 且依赖构建产物哈希。固定路径 + 固定版本，产物 HTML 才能脱离
构建系统单独存在（下载后仍可用 CDN 兜底）。

## 升级

```bash
curl -L "https://unpkg.com/echarts@<版本>/dist/echarts.min.js" -o web/public/vendor/echarts.min.js
shasum -a 256 web/public/vendor/echarts.min.js   # 记到本文件
```

升级后跑一遍 `tests/test_chart_tools.py`，再用一份真实数据目视检查（v6 有过
`tooltip.valueFormatter` 与 `axis.startValue` 的非兼容变更）。
