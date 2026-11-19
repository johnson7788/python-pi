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

## vendor/echarts-gl.min.js

3D/GL 图（`bar3D`、`line3D`、`scatter3D`、`surface`、`globe`、`graphGL`）才加载它；
2D 图不加载，避免每次多下载 625KB。

| 项 | 值 |
|---|---|
| 版本 | Apache ECharts GL **2.1.0**（peer: ECharts `^5.1.2 || ^6.0.0`） |
| 来源 | `https://registry.npmmirror.com/echarts-gl/2.1.0/files/dist/echarts-gl.min.js` |
| 大小 | 639,846 B（gzip 后约 155KB） |
| sha256 | `d6d60f2165ce3eb1ea360ef62e8490835e0ae2c1e0c1788a772fff918e350842` |
| 许可 | Apache-2.0，版权头保留在文件首部 |

生成 HTML 里的加载顺序固定为 `echarts.min.js` → `echarts-gl.min.js` → 图表代码；本地文件
不存在时回退到 BootCDN 的 2.1.0（带 SRI）。

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

## vendor/maps/

2D 地图产物才加载这两个文件；内容是 `echarts.registerMap('china'|'world', …)`，
不是 JSON fetch（产物 iframe 的 CSP `connect-src 'none'`，且 map 注册要先于图表初始化）。

| 文件 | 大小 | sha256 |
|---|---|---|
| `china.js` | 42,756 B | `dd16461219e9e55336edbe7d10b168a520cd06767bf1d157698e27e38e9e9eca` |
| `world.js` | 1,010,363 B | `2d2d90e60902a1032b408352dee7005f3e22779a305a68d748103094f16a2344` |

来源：`echarts@4.9.0` npm 包 `map/json/china.json` / `world.json`，Apache-2.0。
这是**内部测试地图数据，不是正式审图版本**；对外发布前必须换用有审图号的标准地图数据，
并检查台湾、南海诸岛、十段线及世界地图的中国边界表达。
