from pathlib import Path

from gtab import GtabWriter, GtabReader, EV_STRING_NOTE_ON, EV_STRING_NOTE_OFF

# gtab 的产出统一放到 src/MusicList/gtabfile/ 下
OUT_DIR = Path(__file__).resolve().parent / "MusicList" / "gtabfile"
OUT_PATH = OUT_DIR / "demo.gtab"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ---- 写入 ----
w = GtabWriter(string_count=6, max_fret=24,
               time_base_type=0, time_base_value=480,
               default_tempo_us=500000, flags=0)

w.add_meta(0x0001, "standard")     # tuning_name
w.add_meta(0x0002, "Demo Song")    # title
w.add_meta(0x0004, bytes([0]))     # capo = 0

w.note_on(delta=0,   string=6, fret=0)   # 低音 E 空弦
w.note_on(delta=240, string=5, fret=2)   # A 弦第 2 品
w.note_off(delta=240, string=6, fret=0)
w.note_off(delta=0,   string=5, fret=2)
w.end(delta=0)

w.write(str(OUT_PATH))

# ---- 读取 ----
with GtabReader(str(OUT_PATH)) as r:
    print("header:", r.header)
    print("meta tuning:", r.meta.get(0x0001, [b""])[0].decode("utf-8", "replace"))
    for ev in r.events():
        if ev.event_type == EV_STRING_NOTE_ON:
            s, f = ev.data
            print(f"tick {ev.tick:6d}  NoteOn   string={s} fret={f} voice={ev.voice}")
        elif ev.event_type == EV_STRING_NOTE_OFF:
            s, f = ev.data
            print(f"tick {ev.tick:6d}  NoteOff  string={s} fret={f} voice={ev.voice}")
        else:
            print(f"tick {ev.tick:6d}  type=0x{ev.event_type:02X} data={ev.data.hex()}")