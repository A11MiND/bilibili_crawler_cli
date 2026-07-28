# bilibili_crawler_cli

[![CI](https://github.com/A11MiND/bilibili_crawler_cli/actions/workflows/ci.yml/badge.svg)](https://github.com/A11MiND/bilibili_crawler_cli/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)

零第三方运行时依赖的哔哩哔哩评论命令行工具。支持一级与二级评论 CSV 导出、断点续爬、终端进度、登录校验、IP 属地字段、任务锁，以及面向 LLM/coding agent 的稳定 JSON 接口。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
bilibili-crawler crawl "BVxxxxxxxxxx" --anonymous
```

登录抓取：

```bash
bilibili-crawler auth set
bilibili-crawler auth check
bilibili-crawler crawl "BVxxxxxxxxxx"
```

完整安装、命令、数据契约、恢复协议、Agent 集成和代码说明见 [README_CN.md](./README_CN.md)。

参与项目：

- [贡献指南](./CONTRIBUTING.md)
- [安全政策](./SECURITY.md)
- [路线图](./ROADMAP.md)
- [变更记录](./CHANGELOG.md)

当前版本为 Alpha，只处理当前访问身份通过哔哩哔哩接口可见的数据，不绕过登录、权限、验证码或风控。项目使用 MIT License。
