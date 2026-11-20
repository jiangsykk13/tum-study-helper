# TUM 学习小助理（tum-study-helper）

让 AI 助手（Claude Code、Codex 等）帮你查看**自己的** TUM Moodle 和 Artemis：课程、课件、截止日期、成绩、作业状态、
课程公告，并把每门课的课件自动整理到电脑上。另有一个只在本机运行的网页界面：今日学习计划、番茄钟和专注统计、截止日期、
课程浏览、同步中心。

> **English:** A personal, read-only helper for TUM Moodle and Artemis: an MCP server for AI assistants (Claude Code, Codex, …)
> plus a local web UI. Logs in with your own TUM ID via SSO, keeps credentials in Windows Credential Manager, never submits
> or posts anything. Windows 10/11 only; the UI and docs are in Chinese.

非官方项目，与 TUM、Moodle、Artemis 的开发者均无关。只能看到你的账号本来就能看到的内容。

## 能做什么

| 你可以问 AI | 用到的功能 |
|---|---|
| “列出我这学期的课” | `list_courses`、`artemis_courses` |
| “看看数据库这门课有哪些资料和说明” | `list_activities`、`list_files`、`read_page` |
| “未来两周有什么要交的？” | `upcoming_deadlines`、`assignments`、`artemis_exercises` |
| “FoP 的 Artemis 公告里说监督练习怎么安排？” | `artemis_channels`、`artemis_posts` |
| “查一下我的 Moodle 成绩” | `grades`（正式成绩在 TUMonline，不在这里） |
| “先预览，再把离散结构的课件同步到电脑上” | `sync_course`、`artemis_sync`（默认只预览） |

- **本机网页界面**：双击桌面图标打开，不用 AI 也能看截止日期、浏览课程、一键同步所有课、打勾今日计划、用番茄钟。
- **课件自动整理**：同一门课 Moodle 和 Artemis 的文件合在一起，按 `Slides`、`Exercises`、`Exams`、`Other` 分类；
  线上更新时换成新版，旧版移到回收站，你改过（批注过）的文件保留。
- **进阶（可选）**：每晚 23:30 由 AI 自动生成第二天的学习计划和 Java 练习题；在 ChatGPT 网页版只读使用；
  经 Tailscale 在 Mac/iPad 上打开网页界面；浏览器油猴脚本让 Moodle 保持登录。

完整用法见 [docs/USAGE.md](docs/USAGE.md)。

## 安装

需要：Windows 10/11、TUM 账号、[Git](https://git-scm.com/download/win)、[uv](https://docs.astral.sh/uv/)（Python 管理工具，会自动装好
Python 3.12）。想用 AI 的话，再装 [Claude Code](https://docs.claude.com/en/docs/claude-code) 或 [Codex](https://github.com/openai/codex) 之一。

### 方式一：手动安装（约 10 分钟）

在 PowerShell 里，先进入你放 TUM 资料的文件夹（下面以 `D:\TUM` 为例，它就是“学习根目录”）：

```powershell
cd D:\TUM
git clone https://github.com/jiangsykk13/tum-study-helper.git
cd tum-study-helper
uv venv --python 3.12
uv pip install -r requirements.txt
.\.venv\Scripts\python.exe -I .\tum_moodle_auto.py setup
.\.venv\Scripts\python.exe -I .\tum_moodle_auto.py test
```

然后连接 AI、写课程对照表、创建桌面快捷方式，详见 **[docs/INSTALL.md](docs/INSTALL.md)**。

### 方式二：让 Claude Code / Codex 帮你装

装好 Git、uv 和 Claude Code（或 Codex）后，在 PowerShell 里：

```powershell
cd D:\TUM
git clone https://github.com/jiangsykk13/tum-study-helper.git
cd tum-study-helper
claude
```

（用 Codex 就把最后一行换成 `codex`。）然后对它说：

> 请按 docs/AI_INSTALL.md 帮我安装和配置 TUM 学习小助理。

它会检查环境、装依赖、注册 MCP 工具、帮你生成课程对照表。**只有输入 TUM 密码这一步要你自己在单独的终端窗口里完成**，
不要把密码发给 AI。流程见 **[docs/AI_INSTALL.md](docs/AI_INSTALL.md)**。

## 文件夹约定

```text
D:\TUM\                      ← 学习根目录（可以是任何文件夹）
├── tum-study-helper\        ← 本项目
├── 课程对照表.md             ← 每门课的本地文件夹和 Moodle/Artemis 编号（模板：examples/课程对照表.md）
├── Planner\                 ← 学习计划、打勾记录、番茄钟记录（网页界面自动创建需要的文件）
└── Semester 1 (WS 2026-27)\
    └── INHN0002 Fundamentals of Programming\
        ├── Official\        ← 同步下来的官方资料：Slides、Exercises、Exams、Other
        └── _Inbox\          ← 单个文件下载默认放这里
```

项目不放在学习根目录里时，设置用户环境变量 `TUM_STUDY_ROOT` 指向它。

## 安全与隐私

- **只读**：不交作业、不发帖、不报名、不改任何网站设置。
- 账号、密码和（可选的）TOTP 种子只存在 **Windows 凭据管理器**；登录会话只在内存里，不写文件、不进日志，错误信息会遮住会话值。
- 程序只连接 `www.moodle.tum.de`、登录用的 `login.tum.de` 和 `tumidp.lrz.de`，以及 `artemis.tum.de`；网页里其他网站的链接会被拒绝。
- 登录被拒绝时立即停止，不会反复尝试（避免账号被锁）。
- 下载永不覆盖已有文件；本机网页界面只监听 `127.0.0.1`，每次启动换新的访问令牌。
- AI 会看到你让它查的课程内容和成绩；课程页面和论坛里的文字只当资料，AI 不应执行其中的指令。

## 常见问题

- **两步验证**：目前只支持 TOTP，需要验证器最初设置时的 base32 密钥（不是六位验证码）。没开两步验证的账号留空即可。
- **改了 TUM 密码**：重新运行一次 `setup`。
- **Artemis 发来“新登录”邮件**：每次小助理重启后第一次用 Artemis 都会登录一次，属正常；可在 Artemis 通知设置里关掉。
- **课程颜色是灰色**：网页里的课程颜色按课程代号写在 `web/app.js` 的 `COURSE_CLASSES`，别的课显示默认颜色，可以自己加。
- **macOS / Linux**：暂不支持（下载、同步、凭据和快捷方式依赖 Windows）。

## 开发

给 AI 编程代理的开发说明在 [AGENTS.md](AGENTS.md)（Claude Code 通过 `CLAUDE.md` 读取它）：已实测的接口事实、架构、安全规则、
已知问题。个人的路径、账号、流程写在自己的 `AGENTS.local.md`（不进仓库）。

离线测试（不联网、不读凭据）：

```powershell
Get-ChildItem tests\test_*.py | ForEach-Object { .\.venv\Scripts\python.exe -I -B $_.FullName }
.\.venv\Scripts\python.exe -I -B tests\mcp_smoke.py
```

欢迎提 Issue 和 Pull Request。提交前请确认测试样例（`tests/fixtures/`）里没有个人信息、cookie 或 `sesskey`。

## 许可证

[MIT](LICENSE)
