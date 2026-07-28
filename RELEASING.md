# 发布流程

本项目处于 Alpha 阶段。发布必须从脱敏、可验证的源码状态生成，不能直接复制整个本地工作目录。

## 1. 发布前

1. 确认版本在 `pyproject.toml`、`bili_comments/__init__.py` 和 CLI 输出中一致。
2. 更新 `CHANGELOG.md`，将目标版本从“未发布”改为发布日期。
3. 运行私有完整回归和公开脱敏测试，不把测试输出写入发布内容。
4. 运行编译、CLI 和打包检查。
5. 检查 Git 作者与提交者信息，只使用项目身份或 GitHub noreply 地址。
6. 扫描源码和完整 Git 历史，确认没有 Cookie、真实数据、个人信息或本机路径。

## 2. 允许发布的内容

源码仓库可以包含核心代码、社区文件和脱敏 tests。发行包只允许包含：

- `bili_comments/` 核心代码和 `SKILL.md`
- `README.md`、`README_CN.md`
- `pyproject.toml`、`MANIFEST.in`
- `LICENSE`
- 构建后端生成的固定 wheel/sdist 包元数据

wheel 和 sdist 必须排除：

- `tests/` 和测试输出
- PRD、SDD、DEVPLAN 和需求草稿
- CSV、state、备份、Cookie、环境文件和缓存
- 本机路径、真实评论和账号信息

## 3. 构建与核对

在隔离环境构建 wheel 和 sdist，随后列出归档内容并与上述 allowlist 比较。构建产物不得通过联网抓取或真实 Cookie 生成。

## 4. Tag 与 Release

1. 合并已通过检查的版本提交。
2. 创建与版本一致的 `vX.Y.Z` Tag。
3. 创建 GitHub Release，内容来自 `CHANGELOG.md`。
4. 附加已核对的发行包及校验值。
5. 发布后重新安装产物并执行离线 CLI smoke check。

发现敏感信息时立即停止发布。已经公开的内容按 GitHub 安全流程处理，不能只依赖后续提交删除。
