---
name: "bilibili-crawler"
description: "通过可恢复的命令行任务，将单个哔哩哔哩公开视频的可见一级和二级评论导出为 CSV。"
---

# Bilibili Crawler CLI

## 适用任务

当用户要求抓取、恢复抓取或检查单个哔哩哔哩视频的公开评论导出任务时使用本工具。

本工具只处理当前访问身份通过哔哩哔哩接口可见的数据。它不绕过登录、验证码、风控、权限或内容审核。

## 安装与发现

需要 Python 3.12+ 和支持 POSIX `fcntl` 文件锁的 macOS、Linux 或 WSL。

在项目根目录安装：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install .
```

需要独立安装到 PATH 且已安装 `uv` 时：

```bash
uv tool install . --python 3.12
```

确认命令可用：

```bash
bilibili-crawler --help
bilibili-crawler capabilities --json
```

如果命令未安装，可在项目根目录使用：

```bash
python -m bili_comments --help
```

## Agent 调用规则

1. 自动化调用始终添加 `--json`。
2. 先调用 `capabilities` 获取当前命令和退出码，不依赖 README 的静态副本。
3. 抓取前按需要调用 `status <video>`，避免无意重建已完成任务。
4. 匿名抓取显式添加 `--anonymous`。
5. 需要 IP 属地时，先由用户合法提供 Cookie，再调用 `auth set` 和 `auth check`。
6. 不把 Cookie 放入命令参数、提示词、日志、CSV、断点或版本库。
7. 收到可恢复错误时保留输出和断点，稍后重复同一 `crawl` 命令。
8. 只有用户明确要求丢弃旧进度时才添加 `--restart`。
9. 同一视频不得并发启动两个抓取进程；工具的任务锁会拒绝第二个进程。
10. 解析 stdout 的单行 JSON；进度信息只写入 stderr。

## 常用命令

查看机器可读能力：

```bash
bilibili-crawler capabilities --json
```

匿名抓取：

```bash
bilibili-crawler crawl "BVxxxxxxxxxx" --anonymous --json
```

检查本地任务：

```bash
bilibili-crawler status "BVxxxxxxxxxx" --json
```

保存用户通过受信方式提供的 Cookie：

```bash
bilibili-crawler auth set
```

校验已保存的登录状态：

```bash
bilibili-crawler auth check --json
```

登录抓取：

```bash
bilibili-crawler crawl "BVxxxxxxxxxx" --json
```

明确放弃旧进度并重抓：

```bash
bilibili-crawler crawl "BVxxxxxxxxxx" --restart --json
```

## JSON 协议

所有 `--json` 命令只在 stdout 输出一个 JSON 对象，固定顶层字段如下：

```json
{
  "schema_version": 1,
  "command": "crawl",
  "ok": true,
  "exit_code": 0,
  "data": {},
  "error": null
}
```

失败时 `ok` 为 `false`，`data` 为空对象或提供安全的本地状态摘要，`error` 提供结构化错误。调用方必须同时检查进程退出码和 `ok`，不能只检查文件是否存在。

## 退出码

退出码的当前定义应以 `capabilities --json` 为准：

- `0`：命令成功。
- `2`：输入或配置错误。
- `3`：视频不可用或无权访问。
- `4`：上游网络、限流或风控错误；抓取已经开始时可保留断点后重试。
- `5`：本地存储、断点或响应格式错误。
- `6`：登录凭据无效或已过期。
- `7`：查询的本地任务不存在。
- `70`：JSON 模式下未分类的内部错误。
- `130`：用户中断；抓取已经开始时，已提交进度可恢复。

## 结果验证

成功后从 JSON 的 `data` 读取 CSV 和 checkpoint 路径。继续执行以下检查：

1. `ok` 为 `true` 且退出码为 `0`。
2. CSV 文件存在并包含固定表头。
3. `status <video> --json` 返回任务状态 `complete`。
4. 不用网页显示的评论总数替代接口可见结果的校验。

## 安全边界

- 只允许合法的哔哩哔哩视频 URL 或 BV 号。
- Cookie 仅从受控输入、受限权限文件或 `BILI_COOKIE` 环境变量读取。
- 不得回显、复制、提交或上传 Cookie。
- 不得使用本工具规避访问控制、验证码、限流或平台规则。
- CSV 中包含公开账号信息和评论内容，发布或共享前应按适用规则脱敏。
