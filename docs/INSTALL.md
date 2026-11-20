# 手动安装

想让 AI 代劳，看 [AI_INSTALL.md](AI_INSTALL.md)。下面所有命令都在 **PowerShell** 里运行（开始菜单搜索 “PowerShell”）。

## 0. 准备

| 需要 | 怎么装 | 检查 |
|---|---|---|
| Windows 10/11 | — | — |
| Git | `winget install --id Git.Git -e`，或从 [git-scm.com](https://git-scm.com/download/win) 下载 | `git --version` |
| uv | `winget install --id astral-sh.uv -e`，或见 [uv 文档](https://docs.astral.sh/uv/getting-started/installation/) | `uv --version` |
| Claude Code 或 Codex（可选） | 见 [Claude Code](https://docs.claude.com/en/docs/claude-code)、[Codex](https://github.com/openai/codex) | `claude --version` / `codex --version` |
| Google Chrome（可选） | 网页界面优先在 Chrome 里打开，没有就用默认浏览器 | — |

装完 Git 或 uv 后要**关掉并重新打开** PowerShell，新命令才找得到。不需要自己装 Python：uv 会下载 Python 3.12，
只装在本项目的 `.venv` 文件夹里，不影响电脑上别的 Python。

## 1. 下载项目

选一个放 TUM 资料的文件夹作为**学习根目录**（下面以 `D:\TUM` 为例）。课程对照表、学习计划和各课资料都放在这里，
项目放在它下面：

```powershell
cd D:\TUM
git clone https://github.com/jiangsykk13/tum-study-helper.git
cd tum-study-helper
```

以后的命令都在 `D:\TUM\tum-study-helper` 里运行。项目想放在别处时，把学习根目录设成用户环境变量（设完重新打开 PowerShell）：

```powershell
[Environment]::SetEnvironmentVariable("TUM_STUDY_ROOT", "D:\TUM", "User")
```

## 2. 安装依赖

```powershell
uv venv --python 3.12
uv pip install -r requirements.txt
.\.venv\Scripts\python.exe -I -B tests\mcp_smoke.py
```

最后一行不联网，只检查程序能否启动：结尾两行是 `OK: 17 tools` 和 `OK: 12 tools` 就对了。

## 3. 存入 TUM 账号

```powershell
.\.venv\Scripts\python.exe -I .\tum_moodle_auto.py setup
```

按提示输入 TUM ID（如 `ab12cde`）和密码，密码输入时不显示。它们存进 Windows 凭据管理器（服务名 `tum-moodle-sso`），
不写进任何文件。

最后一个问题是 TOTP 种子（两步验证）：没开两步验证就直接回车。开了的话，需要验证器最初设置时显示的 base32 密钥
（不是每 30 秒变一次的六位数）；手上没有就先留空，如果登录时被要求验证，小助理会停下来说明。

检查登录（会真的登录一次）：

```powershell
.\.venv\Scripts\python.exe -I .\tum_moodle_auto.py test
.\.venv\Scripts\python.exe -I .\tum_moodle_auto.py test-artemis
```

分别显示 Moodle、Artemis 上能看到几门课。登录被拒绝时小助理会直接停止，不会反复尝试；先确认账号密码，再重新运行 `setup`。

## 4. 写课程对照表

把模板复制到学习根目录：

```powershell
Copy-Item .\examples\课程对照表.md ..\课程对照表.md
notepad ..\课程对照表.md
```

每门课一行：本地文件夹名、课程名、Moodle 编号、Artemis 编号（没有就空着）。

- **Moodle 编号**：打开课程页，地址栏里 `course/view.php?id=121041` 的数字。
- **Artemis 编号**：打开 Artemis 课程，地址栏里 `artemis.tum.de/courses/585/...` 的数字。

也可以连上 AI 后（第 5 步）对它说“用 list_courses 和 artemis_courses 帮我填好 ..\课程对照表.md”。
对照表只影响同步中心、网页里的课程名和学习计划；AI 查课程时不需要它。

## 5. 连接 AI（可选）

在项目文件夹里运行对应的一条（`$PWD` 会自动换成当前文件夹的完整路径）。

**Claude Code**（写入项目里的 `.mcp.json`，它含本机路径，已在 `.gitignore` 里）：

```powershell
claude mcp add moodle -s project -- "$PWD\.venv\Scripts\python.exe" -I "$PWD\tum_moodle_auto.py" mcp
```

之后在项目文件夹里启动 `claude`，第一次会问是否信任这个 MCP 服务，选同意；输入 `/mcp` 能看到 `moodle` 已连接。
Claude 桌面应用的 Code 页面打开这个文件夹时也会用它。

**Codex**（写入你的 `~/.codex/config.toml`，在任何文件夹里都能用）：

```powershell
codex mcp add moodle -- "$PWD\.venv\Scripts\python.exe" -I "$PWD\tum_moodle_auto.py" mcp
codex mcp list
```

然后试着问：“列出我当前的 Moodle 课程。” 第一次查询会自动登录，要等十几秒。

## 6. 网页界面和桌面快捷方式

```powershell
.\.venv\Scripts\python.exe -I .\tum_moodle_auto.py install-shortcut
```

桌面和开始菜单会出现 **TUM 学习小助理**，双击后在 Chrome 里打开（没有终端窗口）；关掉标签页约 20 秒后自动退出。可选项：

- `--resident`：登录 Windows 时在后台启动，关掉页面也不退出。
- `--tailscale`：允许你自己的其他设备经 Tailscale 访问（见 [USAGE.md](USAGE.md)「在 Mac 上用」）。

排查问题时可以双击项目里的 `moodle-ui.bat`，它会开一个能看到输出的黑色窗口。日志在 `%LOCALAPPDATA%\TUM-Study-Helper\ui.log`。

## 7. 进阶功能（都可选）

| 功能 | 说明 |
|---|---|
| 每晚学习计划 | 每晚 23:30 由 Claude / Codex 定时任务生成第二天的计划和 Java 练习题：[nightly-plan/README.md](nightly-plan/README.md) |
| ChatGPT 只读 | 通过 OpenAI 官方安全隧道在 ChatGPT 网页版里只读查询：[USAGE.md](USAGE.md)「在 ChatGPT 网页版里用」 |
| Moodle 保持登录 | 把 `tum-moodle-keepalive.user.js` 装进 Tampermonkey 等油猴扩展，Moodle 标签页开着时不会 8 小时后被登出 |

## 更新

```powershell
git pull
uv pip install -r requirements.txt
```

用快捷方式打开的网页界面会在代码更新后自动重启。

## 卸载

1. 删掉桌面、开始菜单（和 `shell:startup` 里，如果用了 `--resident`）的 **TUM 学习小助理** 快捷方式。
2. 删除凭据：控制面板 → 凭据管理器 → Windows 凭据，删掉名字里带 `tum-moodle-sso` 的条目。
3. 删除 MCP 配置：`claude mcp remove moodle -s project` 或 `codex mcp remove moodle`。
4. 删除项目文件夹。学习根目录里的课程资料、`课程对照表.md` 和 `Planner\` 是你的数据，需要时自己保留。
