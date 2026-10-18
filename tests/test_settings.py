"""设置页 / 全局设置 / MIDI 八度投影的测试（不弹窗口、不真发按键、不改用户设置文件）。

覆盖三块：

- ``ui/settings.py``：默认值、JSON 往返、坏文件容错、缓存与重载、``force_event_crc``
  对 gtab / gct 两个模型都生效；
- 两个编辑页已**没有**单独的「事件 CRC」开关（只受全局开关 + 保存时 `force_event_crc` 控制），
  以及页面级保存链路（保存后重新读回，flags bit0 确实写进文件）；
- MIDI 八度投影：``project_note`` 的边界、``build_key_schedule(project_octave=True)``、
  页面上的块 / Capo 不自动移调 / 播放时的提示；设置页的版本号。

**不要用 tempfile**（沙箱里 %TEMP% 不可写，见检查点坑表）：固定文件名
``tests/_settings.test.json`` + ``finally unlink``；涉及全局设置的测试都把
``ui.settings.SETTINGS_FILE`` 临时指到这个文件，跑完恢复，绝不写 ``src/settings.json``。

运行：``.venv\\Scripts\\python.exe tests/test_settings.py``
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from ui import settings as settings_mod  # noqa: E402
from ui.gct_doc import GctDocument, demo_document  # noqa: E402
from ui.gtab_doc import GtabDocument  # noqa: E402
from ui.keyboard_backend import (  # noqa: E402
    build_key_schedule,
    load_key_table,
    parse_key_table,
    project_note,
)
from ui.midi_doc import DEFAULT_TEMPO_US, MidiDocument, NoteEvent, TempoMark, demo_document as midi_demo  # noqa: E402
from ui.pages.gct_page import GctPage  # noqa: E402
from ui.pages.gtab_page import GtabPage  # noqa: E402
from ui.pages.midi_page import MidiPage  # noqa: E402
from ui.pages.settings_page import SettingsPage  # noqa: E402
from ui.settings import (  # noqa: E402
    AppSettings,
    DEFAULTS,
    SETTINGS_FILE,
    force_event_crc,
    get_settings,
    load_settings,
    reload_settings,
    save_settings,
    set_settings,
    update_settings,
)

HERE = Path(__file__).resolve().parent
TEST_FILE = HERE / "_settings.test.json"
REAL_SETTINGS_FILE = SETTINGS_FILE


class FakePage:
    """只实现 MIDI / 设置页用到的那几个 Page 方法。"""

    def __init__(self) -> None:
        self.tasks: list[tuple[object, tuple]] = []
        self.dialogs: list[object] = []
        self.updates = 0

    def run_task(self, handler, *args) -> None:
        self.tasks.append((handler, args))

    def show_dialog(self, dialog) -> None:
        self.dialogs.append(dialog)

    def update(self) -> None:
        self.updates += 1


def _use(**flags) -> AppSettings:
    """把缓存设成指定开关（不写盘），返回它。"""
    return settings_mod.set_settings(AppSettings(**flags), persist=False)


def _defaults() -> None:
    settings_mod.SETTINGS_FILE = REAL_SETTINGS_FILE
    _use()
    TEST_FILE.unlink(missing_ok=True)


def _patch_file() -> None:
    settings_mod.SETTINGS_FILE = TEST_FILE


# ---------------------------------------------------------------- 设置模型


def test_defaults_are_off() -> None:
    assert DEFAULTS.global_event_crc is False
    assert DEFAULTS.midi_project_octave is False
    assert AppSettings() == DEFAULTS


def test_load_missing_file_returns_defaults() -> None:
    _defaults()
    assert load_settings(TEST_FILE) == DEFAULTS


def test_save_and_load_round_trip() -> None:
    _defaults()
    wanted = AppSettings(global_event_crc=True, midi_project_octave=True)
    try:
        written = save_settings(wanted, TEST_FILE)
        assert written == TEST_FILE
        assert load_settings(TEST_FILE) == wanted
        raw = json.loads(TEST_FILE.read_text(encoding="utf-8"))
        assert raw == {"global_event_crc": True, "midi_project_octave": True}
    finally:
        TEST_FILE.unlink(missing_ok=True)


def test_load_bad_json_returns_defaults() -> None:
    _defaults()
    try:
        TEST_FILE.write_text("{not json", encoding="utf-8")
        assert load_settings(TEST_FILE) == DEFAULTS
    finally:
        TEST_FILE.unlink(missing_ok=True)


def test_load_non_object_returns_defaults() -> None:
    _defaults()
    try:
        TEST_FILE.write_text("[true, false]", encoding="utf-8")
        assert load_settings(TEST_FILE) == DEFAULTS
    finally:
        TEST_FILE.unlink(missing_ok=True)


def test_load_ignores_wrong_types() -> None:
    _defaults()
    try:
        # 字符串 "true" / 整数 1 都不算布尔：不能用真值蒙混过关
        TEST_FILE.write_text(
            '{"global_event_crc": "true", "midi_project_octave": 1}', encoding="utf-8"
        )
        assert load_settings(TEST_FILE) == DEFAULTS
    finally:
        TEST_FILE.unlink(missing_ok=True)


def test_set_settings_persist_false_does_not_write() -> None:
    _defaults()
    _patch_file()
    try:
        _use(global_event_crc=True)
        assert not TEST_FILE.exists(), "persist=False 不应该碰磁盘"
        assert get_settings().global_event_crc is True
    finally:
        _defaults()


def test_update_settings_writes_and_ignores_unknown_fields() -> None:
    _defaults()
    _patch_file()
    try:
        updated = update_settings(global_event_crc=True, bogus_field=1)
        assert updated == AppSettings(global_event_crc=True, midi_project_octave=False)
        assert load_settings(TEST_FILE) == updated
    finally:
        _defaults()


def test_get_settings_caches_until_reload() -> None:
    _defaults()
    _patch_file()
    try:
        save_settings(AppSettings(global_event_crc=True), TEST_FILE)
        settings_mod._cache = None
        assert get_settings().global_event_crc is True
        # 直接改盘：缓存还在，get 不会看到
        save_settings(AppSettings(global_event_crc=False, midi_project_octave=True), TEST_FILE)
        assert get_settings().global_event_crc is True
        # reload 之后才看到
        assert reload_settings().midi_project_octave is True
        assert get_settings().global_event_crc is False
    finally:
        _defaults()


def test_corrupt_file_does_not_break_get_settings() -> None:
    _defaults()
    _patch_file()
    try:
        TEST_FILE.write_text("", encoding="utf-8")
        settings_mod._cache = None
        assert get_settings() == DEFAULTS
    finally:
        _defaults()


# ---------------------------------------------------------------- force_event_crc


def test_force_event_crc_off_keeps_flags() -> None:
    _defaults()
    gtab_doc = GtabDocument()
    gct_doc = GctDocument()
    assert force_event_crc(gtab_doc) is False
    assert force_event_crc(gct_doc) is False
    assert gtab_doc.event_crc_enabled is False
    assert gct_doc.event_crc_enabled is False


def test_force_event_crc_on_gtab_round_trip() -> None:
    _use(global_event_crc=True)
    try:
        doc = GtabDocument()
        assert doc.event_crc_enabled is False
        assert force_event_crc(doc) is True
        blob = doc.to_bytes()
        again = GtabDocument.from_bytes(blob)
        assert again.event_crc_enabled is True
    finally:
        _defaults()


def test_force_event_crc_on_gct_round_trip() -> None:
    _use(global_event_crc=True)
    try:
        doc = demo_document()
        assert doc.event_crc_enabled is False
        assert force_event_crc(doc) is True
        blob = doc.to_bytes()
        again = GctDocument.from_bytes(blob)
        assert again.event_crc_enabled is True
    finally:
        _defaults()


def test_force_event_crc_none_and_duck_typed() -> None:
    _use(global_event_crc=True)
    try:
        assert force_event_crc(None) is False
        # 没有 event_crc_enabled 属性的文档退回改 flags 的 bit0
        duck = SimpleNamespace(flags=0)
        assert force_event_crc(duck) is True
        assert duck.flags & 0x01
        # 连 flags 都没有的对象：安静地不做事
        assert force_event_crc(object()) is False
    finally:
        _defaults()


# ---------------------------------------------------------------- 编辑页不再有 CRC 开关


def test_edit_pages_have_no_per_document_crc_switch() -> None:
    """「单独的事件 CRC」已删除：两个编辑页只受设置页的全局开关控制。"""
    _defaults()
    try:
        gtab_view = GtabPage(page=None)
        gct_view = GctPage(page=None)
        assert not hasattr(gtab_view, "_sw_crc")
        assert not hasattr(gct_view, "_sw_crc")
        assert not hasattr(gtab_view, "apply_settings")
        assert not hasattr(gct_view, "apply_settings")
        # 文档 flags 仍能正常读写（保存逻辑靠它）
        assert gtab_view.doc.event_crc_enabled is False
        assert gct_view.doc.event_crc_enabled is False
    finally:
        _defaults()


# ---------------------------------------------------------------- 保存链路（页面级）

# 这一组是「真机验收」的离线等价物：走**页面自己的保存逻辑**（_on_save →
# force_event_crc → to_bytes → 写盘），再把文件当外部文件重新读回来。
# page=None 是故意的：顺带守住「离线路径调 _snack 不能崩」这条（GtabPage 曾经缺守卫）。

SAVED_GTAB = HERE / "_crc_save.gtab"
SAVED_GCT = HERE / "_crc_save.gct"


def test_gtab_page_save_forces_global_crc_end_to_end() -> None:
    _use(global_event_crc=True)
    try:
        view = GtabPage(page=None)
        view.doc = GtabDocument.from_path(GtabPage.DEMO_PATH)
        view.path = SAVED_GTAB
        assert view.doc.event_crc_enabled is False, "载入的示例谱本身不带 CRC"

        asyncio.run(view._on_save())  # page=None 也不能崩（保存完要弹提示）
        assert SAVED_GTAB.exists()

        again = GtabDocument.from_bytes(SAVED_GTAB.read_bytes())
        assert again.event_crc_enabled is True, "保存出的字节里 flags bit0 必须是 1"
        assert len(again.events) == len(view.doc.events), "内容不能丢"
        assert again.bpm == view.doc.bpm
    finally:
        SAVED_GTAB.unlink(missing_ok=True)
        _defaults()


def test_gct_page_save_forces_global_crc_end_to_end() -> None:
    _use(global_event_crc=True)
    try:
        view = GctPage(page=None)
        view.doc = demo_document()
        view.path = SAVED_GCT
        assert view.doc.event_crc_enabled is False

        asyncio.run(view._on_save())
        assert SAVED_GCT.exists()

        again = GctDocument.from_bytes(SAVED_GCT.read_bytes())
        assert again.event_crc_enabled is True
        assert len(again.chord_events()) == len(view.doc.chord_events())
    finally:
        SAVED_GCT.unlink(missing_ok=True)
        _defaults()


def test_page_save_keeps_crc_off_when_setting_off() -> None:
    """全局关着时，保存不能偷偷加 CRC（否则游戏可能读不了）。"""
    _defaults()
    try:
        view = GtabPage(page=None)
        view.doc = GtabDocument.from_path(GtabPage.DEMO_PATH)
        view.path = SAVED_GTAB
        asyncio.run(view._on_save())

        again = GtabDocument.from_bytes(SAVED_GTAB.read_bytes())
        assert again.event_crc_enabled is False
    finally:
        SAVED_GTAB.unlink(missing_ok=True)
        _defaults()


def test_unknown_page_snack_without_page_is_silent() -> None:
    """离线构建（page=None）时任何提示都只丢弃，不抛异常。"""
    _defaults()
    view = GtabPage(page=None)
    view._snack("离线提示")
    view._snack("离线错误", error=True)


# ---------------------------------------------------------------- 设置页


def test_settings_page_builds_and_mirrors_settings() -> None:
    _defaults()
    try:
        _use(global_event_crc=True)
        view = SettingsPage(page=None)
        assert view.TITLE == "设置"
        assert view._version_text.value == "版本 1.0.0-rc1"
        assert view._sw_crc.value is True
        assert view._sw_project.value is False
        assert view.build() is not None
        assert SETTINGS_FILE.name == "settings.json"

        _use()
        view.refresh()
        assert view._sw_crc.value is False
    finally:
        _defaults()


def test_settings_page_toggle_persists_and_notifies() -> None:
    _defaults()
    _patch_file()
    seen: list[AppSettings] = []
    try:
        view = SettingsPage(page=None)
        view.on_change = seen.append
        view._sw_project.value = True
        view._on_project_change()
        assert seen and seen[-1].midi_project_octave is True
        assert load_settings(TEST_FILE).midi_project_octave is True
        assert view._status.value == "已保存"
    finally:
        _defaults()


def test_settings_page_reset_turns_both_off() -> None:
    _defaults()
    _patch_file()
    try:
        _use(global_event_crc=True, midi_project_octave=True)
        view = SettingsPage(page=None)
        view._on_reset()
        assert view._sw_crc.value is False
        assert view._sw_project.value is False
        assert load_settings(TEST_FILE) == DEFAULTS
    finally:
        _defaults()


# ---------------------------------------------------------------- 八度投影


def test_project_note_moves_by_octaves() -> None:
    table = load_key_table()
    assert table.base_note == 52 and table.last_note == 87
    assert project_note(48, table) == 60, "C3 往上一个八度到 C4"
    assert project_note(88, table) == 76, "高于窗口顶的音往下一个八度"
    assert project_note(60, table) == 60, "已经在窗口里就不动"
    assert project_note(60, table, transpose=12) == 72, "移调先加再投影"


def test_project_note_returns_none_when_impossible() -> None:
    narrow = parse_key_table({"BASE_NOTE": 60, "KEYS": ["q", "w", "e"]})  # 只有 3 个半音
    assert project_note(61, narrow) == 61
    # 54 加上任意个八度都落不进 60..62
    assert project_note(54, narrow) is None
    # 空键位表在 parse_key_table 就被拒了，不会跑到投影里
    try:
        parse_key_table({"BASE_NOTE": 60, "KEYS": []})
    except ValueError:
        pass
    else:  # pragma: no cover - 只要它还会拒空表就行
        raise AssertionError("KEYS 为空应该报错")


def test_build_key_schedule_projects_out_of_window_notes() -> None:
    doc = MidiDocument(
        format=0,
        tracks=1,
        division=480,
        notes=[NoteEvent(0, 480, 48), NoteEvent(480, 960, 60)],
        tempos=[TempoMark(0, DEFAULT_TEMPO_US)],
        end_tick=960,
    )
    table = load_key_table()
    plain = build_key_schedule(doc, table)
    assert plain.skipped == ["C3"] and plain.note_count == 1
    assert plain.projected == []

    projected = build_key_schedule(doc, table, project_octave=True)
    assert projected.skipped == []
    assert projected.projected == ["C3→C4"]
    assert projected.note_count == 2
    # 投影后的 C3 和真正的 C4 落在同一个键上
    presses = [action for action in projected.actions if action.kind == "press"]
    assert [action.key for action in presses] == ["l", "l"]
    assert presses[0].name == "C3", "记录里仍写原音名"


def test_build_key_schedule_projection_falls_back_to_skip() -> None:
    doc = MidiDocument(
        format=0,
        tracks=1,
        division=480,
        notes=[NoteEvent(0, 480, 54)],
        tempos=[TempoMark(0, DEFAULT_TEMPO_US)],
        end_tick=480,
    )
    narrow = parse_key_table({"BASE_NOTE": 60, "KEYS": ["q", "w", "e"]})
    schedule = build_key_schedule(doc, narrow, project_octave=True)
    assert schedule.projected == [], "落不进窗口的音不能假装投影成功"
    assert schedule.skipped == ["F#3"]


def test_midi_page_projection_blocks_and_no_auto_transpose() -> None:
    _defaults()
    try:
        _use(midi_project_octave=True)
        view = MidiPage(None)
        assert view.project_octave is True
        view._set_doc(midi_demo())
        assert view.capo == 0, "不自动移调，载入后 Capo 一定是 0"
        blocks = view._note_blocks()
        assert len(blocks) == 14
        assert all(block[5] for block in blocks), "投影后不该再有灰色块"
        assert "投影" in view._project_hint.value
    finally:
        _defaults()


def test_midi_page_projection_offline_play_reports_projected() -> None:
    _defaults()
    try:
        _use(midi_project_octave=True)
        view = MidiPage(FakePage())
        view._set_doc(midi_demo())
        asyncio.run(view._on_play())
        assert view._playing is True
        assert view._warning.visible is True
        assert "投影" in view._warning.value
        assert "跳过" not in view._warning.value
        assert "0/14" in view._progress.value, "14 个音全部发得出去"
        view.stop()
    finally:
        _defaults()


def test_midi_page_apply_settings_toggles_projection() -> None:
    _defaults()
    try:
        view = MidiPage(None)
        view._set_doc(midi_demo())
        assert view.project_octave is False
        assert view.capo == 0, "载入后 Capo 0"

        view._dd_capo.value = "2"
        view._on_capo_change()
        assert view.capo == 2

        _use(midi_project_octave=True)
        view.apply_settings(update=False)
        assert view.project_octave is True
        assert view.capo == 2, "投影开关变化不能冲掉手选的 Capo"

        _use(midi_project_octave=False)
        view.apply_settings(update=False)
        assert view.project_octave is False
        assert view.capo == 2, "关投影也不该改 Capo"
    finally:
        _defaults()


class FakeShellPage:
    """够 ``app.main`` 搭壳用的假 Page（不启动 Flet 运行时、不弹窗）。"""

    def __init__(self) -> None:
        self.title = ""
        self.padding = None
        self.theme_mode = None
        self.theme = None
        self.window = SimpleNamespace(width=None, height=None, min_width=None, min_height=None)
        self.controls: list[object] = []
        self.updates = 0

    def add(self, *controls) -> None:
        self.controls.extend(controls)

    def update(self) -> None:
        self.updates += 1


def test_app_shell_builds_with_real_settings_page() -> None:
    """主壳第 5 页必须是真的设置页（不再是 PlaceholderPage）。"""
    _defaults()
    try:
        import app

        page = FakeShellPage()
        app.main(page)
        assert page.controls, "主壳应该往 page 里放一个 Row"
        rail = page.controls[0].controls[0]
        labels = [destination.label for destination in rail.destinations]
        assert labels == ["GTAB 编辑", "GCT 编辑", "手柄映射", "MIDI 映射", "设置"]
        assert not hasattr(app, "PlaceholderPage"), "设置页已替换成真实页面"
        # 窗口图标：主壳启动时就要设上（Windows 读 .ico）
        assert str(page.window.icon).endswith("AkaneRin.ico")
        assert Path(page.window.icon).is_file()
    finally:
        _defaults()


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL  {test.__name__}: {exc!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)
