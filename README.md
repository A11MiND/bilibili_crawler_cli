# bilibili_crawler_cli

一个零第三方运行时依赖的哔哩哔哩评论命令行工具。支持一级与二级评论导出、CSV、断点续爬、登录状态校验、IP 属地字段、任务锁，以及面向 LLM/coding agent 的稳定 JSON 接口。

完整安装、使用、Agent 集成、代码实现和设计说明见 [README_CN.md](./README_CN.md)。

快速安装：

```bash
python3 -m pip install .
bilibili-crawler capabilities --json
```

匿名抓取：

```bash
bilibili-crawler crawl "BVxxxxxxxxxx" --anonymous
```

登录抓取：

```bash
bilibili-crawler auth set
bilibili-crawler auth check
bilibili-crawler crawl "BVxxxxxxxxxx"
```

本工具只处理当前访问身份通过哔哩哔哩接口可见的数据，不绕过登录、权限、验证码或风控。项目使用 MIT License。
