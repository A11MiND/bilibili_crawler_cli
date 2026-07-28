# 贡献指南

感谢参与 `bilibili_crawler_cli`。项目当前处于 Alpha 阶段，目标是提供零第三方运行时依赖、可恢复、可供人和 Agent 调用的 Bilibili 评论 CLI。

## 开发边界

- Python 3.12 或更高版本。
- 当前支持 macOS、Linux 和 WSL；因使用 POSIX `fcntl`，不直接支持原生 Windows。
- 运行时只使用 Python 标准库。新增运行时依赖必须先通过 Issue 说明必要性。
- 不加入自动登录、验证码绕过、代理池或反风控能力。
- 保持 `--json`、退出码、CSV 契约和 checkpoint 的向后兼容；需要变更时必须提供迁移说明。

## 数据安全

公开提交只能使用人工构造或充分脱敏的数据：

- 不提交 Cookie、令牌、环境文件或账号信息。
- 不提交真实 CSV、state、评论、昵称、用户 ID、IP 属地或完整 API 响应。
- 不提交本机绝对路径、内部 PRD/SDD/DEVPLAN、需求草稿或测试输出。
- 示例中的视频标识、路径和用户数据必须使用占位符。

如果凭据曾进入提交、Issue 或日志，应立即撤销或更新凭据，并按 `SECURITY.md` 私下报告。

## 开发流程

1. 从最新主分支创建单一目的的分支。
2. 保持改动范围小，不混入格式化或无关重构。
3. 为行为变化补充脱敏测试和必要文档。
4. 运行本地检查：

```bash
python -m unittest discover -s tests
python -m compileall bili_comments
python -m bili_comments capabilities --json
```

脱敏测试源码可以进入公开源码仓库，便于社区验证。wheel 和 sdist 属于发行包，必须继续通过 `MANIFEST.in` 排除 `tests/`、内部设计文档和运行数据。

## Pull Request 要求

PR 应说明：

- 问题、解决方案和影响范围。
- CLI、JSON、CSV、认证或 checkpoint 是否发生变化。
- 已执行的验证类型，不粘贴包含真实数据的测试输出。
- 对 Alpha/POSIX/零运行依赖边界的影响。

用户可见变化应更新 `CHANGELOG.md`。安全问题不要提交公开 PR，按 `SECURITY.md` 私下报告。
