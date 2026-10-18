"""GCT 编辑页的文件操作测试：打开 / 保存 / 另存为 / 取消（用假 FilePicker，不弹窗口）。

为什么要单独一份：真机对话框没法在受限环境里点开（Agent 启动的窗口是 Low integrity），
但**对话框返回结果之后**的那段逻辑——路径怎么写回、脏标记怎么变、字节交给谁——
和对话框本身无关，可以在这里逐条固定住。真机上只剩「原生对话框能不能弹出来」这一件事。

``FakePicker.save_file`` 故意照桌面端 Flet 的行为实现：**把 ``src_bytes`` 写进目标文件**
（见 ``flet/controls/services/file_picker.py`` 的 ``save_file`` 说明）。页面因此不需要
自己再写一次字节；测试在这里同步这个契约，Flet 哪天改行为会先在测试里露出来。

运行：``.venv\\Scripts\\python.exe tests/test_gct_file_ops.py``
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from ui.gct_doc import GctDocument  # noqa: E402
from ui.pages.gct_page import GctPage  # noqa: E402

# 不用 tempfile：受限环境里 %TEMP% 和「新建的临时子目录」可能写不进去
TESTS = Path(__file__).resolve().parent
OPEN_FILE = TESTS / "_file_ops_open.gct"
SAVE_AS_FILE = TESTS / "_file_ops_saveas.gct"
BAD_FILE = TESTS / "_file_ops_bad.gct"


class FakePicker:
    """够用的假 FilePicker：记录调用参数，按设定返回结果。"""

    def __init__(self, files=None, save_path: Path | None = None) -> None:
        self.files = files
        self.save_path = save_path
        self.pick_kwargs: dict = {}
        self.save_kwargs: list[dict] = []

    async def pick_files(self, **kwargs):
        self.pick_kwargs = kwargs
        return self.files

    async def save_file(self, **kwargs):
        self.save_kwargs.append(kwargs)
        if self.save_path is None:  # 用户点了取消
            return None
        blob = kwargs.get("src_bytes")
        if blob is not None:
            Path(self.save_path).write_bytes(blob)
        return str(self.save_path)


def _picked(path: Path) -> SimpleNamespace:
    return SimpleNamespace(name=path.name, path=str(path), bytes=None)


def _make_file() -> None:
    """造一个真实的 .gct：一条 Strum（和弦名取 chord.json 第一个）。"""
    maker = GctPage(page=None)
    maker._on_add_chord()
    maker.doc.save(OPEN_FILE)


def _page_with(picker: FakePicker) -> GctPage:
    view = GctPage(page=None)  # page=None：构建与渲染都不需要页面上下文
    view._picker = picker
    return view


def _run(coro) -> None:
    asyncio.run(coro)


# ---------------------------------------------------------------- 打开


def test_open_loads_picked_file_and_resets_state() -> None:
    try:
        _make_file()
        picker = FakePicker(files=[_picked(OPEN_FILE)])
        view = _page_with(picker)
        view.dirty = True

        _run(view._on_open())

        assert view.path == OPEN_FILE, "打开后 path 要指向所选文件，保存才会写回它"
        assert view.dirty is False, "刚打开不算改动"
        assert len(view.doc.chord_events()) == 1
        assert picker.pick_kwargs["allowed_extensions"] == ["gct"]
        assert picker.pick_kwargs["allow_multiple"] is False
    finally:
        OPEN_FILE.unlink(missing_ok=True)


def test_open_accepts_inline_bytes_without_path() -> None:
    try:
        _make_file()
        blob = OPEN_FILE.read_bytes()
    finally:
        OPEN_FILE.unlink(missing_ok=True)

    picker = FakePicker(files=[SimpleNamespace(name="mem.gct", path=None, bytes=blob)])
    view = _page_with(picker)
    _run(view._on_open())

    assert len(view.doc.chord_events()) == 1, "拿不到 path 时也要能从 bytes 载入"
    assert view.path is None, "没有 path 就只能另存为"
    assert view.dirty is False


def test_open_bad_file_keeps_current_document() -> None:
    try:
        BAD_FILE.write_bytes(b"not a gct file at all")
        view = _page_with(FakePicker(files=[_picked(BAD_FILE)]))
        before = list(view.doc.events)
        _run(view._on_open())
        assert view.doc.events == before, "解析失败不能把当前文档换掉"
        assert view.path is None
    finally:
        BAD_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------- 保存


def test_save_writes_current_path_and_appends_end() -> None:
    try:
        _make_file()
        view = _page_with(FakePicker(files=[_picked(OPEN_FILE)]))
        _run(view._on_open())

        view._on_add_chord()
        assert view.dirty is True
        _run(view._on_save())

        assert view.dirty is False
        again = GctDocument.from_path(OPEN_FILE)
        assert len(again.chord_events()) == 2, "保存后的文件要能被自己重新读出来"
        assert again.events[-1].event_type == 0x05, "保存时自动补 End"
    finally:
        OPEN_FILE.unlink(missing_ok=True)


def test_save_without_path_falls_back_to_save_as() -> None:
    try:
        view = _page_with(FakePicker(save_path=SAVE_AS_FILE))
        view._on_add_chord()
        _run(view._on_save())

        assert view.path == SAVE_AS_FILE, "没有 path 时「保存」应直接转到另存为"
        assert len(view._picker.save_kwargs) == 1
        assert SAVE_AS_FILE.exists()
    finally:
        SAVE_AS_FILE.unlink(missing_ok=True)


def test_save_as_passes_bytes_and_updates_path() -> None:
    try:
        view = _page_with(FakePicker(save_path=SAVE_AS_FILE))
        expected = view.doc.to_bytes()  # 空谱只有 End
        view._on_add_chord()

        _run(view._on_save_as())

        kwargs = view._picker.save_kwargs[0]
        assert isinstance(kwargs["src_bytes"], bytes)
        assert kwargs["file_name"] == "untitled.gct", "没打开过文件时的默认名"
        assert kwargs["allowed_extensions"] == ["gct"]
        assert view.path == SAVE_AS_FILE
        assert view.dirty is False
        assert SAVE_AS_FILE.read_bytes() == view.doc.to_bytes()
        assert SAVE_AS_FILE.read_bytes() != expected, "另存为写的是当前内容，不是建页时的空谱"
    finally:
        SAVE_AS_FILE.unlink(missing_ok=True)


def test_save_as_cancel_keeps_path_and_dirty() -> None:
    view = _page_with(FakePicker(save_path=None))
    view._on_add_chord()
    _run(view._on_save_as())

    assert view.path is None, "取消不该记住一个不存在的路径"
    assert view.dirty is True, "取消后改动还在，不能清脏标记"


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
