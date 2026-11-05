# 工作台 M12（插件上传安装）

> 2026-11-03。插件从“管理员拷贝目录”升级为“管理员上传 zip 安装”，校验 manifest 与 zip 路径。

## 已完成

### 后端

- `POST /api/plugins/install`：
  - 仅管理员（`user.is_admin`）；
  - raw zip body，1B–10MB；解压总计 ≤20MB；
  - 拒绝绝对路径、`..`、symlink；
  - 必须恰好包含一个 `plugin.json`（支持 zip 根目录下套一层目录）；
  - 校验 id/version/entry；
  - 安装到 `<data_root>/plugins/<id>`；
  - 已存在则先备份再替换，失败回滚；
  - 新装插件默认停用。
- `GET /api/plugins` 增加 `canInstall`（是否管理员）。

### 前端

- 插件页管理员显示「安装插件」；
- 选择 `.zip` 后直接上传，成功后刷新列表；
- 错误显示在页面顶部；
- 非管理员不显示安装按钮。

## 验证

- `npm run typecheck`、`npm run build` 通过；
- `tests/test_plugin_install.py`：
  - 管理员上传示例插件 → 201，目录落盘，列表可见，默认停用；
  - 非 zip → 422；
  - `../escape.txt` → 422 且不写出插件根目录；
- 非 Docker 全量：**638 passed**；
- Playwright：插件页显示安装按钮，无 console 报错。

## 已知边界

- 仅管理员可安装；没有审核/签名/白名单；
- 一次只支持一个插件包；zip 必须只有一个 plugin.json；
- 同 id 覆盖安装，没有并行多版本；
- 没有安装进度条/取消；
- 没有卸载/回滚 UI（可用文件系统或后续 API）；
- 插件仍与 server 同进程执行，只安装可信代码。

## 下一刀

技能/插件市场（浏览、评分、来源），或应用变更审计与恢复默认。
