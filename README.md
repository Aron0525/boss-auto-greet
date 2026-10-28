# bossgreet

面向个人 Windows 电脑的 BOSS 直聘岗位搜索、筛选与打招呼工具。

它不是 BOSS 直聘官方工具，不提供云端服务、多人共享账号或自动登录。浏览器登录、扫码和安全校验始终由使用者本人完成；程序默认只读，真实发送必须明确选择。

完整操作请看 [中文操作手册](docs/操作手册.md)。

## 能做什么

| 命令 | 行为 | 会发送消息吗？ |
| --- | --- | --- |
| `scan` | 搜索并按规则筛选列表岗位 | 不会 |
| `preview` | 筛选、读取职位详情并预览结果 | 不会 |
| `capture` | 把筛选结果和 JD 写入本地 JSONL | 不会 |
| `export` | 从本地联系记录导出 Markdown/HTML 清单 | 不会 |
| `run --send` | 点击 BOSS 的“立即沟通” | 会 |

默认的 `--greet-only` 只使用你 BOSS 账号中已有的默认招呼语。`--custom` 会在默认招呼语之后再发送一条自定义内容，因此对方可能收到两条消息。

## 环境要求

- Windows 10/11
- Python 3.10 或更新版本
- PowerShell
- Microsoft Edge 或 Google Chrome
- 你自己的 BOSS 直聘账号

程序会为 Edge/Chrome 使用独立的浏览器资料目录和本机调试端口，不会读取或保存密码。首次运行时请保留浏览器窗口可见，不要最小化它。

## 安装

从 GitHub 下载 ZIP 并解压，或克隆公开版仓库。在包含 `pyproject.toml` 的目录运行：

```powershell
py -3 -m venv .venv
$py = (Resolve-Path .\.venv\Scripts\python.exe).Path
& $py -m pip install --upgrade pip
& $py -m pip install -e .
& $py -m bossgreet --help
```

如果没有 `py` 启动器，请使用本机 Python 程序的完整路径。可选依赖：`.[osinput]` 用于真实鼠标键盘输入，`.[llm]` 用于模型生成消息，`.[dev]` 用于测试。

## 首次使用

建议把本地配置、联系记录、截图和导出文件放在源码目录外：

```powershell
$work = Join-Path $env:LOCALAPPDATA 'bossgreet\work'
& $py -m bossgreet init --dir $work
Set-Location $work
& $py -m bossgreet info
```

在 `config.yaml` 中：

- `browser.name` 选择 `edge` 或 `chrome`；
- 修改 `search_queries`、`keywords`、`must_keywords`、城市和筛选条件；
- 第一次真实运行前，把 `pace.daily_limit`、`pace.batch_size` 设为保守的小值；
- `profile.yaml` 只在你需要自定义消息时填写真实且愿意发送的资料。

第一次预览只会打开专用浏览器的 BOSS 登录页并退出：

```powershell
& $py -m bossgreet preview --limit 5 --pages 1 --show-window
```

请在该专用窗口完成登录或安全校验，然后重新运行同一条命令。第二次运行时程序才会打开职位页进行预览。

## 搜索一个主题

`--query` 会临时覆盖配置中的 `search_queries`，适合先验证一个方向：

```powershell
& $py -m bossgreet scan --query "AI agent" --pages 3 --show-window
& $py -m bossgreet preview --query "AI agent" --limit 5 --pages 1 --show-window
```

`scan` 不打开职位详情；`preview` 会读取职位详情，但两者都不会点击“立即沟通”。如果换了方向，别忘记同步调整筛选关键词、经验年限、城市和排除规则，否则结果可能被旧规则过滤掉。

## 真实发送（可选）

先完成预览，并在 BOSS 中确认默认招呼语。首次建议只尝试一条：

```powershell
& $py -m bossgreet run --send --greet-only --limit 1 --pages 1 --show-window
```

终端要求输入准确的 `send` 才会开始。真实运行时，`--limit` 与 `pace.daily_limit` 的较小值是**成功发送招呼语的上限**；每到 `pace.batch_size` 会再次要求确认。

程序会：

- 使用 `contacted.json` 跳过已联系岗位；
- 计入既有成功联系记录，避免重启后再次联系同一公司；
- 在页面异常、登录墙、安全校验、平台额度、按钮缺失或发送状态不确定时停止；
- 对 BOSS 的“工作经历不匹配”提示跳过该岗位，不发送也不尝试绕过。

不要把 `run --send` 当作正式简历投递的替代品：该命令执行的是 BOSS 站内“立即沟通”。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 第一次命令没有开始搜索 | 正常。先在专用浏览器登录，再重跑同一条 `preview`/`scan` 命令。 |
| 找不到浏览器 | 在 `config.yaml` 填写 `browser.executable` 的完整 exe 路径。 |
| 浏览器打开但连不上 | 检查 `browser.cdp_url`；不要让另一种浏览器占用同一端口。 |
| 出现安全校验 | 在专用浏览器中由本人完成校验，再重新预览。 |
| 显示“工作经历不匹配” | 平台拒绝该岗位，程序会跳过；调整经验筛选或选择其他岗位。 |
| 发送状态不确定 | 程序会停止并在本地记录中标记待核对；先在 BOSS 聊天页确认，切勿直接重试。 |
| 页面点击无效 | 浏览器不要最小化；从源码目录运行 `scripts/doctor.py` 做诊断。 |

## 隐私与安全

`config.yaml`、`profile.yaml`、`contacted.json`、截图、日志、采集结果和导出报告都可能包含个人资料、求职记录或聊天内容。它们应只保存在本地工作目录，绝不上传到 GitHub。

`.gitignore` 只能阻止新文件误提交，不能移除已进入 Git 历史的敏感数据。提交公开仓库前，请检查 `git status`，并运行：

```powershell
& $py .\scripts\privacy_scan.py . --staged
```

项目采用 [MIT License](LICENSE)。测试仅使用模拟数据，不连接招聘网站，也不发送消息。
