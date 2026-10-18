# NTE Autoguitar

基于 [Flet](https://flet.dev) 的吉他谱工具。目前有两个页面：**GTAB 编辑**（读取 / 编辑 / 写回 `.gtab` 文件，含试听）和**手柄测试**（`.gtab` / `.gct` 谱面 → 虚拟手柄的可行性与时序验证）。

鼠标弹奏在游戏里不稳定（时序不可控、也容易丢音），所以本项目舍弃了键鼠方案，
改用虚拟手柄（Xbox 360 控制器）来触发游戏里的音符。

![GTAB 编辑页](docs/gtab_page.png)

![手柄测试页](docs/gamepad_page.png)

## 安装与运行

要求 **Windows 10/11**（手柄页依赖 ViGEmBus 驱动，试听用 `winsound`，全局热键用
`RegisterHotKey`，截图脚本用 DWM API）和 **Python 3.10+**（开发环境是 3.14.7）。

```powershell
git clone <本仓库地址> NTE-Autoguitar
cd NTE-Autoguitar
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

依赖（`requirements.txt`）：`flet==1.0.4`、`flet-desktop==1.0.4`、`vgamepad==0.1.0`。
`vgamepad` 还需要一次性安装 **ViGEmBus** 驱动（装完确认 Windows 服务 `ViGEmBus` 在运行）：
下载安装包 <https://github.com/nefarius/ViGEmBus/releases> ，装完重启一次即可。

**推荐双击 `run.bat`**（普通权限）启动。如果游戏以管理员运行、或热键在游戏前台不生效，
改用 **`run_admin.bat`**（会弹 UAC；本次实测管理员启动后热键正常）。也可以在普通终端里运行：

```powershell
.venv\Scripts\python.exe main.py
```

> 不要从低权限进程（沙盒终端、CI、部分 IDE 的运行器）启动：那会是 Low integrity 进程，
> 游戏在前台时全局热键收不到（见「排错」）。

## 目录结构

```
main.py                  入口，只负责启动
run.bat                  普通权限启动（全局热键需要）
run_admin.bat            管理员启动（游戏以管理员运行时用）
requirements.txt         运行依赖（flet / flet-desktop / vgamepad）
.gitignore               排除 .venv、__pycache__、开发交接笔记、临时 wav
LICENSE                  MIT
src/
  app.py                 主 GUI 外壳：左侧导航 + 页面容器
  example.py             用 GtabWriter 生成示例谱 demo.gtab 的小脚本
  gtab.py                gtab 二进制格式的底层读写
  gct.py                 gct（和弦谱）二进制格式的底层读写
  chord.json             24 个和弦名（顺序 = 手柄上的位置，见下）
  ui/
    gtab_doc.py          gtab 文档模型：解析 / 校验 / 序列化（无界面依赖）
    gct_doc.py           gct 文档模型：和弦事件 / 和弦表 / Tempo（无界面依赖）
    chords.py            和弦名 → 第几页 + 第几个方向（4 页 × 6 方向）
    audio_preview.py     试听合成：文档 → 16-bit WAV（只用标准库）
    gamepad_backend.py   虚拟手柄封装 + 谱面 → 按键排期（纯函数可测）
    gct_backend.py       和弦谱 → 多根弦的扫弦/拨奏排期（复用同一套时序）
    replay_view.py       手柄页左侧的「回放轮盘」：摇杆方向（品/和弦）+ 六根弦
    hotkeys.py           全局热键（F9 播放 / F10 停止，Windows RegisterHotKey）
    components.py        跨页面复用的小控件
    pages/
      gtab_page.py       GTAB 编辑页
      gamepad_page.py    手柄测试页（回放 + 轮盘 + 时序）
      placeholder.py     占位页
tests/
  test_gtab_doc.py       模型层测试（含音长 / 自动 NoteOff / 试听时间轴）
  test_gtab_page.py      页面构建测试（音符视图、音长编辑、孤立 NoteOff）
  test_audio_preview.py  试听合成测试（频率 / WAV / BPM / Tempo）
  test_gamepad_mapping.py  谱面 → 按键排期测试
  test_replay_view.py    回放轮盘测试（扇区映射 / 状态推进 / 回放接线）
  test_gct_doc.py        gct 格式测试（读写往返 / CRC / Tempo / 缺 End）
  test_gct_mapping.py    和弦表 + gct → 扫弦排期测试
  test_gct_page.py       手柄页 gct 回放测试（载入 / 计数 / 上扫顺序 / 复位）
  test_hotkeys.py        全局热键测试（F24 注册 + 消息派发）
  preview_gtab_page.py   开发用：单独预览 GTAB 页
  preview_gamepad_page.py 开发用：单独预览手柄页（PREVIEW_SCORE=gct 看和弦谱）
  preview_replay_wheel.py 开发用：单独预览回放轮盘
  capture_gtab_screenshot.ps1 开发用：重拍界面截图（DPI 感知）；-Page gamepad|wheel，-Out 指定文件
```

预览脚本的开关（截图脚本会继承环境变量）：

| 环境变量 | 作用 |
|---|---|
| `PREVIEW_WIDTH` / `PREVIEW_HEIGHT` | 窗口逻辑尺寸；App 内容宽度是 **1144**（1280 减导航栏 96 与分隔线） |
| `PREVIEW_SCORE=gtab\|gct` | 手柄页载入单音谱还是和弦谱；gct 时默认存 `docs/gamepad_gct.png` |
| `PREVIEW_STATE=play` | 手柄页摆成「和弦扫到一半」；GTAB 页摆成「试听中」（只摆状态，不播音） |
| `PREVIEW_PAGE=4` | 用最后一页的和弦（名字最长）当示例 |
| `PREVIEW_SCALE=1.6` | 轮盘预览（`-Page wheel`）整体放大，核对和弦名会不会顶出外圈 |

例：`$env:PREVIEW_WIDTH='1144'; $env:PREVIEW_SCORE='gct'; pwsh -File tests/capture_gtab_screenshot.ps1 -Page gamepad`

## 加一个新页面

1. 在 `src/ui/pages/` 下写一个类，提供 `build() -> ft.Control`；
2. 在 `src/app.py` 的 `entries` 里加一行 `(标题, 图标, 页面控件)`。

## gtab 事件里的 voice

NoteOn / NoteOff 的事件头里固定有一个 1 字节 `voice`（默认 `0`，界面里可改 0–255）。
**格式本身没有规定它代表什么**，本项目只把它当「声部号」用：`note_blocks()` 按
`(voice, 弦, 品)` 把 NoteOn / NoteOff 配成一个个音。所以同一对 On/Off 必须用同一个
`voice`；手柄回放不读它，只读配对后的弦、品和起止 tick。

NoteOn / NoteOff 行里的三个数字：

| 输入 | 含义 |
|---|---|
| `voice` | 事件头 1 字节声部号，默认 0；只参与配对，不决定音高 |
| `弦` | 1–6 弦，决定按哪个键（6→X、5→A、4→上、3→右、2→左、1→下） |
| `品` | 0–24 品；0 = 空弦（第 5 页），其余决定摇杆方向和翻页 |

配对键是 `(voice, 弦, 品)`，三者都对上才会把 On/Off 合成一个音；配不上的 NoteOn
按 1/4 拍占位。

## 音符与音长（NoteOff 自动维护）

GTAB 编辑页的音符区**一行就是一个音**，不再把 NoteOn / NoteOff 拆成两行：

| 列 | 含义 |
|---|---|
| `拍` | 起点（拍）；改它会 On/Off 一起移动，音长不变 |
| `音长(拍)` | 音的长度；改它会自动移动这条音对应的 NoteOff |
| `弦` / `品` / `voice` | 改任一项都会同时改 NoteOn 和 NoteOff（否则配不上） |

- **保存 / 写入时自动补 NoteOff**：读到只有 NoteOn、没有 NoteOff 的谱（手工或旧工具
  留下的），保存时会按 **1/4 拍**补一个 NoteOff；想更长就在「音长」里改。
  文件里的 NoteOff 仍然照常保留，只是界面上不单独显示。
- **同一个 `(voice, 弦, 品)` 不能重叠**：改音长时会被自动夹到下一个同键音的起点之前
  （输入框会显示真实音长）；如果还是重叠，保存会明确报错，而不是写出坏文件。
- **孤立 NoteOff 不会被静默删除**：会话里把某个事件改成 NoteOff、却找不到配对的 NoteOn 时，
  它会显示在「其他事件」里并标红，保存会拒绝并指出位置——请删掉它或把对应的音补成完整音符。
  （含孤立 NoteOff 的**文件**在严格读取阶段就会打不开，不会静默丢事件。）
- 「添加音符」在最后一个音之后的整拍上加一个 **1 拍**的音（6 弦空弦）。

## 试听（调试用）

GTAB 编辑页工具栏的「试听 / 停止」按**当前写入的内容**合成并播放音乐，用于核对
音高、节奏、音长和 BPM 是否符合预期：

- 合成只用标准库：标准定弦 `E2=82.41 / A2=110 / D3=146.83 / G3=196 / B3=246.94 / E4=329.63`，
  0 品 = 空弦，每品 +1 半音；基频加少量谐波 + 指数衰减，混音后归一化成
  16-bit 单声道 WAV（22.05 kHz）；
- 时间轴用 `tick_to_seconds()`，**BPM 和 Tempo 事件都会算进去**；开头留 0.15s、
  结尾留 0.35s，最长合成 120 秒；
- 播放用 Windows 自带的 `winsound`：`winsound` 不支持「内存 WAV + 异步播放」同时使用，
  所以先写到系统临时目录的 `.wav` 再异步播放，停止 / 播完自动删除；节拍轴上有一条红色
  播放游标，工具栏显示已播 / 总时长；
- 「停止」、载入新谱、切到别的页面都会停掉试听并清空播放队列；
- 播放时时间轴会**自动横向跟随**游标，长谱也不会让红线走出可视区；
- 试听是**调试音色**，不是最终音源；要换音色改 `src/ui/audio_preview.py` 顶部的
  谐波 / 衰减常量即可。

![试听时的播放游标](docs/gtab_preview_playback.png)

> 实现注意：WAV 合成在后台线程（`page.run_thread`），进度/游标刷新由页面事件循环里的
> async ticker（`page.run_task`）推进。**不要在后台线程里直接 `page.update()`**——
> 实测那样不会重绘（红线不出现、进度文字停在「正在合成…」）。

## 手柄映射

映射规则集中在 `src/ui/gamepad_backend.py` 顶部，改常量即可，页面和时序逻辑不用动：

| 输入 | 含义 |
|---|---|
| `X` / `A` / 十字键`上` / `右` / `左` / `下` | 6 / 5 / 4 / 3 / 2 / 1 弦（`STRING_BUTTONS`） |
| 左摇杆：`上` 起顺时针每 60° | 当前页的 1–6 品（`fret_direction`） |
| `右摇杆` 下 / 上 | **gct 扫弦：下扫 / 上扫**（不再依次按 6 个弦键） |
| `右摇杆` 左 / 右 | 下一页 / 上一页（gtab / gct 共用；一次推 = 一页） |
| 第 5 页（`OPEN_PAGE`） | 六个方向全为空弦 |

回放时的行为：

- 每个音符先确认页号，需要时用**右摇杆向左（下一页）/ 向右（上一页）推一次翻一页**
  （提前 `PAGE_LEAD` 完成，推完回中），再把左摇杆转到对应品，
  **隔 `AIM_GAP`（默认 65ms）再按弦**——同一帧里切摇杆 + 按弦时，
  游戏可能先处理按键、弹成上一个品（50ms 实测仍偶发，65ms 稳定）。摇杆方向没变时
  不重复移动、也不多等；
- 如果前一个音按得太近、或翻页占满了时间，按弦会**最多推迟到下一个音开始前**来补足间隔；
  补不足时在 ℹ 提示里写明实际间隔（毫秒）。手柄页的「摇杆→键ms」可直接调（0–300ms）；
- 开场有 `LEAD_IN=0.5s` 的预备时间，够初始翻页；
- **时间上与其他音重叠的音**（比如一边按住 6 弦空弦、一边弹 5 弦）会
  **一直按住到自己的 NoteOff**：摇杆照常切到新音需要的品，原来的弦不松手；
  这条优先于点按开关，否则长音会在重叠处被掐断；
- **游戏在按下那一刻就锁定这个音，按住期间切摇杆/翻页都不会改变它**
  （真机确认）——所以「保持按住 + 切摇杆」是正常演奏方式，不是冲突；
- 同一根弦上还没松手就要拨下一个音时，会在新音前 `REPLUCK_GAP` 先松开再按下
  （默认 40ms，实测稳定；10ms 会短于游戏一帧，不稳定）。手柄页的「重拨间隔」可直接调；
- 「音符点按 80ms」开关：开着是点按（大多数游戏按下即触发），关掉则按
  NoteOff 时长按住；点按只用于**不重叠**的音；
- 页面上的提示分两级：
  - ⚠ **冲突**（可能导致弹错）：翻页次数太多来不及、弦没有映射到按键；
  - ℹ **提示**（预期处理，不算错）：重叠时换摇杆、同一根弦松开重拨。
- 「同弦重拨」按钮会在内存里生成一张测试谱（6 弦 1 品 0–2 拍 → 3 品 1–3 拍），
  用来验证游戏能否识别「松开再按」这个重拨边沿；旁边的「重拨间隔」默认 40ms，
  第二次没声就调到 60–100ms 再试；「摇杆/翻页→按键」默认 65ms（实测稳定），弹错品就调大。
- **gct 和弦谱的扫弦不走弦键**：右摇杆向下推 = 下扫、向上推 = 上扫，推到位
  `CHORD_STRUM_FLICK`（默认 50ms）后回中；如果真机里扫不出来，先调这个时长。

## 回放轮盘（手柄页左半部分）

回放时，左边的轮盘实时显示**当前正在发什么**，用来肉眼核对手柄信号对不对：

- **轮盘六个扇区** = 当前页的六个品：正上方是页内第 1 个品，顺时针每 60° 一个
  （和摇杆映射完全一致）；扇区上写的是**绝对品号**（第 2 页就是 7–12 品），
  空弦页（第 5 页）六个方向都写「空」；
- **中心**是当前绝对品号（空弦显示「空弦」），高亮扇区 + 指针表示摇杆方向；
- **六根弦**：6 弦在最上、1 弦在最下。正在按的那根变粗变蓝，右侧写出它的品号
  （重叠长音会同时亮两三根，和「保持按住」一致）；
- **gct 和弦谱**下换成和弦模式：六个扇区写**和弦名**（第 4 页就是 `G/B Am7/G D/F# F/A A Dm`），
  中心是当前和弦名 + 页号，六根弦照常高亮正在扫/拨的那几根（扫弦由右摇杆触发，
  六根弦 20ms 一根依次亮，不是六根一起亮）；
- 回放结束 / 停止 / 载入新谱会复位成「就绪」。

![回放轮盘](docs/replay_wheel.png)

![和弦模式的轮盘（第 4 页，1.6 倍放大核对）](docs/replay_wheel_gct.png)

> 这一块原来是手动按键测试面板（X/A/方向键/翻页按钮），按需求换成了轮盘；
> 手动验证可以仍用「载入示例 → 播放」跑一遍。

`build_schedule(doc, tap_notes=True/False, repluck_gap=秒, aim_gap=秒)` 是纯函数，返回
`Schedule(actions, conflicts, hints)`（`conflicts` 是可能弹错的问题，
`hints` 是预期处理），可以直接单测。

## gct 和弦谱（读 + 回放）

`.gct` 是**和弦谱**：事件只说「什么时候弹哪个和弦」，没有 NoteOn/NoteOff、也没有音长。
格式的权威说明在 `src/gct.py` 顶部（头部布局 + 事件类型表 + 扫弦方向），模型在
`src/ui/gct_doc.py`。

- **和弦的位置由名字决定，不由文件里的 chord_id 决定**：`.gct` 自带的和弦表只保证文件内
  引用有效，跨文件稳定的只有名字。`src/chord.json` 里 24 个名字的**顺序就是位置**：
  第 1 个 = 第 1 页正上方，顺时针每 60° 一个，第 7 个 = 第 2 页正上方……
  一共 **4 页 × 6 方向 = 24 个位**。查表在 `src/ui/chords.py`。
- 名字不在 `chord.json` 里的事件会在 ⚠ 冲突里写明并跳过，不会弹成别的和弦。
- 回放规则（`src/ui/gct_backend.py`；翻页 / 摇杆提前量 / 按键推迟复用
  `gamepad_backend.PositionPlanner`，和 gtab 只有一份实现）：
  - 每个和弦事件先翻页（右摇杆向左/向右，推一次一页），把左摇杆提前 `AIM_GAP`
    （默认 65ms）转到和弦位；
  - **扫弦用右摇杆**：向下推 = 下扫、向上推 = 上扫，推到位 `CHORD_STRUM_FLICK=50ms`
    后回中；轮盘上的六根弦按顺序点亮（`CHORD_STRUM_SWEEP=20ms` 一根，比原来逐键按的
    30ms 更快），亮完 `CHORD_LIGHT_HOLD=180ms` 后一起熄灭——**灯只是显示，不发手柄**；
  - **扫弦不再按 6 个弦键**；单弦拨奏（Pluck）才按对应的弦键并按住 `CHORD_HOLD=150ms`；
    只切指位（Set）只摆左摇杆、不出声；
  - **一次扫弦（六根弦依次亮）只计 1 个和弦**（进度文字是「x/y 个和弦」）。
- `.gct` 里没有音长：一个和弦响多久完全由播放器的 `CHORD_HOLD` 决定，文件里查不到，
  也别去文件里找 duration。
- 打开方式：手柄页「打开谱面」按扩展名自动分派 `.gtab` / `.gct`；「示例 gct」在内存里
  生成一张 8 个和弦的示例（走第 1 / 2 / 4 页，含一次上扫和一个 Set），
  如果 `src/MusicList/gctfile/demo.gct` 存在则优先读它。
- **gct 目前只能读和回放，没有编辑页**；和弦谱的时序规则仍以
  `tests/test_gct_mapping.py` 为准（那是可执行文档）。

![gct 和弦谱待机（第 1 页）](docs/gamepad_gct.png)

![gct 和弦回放中（第 4 页 G/B 下扫）](docs/gamepad_gct_play.png)

`build_chord_schedule(doc, strum_gap=秒, hold=秒, aim_gap=秒)` 同样是纯函数，
返回 `Schedule(actions, conflicts, hints)`，`unit_label="和弦"`。

## 测试用全局快捷键

回放测试时游戏要保持焦点，切回本程序点「播放」会让游戏失焦。进入「手柄测试」页后，
程序会注册 **系统级** 热键（Windows `RegisterHotKey`），游戏在前台也能触发：

| 热键 | 动作 |
|---|---|
| `F9` | 播放当前谱面（不抢焦点；虚拟手柄没连接会先自动连接） |
| `F10` | 停止回放并松开所有键（panic stop） |

- 热键只在「手柄测试」页激活时注册，切到别的页自动注销，避免长期占用游戏按键；
- 键位在 `src/ui/hotkeys.py` 顶部的 `HOTKEY_PLAY` / `HOTKEY_STOP`；
- 如果 `F9` 已被别的软件注册为全局热键，页面会红字提示占用；
  游戏自己用 F9/F10 的话，改上面两个常量即可；
- **必须用普通权限或管理员启动**：低权限（沙盒 / CI / 受限终端）启动的进程是 Low integrity，
  Windows 的 UIPI 会让它在游戏前台收不到全局输入（表现就是「点一下本程序再用热键才行」）。
  双击项目根目录的 `run.bat`；游戏以管理员运行时改用 `run_admin.bat`
  （本次实测：管理员启动后手柄页红字消失、热键正常）；
- 推荐流程：双击 `run.bat` → 载入谱面 → 点一下游戏窗口让它获得焦点 → `F9` 开始 → `F10` 随时停。

## 测试

```powershell
.venv\Scripts\python.exe tests\test_gtab_doc.py
.venv\Scripts\python.exe tests\test_gtab_page.py
.venv\Scripts\python.exe tests\test_audio_preview.py
.venv\Scripts\python.exe tests\test_gamepad_mapping.py
.venv\Scripts\python.exe tests\test_replay_view.py
.venv\Scripts\python.exe tests\test_gct_doc.py
.venv\Scripts\python.exe tests\test_gct_mapping.py
.venv\Scripts\python.exe tests\test_gct_page.py
.venv\Scripts\python.exe tests\test_hotkeys.py
```

共 110 项（18 / 10 / 7 / 23 / 13 / 11 / 16 / 8 / 4）。

## 排错

### 全局快捷键只在点过本程序后有效

如果 `F9` 在本程序窗口获得焦点时才生效、切到游戏就没反应，几乎都是**权限**问题：

Windows 的 UIPI 不允许低权限（Low integrity）进程接收更高权限窗口的键盘输入。
由低权限进程（沙盒终端、CI、部分 IDE 的运行器）启动的程序就是 Low integrity；
用普通终端或下面两个脚本启动则是 Medium：

- `run.bat` — 普通权限启动（游戏也以普通权限运行时用这个）；
- `run_admin.bat` — 以管理员启动（游戏本身是管理员运行时用这个，会弹 UAC）。

「手柄测试」页的热键提示会红字显示当前是否为低权限。也可以用 PowerShell 确认：

```powershell
whoami /groups | Select-String 'S-1-16-'
```

`S-1-16-4096` = Low，`S-1-16-8192` = Medium。

### 窗口一闪而过 / 程序自己退出

Flet 桌面版是「Python 进程 + flet.exe 客户端」两个进程。flet.exe 一旦崩溃，
Python 端会因为客户端断开而结束，看起来就像"程序自己退了"。

先确认不是代码问题——查事件查看器里有没有 **事件 ID 1000**（应用程序错误）：

```powershell
Get-WinEvent -FilterHashtable @{LogName='Application'; Id=1000; StartTime=(Get-Date).AddHours(-2)} |
  Select-Object TimeCreated, @{n='app';e={ ($_.Message -split "`n")[0] }} | Format-List
```

如果 `出错模块名称` 是 `GPP64.dll`、`RTSSHooks64.dll`、`NahimicOSD.dll` 之类，
说明是第三方**游戏覆盖层 / OSD** 注入到 flet.exe 后访问冲突，与本项目无关。
处理办法：退出该类软件（游戏加加、MSI Afterburner/RivaTuner、Nahimic 等），
或在它们的设置里把 `flet.exe`、`python.exe` 加入「不注入 / 屏蔽」名单。

## License

MIT，见 [LICENSE](LICENSE)。


