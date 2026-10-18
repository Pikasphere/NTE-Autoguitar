# AGENTS.md — 给接手本项目的 AI

> 2026-10-18 新增。任何 AI 接手前先读这一页：**检查点在哪、经验在哪、哪些坑不能踩**。
> 这里只放**稳定规则**；「这一棒做到哪了」看 `checkpoint/` 里最新的那份。

## 📁 交接检查点全部在 `checkpoint/`（重要）

- **所有** `checkpoint-*.md` 都在 **`checkpoint/` 目录**里；项目根目录**不放**检查点文件。
- 接手第一步：`ls checkpoint/`，按修改时间找最新那份（文件名是会话 id，越早的越细）。
- **新写检查点也写进 `checkpoint/`**（`checkpoint/checkpoint-<会话id>.md`），**不要**写到根目录。
- 目录里的 `put checkpoint here` 是用户留的位置标记，**别删**。
- `checkpoint/` 已在 `.gitignore`（内部交接状态，不进版本库）。
- 检查点只写**状态**；项目的长期经验在下面这些地方，别只读检查点就动手。

## 🗺 文档地图（经验不只写在检查点里）

| 位置 | 内容 |
|---|---|
| `README.md` | 唯一对外文档：安装、五个页面、gtab/gct 映射、MIDI 键位 + Capo、设置页、试听、测试计数、排错 |
| `checkpoint/` | 接力状态：这一棒到哪、已知坑、已修缺陷、未决事项、已读水位线 |
| `src/*.py`、`src/ui/*.py` 顶部 docstring | 格式/算法/时序的权威说明（gtab/gct 二进制、SMF、键位表、手柄映射、设置语义） |
| `tests/test_*.py` | 可执行文档；跑一遍就知道真实行为 |
| `tests/preview_*.py` + `tests/capture_gtab_screenshot.ps1` | 看界面 / 重拍 `docs/*.png` |

全项目 md 只有 `README.md`、本文件、`checkpoint/*.md`；没有 `任务单/`、`HANDOVER.md`、`事实总表.md`。

## ⚠️ 必守约束（完整清单看最新检查点的「已知坑」）

- 测试必须在**默认设置**下跑；构造页面的测试/预览先 `settings_mod.set_settings(AppSettings(), persist=False)`。
- 测试**别用** `tempfile`：用 `tests/` 固定文件名 + `finally unlink`。
- `ft.Colors` 是 Enum，别 `str()`；Flet 1.0：`Dropdown.on_select`、`ft.Button`、`FilePicker` 需页面上下文。
- 工具栏/标题栏别靠 `expand` 占位（内容宽约 1144，最小窗 1020）。
- 手柄必须**手动连接**；全局热键只在对应页激活时注册，切页先注销。
- 全局事件 CRC **只在保存时强制**；gtab/gct 两页已**没有**单独开关（设置页是唯一控制点）。
- MIDI 页**不自动移调**：只有手选 **Capo**（0–12 品），载入新 MIDI 时回到 0。
- **别替用户 commit / push**；用户文件（`Letters to Me.gtab`、`demo.gct`）别删别改。
- 截图必须用**宿主 `pwsh`** 调脚本；脚本会把预览窗口临时置顶，避免截进浏览器/画图。

## 🧭 当前状态（截至 2026-10-18 17:45）

- 功能已齐：gtab/gct 读写与编辑、手柄回放、MIDI→键盘、设置页（全局 CRC + 八度投影 + 版本号）。
- 测试 **214/214**（13 个脚本）；`docs/` 下 12 张截图已按新 UI 重拍。
- 版本号 `1.0.0-rc1`（`src/version.py`）；窗口图标 `docs/AkaneRin.ico`（`src/branding.py`）。
- **改动未提交**（用户自己提交）；真机验收（全局 CRC 文件游戏可读性、MIDI 投影听感）仍待用户实测。
