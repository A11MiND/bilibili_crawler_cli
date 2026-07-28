## 变更摘要

说明问题、解决方案和改动范围。

## 兼容性

- CLI / JSON：
- CSV / checkpoint：
- 认证 / Cookie：
- Alpha / POSIX：

## 验证

说明已执行的脱敏测试和离线检查，不粘贴真实数据或敏感测试输出。

## 检查清单

- [ ] 改动保持零第三方运行时依赖，或已在 Issue 中说明例外理由。
- [ ] 未加入绕过登录、验证码、限流或访问控制的能力。
- [ ] 未提交 Cookie、真实 CSV、state、评论、账号信息、IP 属地或本机路径。
- [ ] 未提交内部 PRD/SDD/DEVPLAN、需求草稿或测试输出。
- [ ] 行为变化已补充公开脱敏 tests；发行包仍通过 `MANIFEST.in` 排除 `tests/`。
- [ ] 已评估 JSON、退出码、CSV 和 checkpoint 的兼容性。
- [ ] 用户可见变化已更新 `CHANGELOG.md`。
- [ ] 相关文档、`capabilities` 和 `SKILL.md` 已按需要同步。

安全漏洞不要提交公开 PR，请按 `SECURITY.md` 私下报告。
