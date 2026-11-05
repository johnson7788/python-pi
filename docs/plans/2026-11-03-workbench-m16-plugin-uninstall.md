# 工作台 M16（插件卸载与更新）

> 2026-11-03。插件生命周期补齐卸载：管理员删除插件目录、清理所有租户的启用记录，并让已加载该插件的 worker 重建。

## 已完成

### 后端

- `DELETE /api/plugins/{plugin_id}`：
  - 仅管理员；
  - 插件不存在 404；
  - 删除 `<data_root>/plugins/<id>` 目录；
  - 删除所有租户/应用的 `plugin_enable` 记录；
  - 找出 `options.plugin_paths` 指向该目录的 worker，shutdown 后按需重建；
  - 返回 `{deleted}`。
- 更新插件：上传同 id 的 zip 仍走 M12 的覆盖安装（备份/替换/回滚）。
- 仍保持“同一 id 一个版本”，不做多版本并存。

### 前端

- 已安装插件卡片：管理员显示「卸载」；
- 二次确认，成功后刷新列表；
- 非管理员不显示；
- 插件页底部提示“上传同 id 的 zip 会覆盖更新”。

## 验证

- `npm run typecheck`、`npm run build` 通过；
- `tests/test_plugin_install.py` 扩展：安装 → 卸载 → 目录消失、列表为空、再次卸载 404；
- 非 Docker 全量：**642 passed**；
- Playwright：2 个卸载按钮、确认后剩 1 个插件，无 console 报错。

## 已知边界

- 没有版本列表/回滚；更新=覆盖；
- 卸载会删除所有租户的启用状态，不可恢复；
- 没有软删除/回收站；
- 若插件正在执行 turn，worker 会被 shutdown，当前回合中断；
- 插件代码仍与 server 同进程，只安装可信代码。

## 下一刀

市场排序/分类，或插件的版本列表/更新记录。
