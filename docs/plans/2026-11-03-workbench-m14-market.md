# 工作台 M14（技能/插件市场）

> 2026-11-03。技能页和插件页新增「市场」标签：浏览来源、评分、安装状态，并可直接安装。

## 已完成

### 后端

- **市场目录** `market.py`：内置 4 个技能、1 个插件示例；
  - 条目字段：kind/id/name/description/version/author/source/tags；
  - 技能带 SKILL.md 内容，插件带本地 package 路径；
  - 来源分为「团队 / 社区 / 官方示例」。
- **DB v11**：`market_ratings(uid, kind, item_id, score)`，按用户记录 1–5 星。
- API：
  - `GET /api/market?kind=skill|plugin`：条目 + 聚合评分 + 我的评分 + 安装状态 + `canInstall`；
  - `POST /api/market/{kind}/{id}/rating`：评分，返回平均分/人数/我的分数；
  - `POST /api/market/skills/{id}/install`：把 SKILL.md 写入用户 agent skills，evict worker；
  - `POST /api/market/plugins/{id}/install`：管理员把目录复制到插件根，默认停用。
- 安装技能后，已安装列表可见；重复安装 409。

### 前端

- 技能页 / 插件页增加「已安装 / 市场」标签；
- 市场卡片显示来源、作者、标签、评分星、安装状态；
- 点击星星评分，即时更新平均分/人数；
- 安装按钮：已安装禁用；插件市场仅管理员可安装；
- 插件页仍保留 zip 上传安装。

## 验证

- `npm run typecheck`、`npm run build` 通过；
- `tests/test_market.py`：列表、评分聚合/我的评分、技能安装落盘/重复 409、插件安装复制/重复 409；
- 非 Docker 全量：**641 passed**；
- Playwright：技能市场 3 条、评分后 5.0（13）、安装后“已安装”、插件市场 1 条，无 console 报错。

## 已知边界

- 市场目录是部署内置代码，不是远程 registry；
- 没有搜索/分类/排序、分页、截图、README 详情页；
- 评分没有评论、审核、防刷；
- 没有卸载/更新/版本选择；
- 插件市场安装仅管理员，且依赖包已随部署提供；
- 技能安装写用户 agent dir，会影响该用户所有空间。

## 下一刀

市场搜索/详情页，或插件的卸载/更新/版本选择。
