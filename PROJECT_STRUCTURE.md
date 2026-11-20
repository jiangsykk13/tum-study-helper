# 项目文件结构

此清单记录项目源码、配置、说明文档、测试及测试样例，方便 AI 快速了解仓库布局（2026-11-20 开源整理时核对）。
已省略 Git 内部目录（`.git/`）、Python 缓存目录（`__pycache__/`）和本机预览配置（`.claude/`）。
运行时数据（计划、进度、专注记录、检查记录、材料索引等）不在本目录，而在 `<学习根目录>\Planner\`（学习根目录默认是项目的上一层，或环境变量 `TUM_STUDY_ROOT`）；课程文件在各课的 `Official\`。

```text
tum-study-helper\
├── docs/
│   ├── nightly-plan/
│   │   ├── README.md
│   │   ├── 每晚任务说明.md
│   │   └── 规划设定.模板.md
│   ├── AI_INSTALL.md
│   ├── INSTALL.md
│   └── USAGE.md
├── examples/
│   └── 课程对照表.md
├── tests/
│   ├── fixtures/
│   │   ├── artemis/          channels, dashboard, exercises, lectures, posts (JSON)
│   │   ├── assign/           draft, german, missing_status, new_overdue, submitted_graded (HTML)
│   │   ├── planner/计划/      2026-10-22.json, 2026-10-23.json
│   │   ├── render/           merged, structured, tables (HTML)
│   │   ├── course_map.md
│   │   ├── grades_course.html
│   │   ├── grades_course_percentage.html
│   │   └── grades_overview.html
│   ├── live_check.py
│   ├── mcp_smoke.py
│   ├── preview_offline.py
│   ├── test_*.py             （18 个）
│   └── test_*.cjs            （3 个，用 node 运行）
├── web/
│   ├── app.ico
│   ├── app.js
│   ├── icon.png
│   ├── index.html
│   └── style.css
├── .gitattributes
├── .gitignore
├── AGENTS.md
├── CLAUDE.md
├── LICENSE
├── PROJECT_STRUCTURE.md
├── README.md
├── requirements.txt
├── app_window.py
├── artemis.py
├── artemis_checks.py
├── chatgpt-bridge.bat
├── course_map.py
├── entrepreneurship.py
├── focus.py
├── local_lock.py
├── material_reorganize.py
├── moodle-ui.bat
├── moodle_assign.py
├── moodle_render.py
├── moodle_sync.py
├── moodle_web.py
├── planner.py
├── planner_presentation.py
├── planner_runtime.py
├── practice.py
├── study_check.py
├── study_feedback.py
├── study_materials.py
├── study_settings.py
├── tum-moodle-keepalive.user.js
└── tum_moodle_auto.py
```

不进仓库、只在各人本机的文件：`.venv/`、`.mcp.json`（含本机路径）、`.claude/settings.local.json`、`AGENTS.local.md`（个人说明）、`ui.log`。

## 主要文件

### 站点访问与同步

| 路径 | 用途 |
|---|---|
| `tum_moodle_auto.py` | 主入口：TUM SSO 登录、Moodle 客户端、MCP 工具注册（完整 17 个 / 只读 12 个）及 `ui`、`setup`、`chatgpt` 等命令。 |
| `artemis.py` | Artemis 登录、课程、讲座、练习、频道与帖子/回复只读获取、附件下载和同步适配。 |
| `moodle_sync.py` | Moodle/Artemis 文件预览与同步、路径安全、按类型存放及同步状态（v1–v3）管理。 |
| `moodle_render.py` | Moodle HTML 转结构化文字、表格和标签渲染。 |
| `moodle_assign.py` | 作业页面 HTML 解析（未经真实页面验证）。 |
| `entrepreneurship.py` | 仅供 MGTHN0133 两门 Moodle 课使用的 Lecture/Exercise 分类、Session 前缀和论文别名规则。 |
| `material_reorganize.py` | 已同步资料的本地整理与恢复（经 `planner.py materials reorganize` 调用，默认只预览）。 |
| `local_lock.py` | 多进程共享目录互斥，Windows 命名 mutex，不创建锁文件。 |
| `course_map.py` | 解析 `<学习根目录>\课程对照表.md`，得到每门课的本地文件夹、Moodle/Artemis 编号和同步目标。 |

### 学习计划与检查

| 路径 | 用途 |
|---|---|
| `planner.py` | 学习计划文件的读取、打勾写入、欠账/顺延计算、计划检查；命令 `debt`、`validate`、`carry`、`revise`、`materials`、`feedback` 等。 |
| `planner_runtime.py` | 柏林目标日期、阶段恢复、草稿校验和原子发布（prepare/stage/publish）。 |
| `planner_presentation.py` | 只读生成状态、任务资料继承及当天安排/补旧工作量汇总，不发布或改写计划。 |
| `study_check.py` | 白天检查与每晚采集：调用带保护的工具函数同步并采集任务和公告快照，维护 `检查记录.json` 与提醒 outbox（`collect`、`ingest`、`view`、`ack`）。 |
| `study_settings.py` | 从现有 Markdown 规划设定读取预算、课程与特殊日期。 |
| `study_materials.py` | 下载与解读之间的持久材料队列、版本和页码摘要。 |
| `study_feedback.py` | 最近七天实际耗时与理解度的只读汇总和估时建议。 |
| `artemis_checks.py` | Artemis 练习页上用户自己的勾选（`Planner\Artemis练习.json`）。 |
| `practice.py` | Java/JUnit 检查、唯一日题登记、代码指纹和评估记录；`--init-runner` 生成 `运行测试.bat`。 |
| `focus.py` | 番茄钟与专注统计：`<学习根目录>\Planner\专注记录.json` 的计时状态、记录、设置及统计纯函数；不连 Moodle/Artemis。 |

### 界面与启动

| 路径 | 用途 |
|---|---|
| `moodle_web.py` | 本机网页界面后端（只听 127.0.0.1 的 JSON 接口，令牌/Host/Origin 校验）。 |
| `web/` | 网页界面前端：HTML、CSS、原生 JS；首页是今日计划，另有番茄钟、统计、截止日期、课程、成绩、同步中心。`app.ico`、`icon.png` 为程序图标。 |
| `app_window.py` | 桌面快捷方式启动：无终端，页面开在 Chrome 标签页里；单实例（`ui --app`、`install-shortcut [--resident] [--tailscale]`）。 |
| `moodle-ui.bat` | 双击启动网页界面（带终端窗口，排查问题用）。 |
| `chatgpt-bridge.bat` | 双击让 ChatGPT 通过 OpenAI 安全隧道使用只读工具。 |
| `tum-moodle-keepalive.user.js` | 浏览器油猴脚本，用于 Moodle/TUMonline 会话保活。 |
| `requirements.txt` | Python 依赖（装进 `.venv`）。 |

### 说明文档

| 路径 | 用途 |
|---|---|
| `README.md` | 项目介绍、两种安装方式入口、安全说明、常见问题。 |
| `docs/INSTALL.md` | 手动安装的完整步骤（依赖、账号、课程对照表、连接 Claude Code / Codex、快捷方式、更新与卸载）。 |
| `docs/AI_INSTALL.md` | 让 Claude Code / Codex 代为安装配置：给使用者的开头，和给 AI 代理的逐步流程与规则。 |
| `docs/USAGE.md` | 详细用法：AI 提问、网页界面各页面、ChatGPT 只读、同步、学习计划、限制、隐私。 |
| `docs/nightly-plan/` | 每晚学习计划（可选）：设置方法、给定时任务看的每晚说明、规划设定模板。 |
| `examples/课程对照表.md` | 课程对照表模板（虚构编号）。 |
| `AGENTS.md` | 给 AI 编程代理的开发说明：已实测的接口事实、架构、安全约束、已知问题。 |
| `CLAUDE.md` | 让 Claude Code 读取 `AGENTS.md` 和本机的 `AGENTS.local.md`。 |
| `LICENSE` | MIT 许可证。 |

## 测试目录

Python 测试均用项目指定的 Python 加 `-I -B` 运行；`.cjs` 用 `node tests/<文件>` 运行。全部不联网（`live_check.py` 除外）。

| 路径 | 覆盖内容 |
|---|---|
| `tests/test_offline.py` | Moodle 工具与客户端的离线回归/集成行为。 |
| `tests/test_assign.py` | 作业解析及对应 HTML 样例。 |
| `tests/test_sync.py` | 文件同步、状态记录、路径安全和失败场景。 |
| `tests/test_client_sync.py` | 文件认证恢复、有限重试、page/label 边界及接口漂移。 |
| `tests/test_entrepreneurship.py` | Entrepreneurship 分类命名、论文别名、版本分离、可恢复迁移及旧计划引用。 |
| `tests/test_render.py` | HTML 渲染、表格及标签顺序。 |
| `tests/test_artemis.py` | Artemis 解析、请求安全、频道/帖子分页与未读保护、下载和同步。 |
| `tests/test_web.py` | 网页接口的访问控制、参数校验、确认写入、串行执行和今日计划接口。 |
| `tests/test_course_map.py` | 课程对照表解析。 |
| `tests/test_planner.py` | 学习计划文件的读取、打勾校验、欠账、顺延和写入。 |
| `tests/test_planner_runtime.py` | 分段欠账、补跑、DST、发布中断恢复、材料证据和预算。 |
| `tests/test_planner_presentation.py` | 只读生成状态、顺延任务继承资料、当天/补旧工作量口径。 |
| `tests/test_study_check.py` | 检查记录的来源隔离、失败保留、提醒阈值去重、冲突、损坏拒写和隐私。 |
| `tests/test_materials.py` | 下载后中断、部分页码、发布对账、历史版本和损坏索引。 |
| `tests/test_practice.py` | 真实编译一道小题并跑 JUnit（缺 javac/java 或 JUnit jar 时跳过）。 |
| `tests/test_practice_records.py` | 相同代码只调整一次难度，零分尝试与旧题缺基线。 |
| `tests/test_feedback.py` | 可选反馈、样本门槛和只读估时建议。 |
| `tests/test_focus.py` | 番茄钟计时、暂停、跨午夜拆分、统计口径、补录、导入导出及网页路由。 |
| `tests/test_plan_dates.cjs` | 网页日期函数在任何浏览器时区下都按柏林日期计算（含夏令时切换）。 |
| `tests/test_exercise_order.cjs` | Artemis 练习页按发布周分组和排序。 |
| `tests/test_plan_priority.cjs` | 「建议先做」只从当天课程任务中选，日期未定的事项不显示倒计时。 |
| `tests/mcp_smoke.py` | MCP 工具注册检查。 |
| `tests/preview_offline.py` | 用系统临时目录里的虚构计划预览真实网页（`--stress`、`--interrupted`）。 |
| `tests/live_check.py` | 明确指定课程的真实验证（需联网和凭据），下载仅进入系统临时目录。 |
| `tests/fixtures/` | 离线测试使用的脱敏/虚构 HTML 与 JSON 样例：`artemis/`（含频道和帖子）、`assign/`、`planner/计划/`、`render/`、成绩页和课程对照表。 |
