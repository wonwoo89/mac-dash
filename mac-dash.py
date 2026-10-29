#!/usr/bin/env python3
"""mac-dash - real-time CPU / GPU / memory TUI dashboard for macOS.

Runs without sudo and without dependencies (standard library curses + ctypes only).

Data sources
    Memory   vm_stat, memory_pressure, sysctl vm.swapusage
             -> same accounting as Activity Monitor: Used = App + Wired + Compressed
    CPU      per-core tick deltas from host_processor_info (ctypes), sysctl hw.perflevel*
             -> on Apple Silicon the mach core order is E-cores first, then P-cores
    GPU      PerformanceStatistics of ioreg AGXAccelerator (Device/Renderer/Tiler %)
    Optional if macmon is on PATH, a background stream adds frequency, power,
             temperature and fan data (without it those fields show '-' and
             everything else works as usual)

Views: 1 all · 2 overview · 3 memory · 4 CPU
"""

import argparse
import atexit
import ctypes
import ctypes.util
import curses
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata
from ctypes import POINTER, byref, c_int, c_size_t, c_uint, c_void_p

__version__ = "0.1.0"

# Border styles.
#
# The thin line (U+2500 ─) has a stroke of only 71/1000 em = 1.3px at 18px in this
# font, so macOS subpixel rendering and translucent background blending wash it out.
# The vertical line (│) has the same thickness but survives because it is a vertical
# stroke, which makes the box look like "verticals visible, horizontals broken".
# heavy is 2.5px and double is 3.8px, so they are safe. That is why heavy was the default.
BORDER_STYLES = {
    # solid: builds the border from █ (U+2588) only.
    # Some terminals render every partial block (▁▔▏▕) and line character (─━) as ◇,
    # but full blocks still render fine there (bars drawing correctly proves it).
    # The most reliable option, but the border looks thick.
    "solid":   {"tl": "█", "tr": "█", "bl": "█", "br": "█", "h": "█", "v": "█"},
    # block: uses block characters only, no line characters (U+2500 range) at all.
    # Some terminals draw line characters with their own renderer and they come out
    # jagged, while block characters are plain filled rectangles and are always straight
    # wherever bars (█) render well.
    # The top uses ▁ (ink at the bottom of the cell) and the bottom uses ▔ (ink at the
    # top), so the border hugs the content exactly.
    "block":   {"tl": "▁", "tr": "▁", "bl": "▔", "br": "▔", "h": "▁",
                "hb": "▔", "v": "▏", "vr": "▕"},
    "heavy":   {"tl": "┏", "tr": "┓", "bl": "┗", "br": "┛", "h": "━", "v": "┃"},
    "double":  {"tl": "╔", "tr": "╗", "bl": "╚", "br": "╝", "h": "═", "v": "║"},
    "rounded": {"tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│"},
    "square":  {"tl": "┌", "tr": "┐", "bl": "└", "br": "┘", "h": "─", "v": "│"},
    "ascii":   {"tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|"},
    "none":    {"tl": " ", "tr": " ", "bl": " ", "br": " ", "h": " ", "v": " "},
}

# A middle dot (·) as the bar track is faint too: its ink covers only 29% of the cell.
# ░ is a dither pattern spread over the whole cell, so it does not have that problem.
GLYPH_UNICODE = {"block": "█", "track": "░", "dash": "▬", "tick": "▏",
                 "mark": "◀", "deg": "°", "spark": "▁▁▂▃▄▅▆▇█"}
GLYPH_ASCII = {"block": "#", "track": ".", "dash": "=", "tick": "|",
               "mark": "<", "deg": "", "spark": ".:-=+*#@@",
               "tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|"}
G = dict(GLYPH_UNICODE)
G.update(BORDER_STYLES["rounded"])
FORCE_DIM_ATTR = False   # --dim-attr forces the old behavior (SGR 2)

PRESSURE_LEVEL = {1: ("NORMAL", "ok"), 2: ("WARN", "warn"), 4: ("CRITICAL", "crit")}
UNIT = {"B": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}
CPU_STATE_MAX = 4          # user, system, idle, nice
PROCESSOR_CPU_LOAD_INFO = 2


# ==================================================================== i18n

# User-facing text. Box titles and column headers stay ASCII-only on purpose
# (see procs_title), so only the strings below are translated.
MESSAGES = {
    "en": {
        "desc": "Real-time CPU/GPU/memory TUI dashboard for macOS",
        "h_interval": "refresh interval in seconds (default 2.0)",
        "h_once": "print the current state once without the TUI",
        "h_group": "aggregate processes by name",
        "h_sort": "process sort key",
        "h_view": "initial view (default all: CPU and memory details together)",
        "h_gap": "spacing (tight/normal/loose, default tight)",
        "h_keep_term": "keep TERM as is (disables the ncurses REP bug workaround)",
        "h_border": "border style. use solid if partial blocks break, none to hide borders",
        "h_dim_attr": "draw secondary text with SGR 2 (dim) instead of palette color 8",
        "h_ascii": "draw bars and sparklines with ASCII characters (non-Unicode terminals)",
        "h_no_macmon": "disable macmon integration (no freq/power/temp, lower CPU usage)",
        "h_top_every": "refresh root-owned process memory via top every N ticks (default 5)",
        "h_lang": "UI language (default: auto-detect from locale)",
        "gpu_na": "utilization unavailable (no AGXAccelerator in ioreg)",
        "tab_all": "all", "tab_overview": "overview", "tab_mem": "memory", "tab_cpu": "CPU",
        "key_sort": "sort", "key_group": "group", "key_pause": "pause", "key_quit": "quit",
        "once_compressed": "%.2fx, %s stored compressed",
        "once_reclaimable": "reclaimable",
    },
    "ko": {
        "desc": "macOS CPU/GPU/메모리 실시간 TUI 대시보드",
        "h_interval": "갱신 주기(초, 기본 2.0)",
        "h_once": "TUI 없이 현재 상태만 출력",
        "h_group": "프로세스를 이름별로 합산",
        "h_sort": "프로세스 정렬 기준",
        "h_view": "시작 뷰 (기본 all: CPU·메모리 상세 동시 표시)",
        "h_gap": "여백 (tight/normal/loose, 기본 tight)",
        "h_keep_term": "TERM 을 그대로 둔다 (ncurses REP 버그 회피를 끔)",
        "h_border": "테두리 스타일. 부분블록도 깨지면 solid, 아예 끄려면 none",
        "h_dim_attr": "보조 텍스트를 팔레트 색 8 대신 SGR 2(dim)로 그린다",
        "h_ascii": "막대·스파크라인을 ASCII 문자로 그린다 (유니코드 미지원 터미널)",
        "h_no_macmon": "macmon 연동을 끈다 (주파수·전력·온도 미표시, CPU 사용 감소)",
        "h_top_every": "root 소유 프로세스 메모리를 N 회마다 top 으로 보강 (기본 5)",
        "h_lang": "UI 언어 (기본: 로케일에서 자동 감지)",
        "gpu_na": "사용률을 읽을 수 없음 (ioreg 에 AGXAccelerator 없음)",
        "tab_all": "전체", "tab_overview": "개요", "tab_mem": "메모리", "tab_cpu": "CPU",
        "key_sort": "정렬", "key_group": "합계", "key_pause": "정지", "key_quit": "종료",
        "once_compressed": "%.2fx, %s 를 압축 보관",
        "once_reclaimable": "회수 가능",
    },
}
LANG = "en"


def t(key):
    return MESSAGES[LANG].get(key) or MESSAGES["en"][key]


def detect_lang(argv):
    """--lang > MAC_DASH_LANG > LC_ALL/LC_MESSAGES > macOS system language > LANG.

    macOS terminals usually export LANG=en_US.UTF-8 even when the system language
    is Korean, so the system language (AppleLanguages) is checked before LANG.
    """
    for i, a in enumerate(argv):
        if a == "--lang" and i + 1 < len(argv):
            v = argv[i + 1]
        elif a.startswith("--lang="):
            v = a.split("=", 1)[1]
        else:
            continue
        if v in MESSAGES:
            return v
    v = os.environ.get("MAC_DASH_LANG")
    if v in MESSAGES:
        return v
    for var in ("LC_ALL", "LC_MESSAGES"):
        v = os.environ.get(var)
        if v:
            return "ko" if v.lower().startswith("ko") else "en"
    m = re.search(r"([A-Za-z]{2})", sh(["defaults", "read", "-g", "AppleLanguages"], timeout=2))
    if m:
        return "ko" if m.group(1).lower() == "ko" else "en"
    return "ko" if os.environ.get("LANG", "").lower().startswith("ko") else "en"


# ==================================================================== utils

def sh(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def sysctl(name):
    return sh(["sysctl", "-n", name]).strip()


def sysctl_int(name, default=0):
    v = sysctl(name)
    try:
        return int(v)
    except ValueError:
        return default


def human(n):
    """Bytes -> GB/MB. Base 1024, same as Activity Monitor."""
    if n is None:
        return "-"
    for unit, div in (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if abs(n) >= div:
            return "%.2f %s" % (n / div, unit)
    return "%d B" % n


def dwidth(s):
    """Display width in the terminal. Hangul/CJK characters take 2 cells."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def dtrim(s, limit):
    """Trim by display width (never splits a full-width character in half)."""
    if limit <= 0:
        return ""
    out, used = [], 0
    for c in s:
        cw = 2 if unicodedata.east_asian_width(c) in "WF" else 1
        if used + cw > limit:
            break
        out.append(c)
        used += cw
    return "".join(out)


# ======================================================= optional macmon feed

class MacmonFeed:
    """If macmon exists, read its JSON stream in the background and keep the latest sample.

    Frequency, power, temperature and fans require IOReport, which is awkward to reach
    from pure Python without sudo, so an installed macmon is reused as is. Optional.
    """

    def __init__(self, interval_ms=1000):
        self.data = None
        self.proc = None
        self.available = shutil.which("macmon") is not None
        if not self.available:
            return
        self.proc = subprocess.Popen(
            ["macmon", "pipe", "-i", str(interval_ms)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        atexit.register(self.stop)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.data = json.loads(line)
                except ValueError:
                    pass
        except Exception:
            pass

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=1)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass


# ==================================================================== CPU

libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
libc.mach_host_self.restype = c_uint
libc.mach_task_self.restype = c_uint
libc.host_processor_info.argtypes = [
    c_uint, c_int, POINTER(c_uint), POINTER(POINTER(c_int)), POINTER(c_uint)]
libc.host_processor_info.restype = c_int
libc.vm_deallocate.argtypes = [c_uint, c_void_p, c_size_t]


def core_ticks():
    """Cumulative (user, system, idle, nice) ticks per core. None on failure."""
    ncpu, cnt = c_uint(0), c_uint(0)
    info = POINTER(c_int)()
    if libc.host_processor_info(libc.mach_host_self(), PROCESSOR_CPU_LOAD_INFO,
                               byref(ncpu), byref(info), byref(cnt)) != 0:
        return None
    try:
        return [tuple(info[i * CPU_STATE_MAX + j] for j in range(CPU_STATE_MAX))
                for i in range(ncpu.value)]
    finally:
        libc.vm_deallocate(libc.mach_task_self(), ctypes.cast(info, c_void_p),
                           c_size_t(cnt.value * ctypes.sizeof(c_int)))


def core_layout():
    """[(label, [core index...]), ...]

    On Apple Silicon the mach processor order is Efficiency (perflevel1) first,
    then Performance (perflevel0). Verified by saturating 8 cores.
    """
    ncpu = sysctl_int("hw.ncpu", 1)
    if sysctl_int("hw.nperflevels", 1) >= 2:
        e_n = sysctl_int("hw.perflevel1.logicalcpu", 0)
        p_n = sysctl_int("hw.perflevel0.logicalcpu", 0)
        if e_n and p_n and e_n + p_n <= ncpu:
            return [("E", list(range(e_n))), ("P", list(range(e_n, e_n + p_n)))]
    return [("CPU", list(range(ncpu)))]


class CPUSampler:
    def __init__(self):
        self.prev = core_ticks()
        self.layout = core_layout()

    def sample(self):
        """List of per-core busy ratios (0~1). Right after the first call, relative to the previous ticks."""
        cur = core_ticks()
        if cur is None:
            return []
        prev = self.prev
        self.prev = cur
        if prev is None or len(prev) != len(cur):
            return [0.0] * len(cur)
        out = []
        for c, p in zip(cur, prev):
            d = [c[i] - p[i] for i in range(CPU_STATE_MAX)]
            total = sum(d)
            busy = d[0] + d[1] + d[3]          # user + system + nice
            out.append((busy / total) if total > 0 else 0.0)
        return out


# ------------------------------------------------ process footprint (libproc)

class RUsageInfoV2(ctypes.Structure):
    """rusage_info_v2 from <sys/resource.h>. ri_phys_footprint equals MEM in top."""
    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64), ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64), ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64), ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64), ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64), ("ri_proc_exit_abstime", ctypes.c_uint64),
        ("ri_child_user_time", ctypes.c_uint64), ("ri_child_system_time", ctypes.c_uint64),
        ("ri_child_pkg_idle_wkups", ctypes.c_uint64), ("ri_child_interrupt_wkups", ctypes.c_uint64),
        ("ri_child_pageins", ctypes.c_uint64), ("ri_child_elapsed_abstime", ctypes.c_uint64),
        ("ri_diskio_bytesread", ctypes.c_uint64), ("ri_diskio_byteswritten", ctypes.c_uint64),
    ]


RUSAGE_INFO_V2 = 2
libc.proc_pid_rusage.argtypes = [c_int, c_int, POINTER(c_void_p)]
libc.proc_pid_rusage.restype = c_int


def footprint(pid):
    """Physical footprint of a process. None for processes owned by other users (no permission)."""
    buf = RUsageInfoV2()
    r = libc.proc_pid_rusage(pid, RUSAGE_INFO_V2,
                             ctypes.cast(byref(buf), POINTER(c_void_p)))
    return buf.ri_phys_footprint if r == 0 else None


def load_avg():
    m = re.findall(r"[\d.]+", sysctl("vm.loadavg"))
    return [float(x) for x in m[:3]] if len(m) >= 3 else [0.0, 0.0, 0.0]


# ==================================================================== GPU

def read_gpu():
    """GPU utilization and VRAM from ioreg. AGXAccelerator on Apple Silicon."""
    out = ""
    for cls in ("AGXAccelerator", "IOAccelerator"):
        out = sh(["ioreg", "-r", "-d", "1", "-w", "0", "-c", cls])
        if "PerformanceStatistics" in out:
            break
    if "PerformanceStatistics" not in out:
        return None

    m = re.search(r'"PerformanceStatistics" = \{(.*?)\}', out, re.S)
    stats = {}
    if m:
        for k, v in re.findall(r'"([^"]+)"=(\d+)', m.group(1)):
            stats[k] = int(v)

    def first_int(pattern):
        mm = re.search(pattern, out)
        return int(mm.group(1)) if mm else None

    model = re.search(r'"model" = "([^"]+)"', out)
    return {
        "util": stats.get("Device Utilization %"),
        "renderer": stats.get("Renderer Utilization %"),
        "tiler": stats.get("Tiler Utilization %"),
        "vram_used": stats.get("In use system memory"),
        "vram_alloc": stats.get("Alloc system memory"),
        "cores": first_int(r'"gpu-core-count" = (\d+)'),
        "model": model.group(1) if model else None,
    }


# ================================================================== memory

def read_vm_stat():
    out = sh(["vm_stat"])
    m = re.search(r"page size of (\d+) bytes", out)
    pagesize = int(m.group(1)) if m else 4096
    d = {}
    for line in out.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        v = v.strip().rstrip(".")
        if v.isdigit():
            d[k.strip().strip('"')] = int(v)
    return pagesize, d


def read_swap():
    out = sysctl("vm.swapusage")
    vals = {}
    for key in ("total", "used", "free"):
        m = re.search(key + r"\s*=\s*([\d.]+)([BKMGT])", out)
        if m:
            vals[key] = int(float(m.group(1)) * UNIT[m.group(2)])
    return vals


def read_pressure():
    m = re.search(r"free percentage:\s*(\d+)", sh(["memory_pressure"]))
    free_pct = int(m.group(1)) if m else None
    try:
        name, key = PRESSURE_LEVEL[sysctl_int("kern.memorystatus_vm_pressure_level", 1)]
    except KeyError:
        name, key = "?", "ok"
    return free_pct, name, key


def read_memory():
    pagesize, vm = read_vm_stat()
    total = sysctl_int("hw.memsize")

    def pg(key):
        return vm.get(key, 0) * pagesize

    # Activity Monitor accounting: anonymous minus purgeable is App Memory,
    # file-backed + purgeable is Cached Files (immediately reclaimable).
    purgeable = pg("Pages purgeable")
    app = max(0, pg("Anonymous pages") - purgeable)
    wired = pg("Pages wired down")
    compressed = pg("Pages occupied by compressor")
    cached = pg("File-backed pages") + purgeable
    free = pg("Pages free")
    used = app + wired + compressed

    stored = vm.get("Pages stored in compressor", 0)
    occupied = vm.get("Pages occupied by compressor", 0)
    free_pct, level, level_key = read_pressure()
    return {
        "pagesize": pagesize, "total": total,
        "app": app, "wired": wired, "compressed": compressed,
        "cached": cached, "free": free, "used": used,
        "other": max(0, total - (used + cached + free)),
        "ratio": (stored / occupied) if occupied else 0.0,
        "stored": stored * pagesize, "occupied": occupied * pagesize,
        "swap": read_swap(),
        "free_pct": free_pct, "level": level, "level_key": level_key,
        "pageins": vm.get("Pageins", 0), "pageouts": vm.get("Pageouts", 0),
        "swapins": vm.get("Swapins", 0), "swapouts": vm.get("Swapouts", 0),
    }


# ================================================================ processes

def parse_cputime(tok):
    """ps TIME ('3:20.00' or '1:02:03.45') -> seconds."""
    parts = tok.split(":")
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        return 0.0
    secs = 0.0
    for v in vals:
        secs = secs * 60 + v
    return secs


def parse_top_mem(tok):
    m = re.match(r"([\d.]+)([BKMGT])", tok)
    return int(float(m.group(1)) * UNIT[m.group(2)]) if m else None


class ProcSampler:
    """Per-process memory footprint + CPU%.

    Memory is ri_phys_footprint from proc_pid_rusage (MEM in top, i.e. the same value
    as the 'Memory' column in Activity Monitor). Reads ~900 processes in 0.004s.
    ps RSS is not used because it double-counts shared memory and distorts the ranking.

    rusage, however, has no permission for processes owned by other users (root etc.).
    That is roughly a third of all processes, including top entries like WindowServer.
    So top runs only once every top_every ticks (0.46s) to fill in those values.
    Daemon memory changes slowly, so the delay does not matter.

    CPU% is the derivative of the cumulative CPU time from ps over the refresh interval
    (CPU% from top -l 1 is the average since process start, not an instantaneous value).
    """

    def __init__(self, top_every=5):
        self.top_every = max(1, top_every)
        self.tick = 0
        self.priv_mem = {}          # values for processes rusage cannot read (from top)
        self.prev_time = {}
        self.prev_ts = None

    def _refresh_priv(self):
        out = sh(["top", "-l", "1", "-n", "999", "-o", "mem", "-stats", "mem,pid"], timeout=10)
        found, started = {}, False
        for line in out.splitlines():
            parts = line.split()
            if not started:
                started = parts[:2] == ["MEM", "PID"]
                continue
            if len(parts) < 2 or not parts[1].isdigit():
                continue
            mem = parse_top_mem(parts[0])
            if mem is not None:
                found[parts[1]] = mem
        if found:
            self.priv_mem = found

    def sample(self):
        if self.tick % self.top_every == 0:
            self._refresh_priv()
        self.tick += 1

        now = time.monotonic()
        cputime, names = {}, {}
        for line in sh(["ps", "-Ao", "pid=,time=,comm="]).splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) < 3 or not parts[0].isdigit():
                continue
            pid, tm, path = parts
            cputime[pid] = parse_cputime(tm)
            names[pid] = path.rsplit("/", 1)[-1] or path

        dt = (now - self.prev_ts) if self.prev_ts else None
        cpu = {}
        if dt and dt > 0:
            for pid, t in cputime.items():
                prev = self.prev_time.get(pid)
                if prev is not None:
                    cpu[pid] = max(0.0, (t - prev) / dt * 100.0)
        self.prev_time, self.prev_ts = cputime, now

        rows = []
        for pid, name in names.items():
            mem = footprint(int(pid))
            if mem is None:                     # no permission -> use the value from top
                mem = self.priv_mem.get(pid)
            if mem is None:
                continue
            rows.append({"mem": mem, "pid": pid, "n": 1,
                         "name": name, "cpu": cpu.get(pid, 0.0)})
        return rows


def group_procs(rows):
    agg = {}
    for r in rows:
        a = agg.get(r["name"])
        if a:
            a["mem"] += r["mem"]
            a["cpu"] += r["cpu"]
            a["n"] += 1
        else:
            agg[r["name"]] = {"mem": r["mem"], "cpu": r["cpu"], "n": 1,
                              "name": r["name"], "pid": r["pid"]}
    return list(agg.values())


def sort_procs(rows, key):
    return sorted(rows, key=lambda r: r[key], reverse=True)


# ================================================================ rendering

C = {}
WIDE_COLS = 160          # at this width or more, CPU/GPU and PRESSURE/SWAP go side by side
PROC_MIN_H = 4           # minimum height reserved for the process box

# Spacing. Change with --gap or MAC_DASH_GAP.
#   tight  boxes touch and lists are one row per item (default)
#   normal one blank line between boxes
#   loose  blank lines between boxes and between list rows
BOX_GAP = 0              # blank lines between boxes
CPU_SUM_GAP = 1          # blank lines between the total CPU bar and the E/P sparklines
CPU_HEAD_GAP = 1         # blank lines between the CPU summary (E/P sparklines) and the per-core list
ROW_STEP = 1             # rows taken by one list item (2 leaves a blank line)
GAP_PRESETS = {"tight": (0, 1), "normal": (1, 1), "loose": (1, 2)}


def rows_for(n):
    """Rows taken by n list items (the blank line after the last item is not counted)."""
    return max(0, n * ROW_STEP - (ROW_STEP - 1))


CORE_COL_MIN = 25        # minimum column width when splitting E/P cores left/right (fits 60 cols)
CORE_COL_GAP = 3         # gap between the E/P columns
CORE_FREQ_MIN = 32       # below this column width, drop the clock (MHz) and give the bar the room


def core_split(w, st):
    """Is the width enough to split E/P cores into two columns?"""
    return len(st["layout"]) == 2 and w >= 2 * CORE_COL_MIN + CORE_COL_GAP


def core_rows(w, st):
    """Rows taken by the per-core list. With two columns, the core count of the larger group."""
    sizes = [len(i) for _l, i in st["layout"]]
    return rows_for(max(sizes) if core_split(w, st) else sum(sizes))


def init_colors():
    if not curses.has_colors():
        return
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    pairs = [
        ("app", curses.COLOR_CYAN), ("wired", curses.COLOR_RED),
        ("comp", curses.COLOR_YELLOW), ("cache", curses.COLOR_BLUE),
        ("ok", curses.COLOR_GREEN), ("accent", curses.COLOR_MAGENTA),
        ("warn", curses.COLOR_YELLOW), ("crit", curses.COLOR_RED),
        ("cpu", curses.COLOR_GREEN), ("gpu", curses.COLOR_MAGENTA),
    ]
    for i, (name, fg) in enumerate(pairs, start=1):
        curses.init_pair(i, fg, bg)
        C[name] = curses.color_pair(i)

    if not FORCE_DIM_ATTR and curses.COLORS >= 16:
        curses.init_pair(len(pairs) + 1, 8, bg)        # 8 = bright black
        C["dim"] = curses.color_pair(len(pairs) + 1)
    else:
        C["dim"] = curses.A_DIM
    C["free"] = C["dim"]


def col(name, extra=0):
    return C.get(name, 0) | extra


def heat(frac):
    return "ok" if frac < 0.5 else ("warn" if frac < 0.8 else "crit")


def put(win, y, x, s, attr=0):
    """Draw at (y, x) and return the next cursor x by display width."""
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w:
        return x
    s = dtrim(s, w - x - 1)
    if not s:
        return x
    try:
        win.addstr(y, x, s, attr)
    except curses.error:
        pass
    return x + dwidth(s)


def bar(win, y, x, width, frac, key=None):
    frac = max(0.0, min(1.0, frac))
    filled = int(round(width * frac))
    nx = put(win, y, x, G["block"] * filled, col(key or heat(frac)))
    if width - filled > 0:
        put(win, y, nx, G["track"] * (width - filled), col("dim"))
    return x + width


def spark(values, scale=1.0):
    sp = G["spark"]
    return "".join(sp[min(8, max(0, int(v / scale * 8)))] for v in values)


def box(win, y, x, w, h, title, key="accent"):
    """Draw a border and return (inner y, inner x, inner width).

    Anything past the screen is clipped, so callers need not worry about size up front.
    """
    sh_, sw = win.getmaxyx()
    h = max(3, min(h, sh_ - y - 1))
    w = max(8, min(w, sw - x - 1))
    hb = G.get("hb", G["h"])          # bottom horizontal char (block style differs top/bottom)
    vr = G.get("vr", G["v"])          # right vertical char
    put(win, y, x, G["tl"] + G["h"] * (w - 2) + G["tr"], col("dim"))
    put(win, y + h - 1, x, G["bl"] + hb * (w - 2) + G["br"], col("dim"))
    for i in range(1, h - 1):
        put(win, y + i, x, G["v"], col("dim"))
        put(win, y + i, x + w - 1, vr, col("dim"))
    if title:
        put(win, y, x + 2, " %s " % title, col(key, curses.A_BOLD))
    return y + 1, x + 2, w - 4


def rjust_in(win, y, x, w, text, attr=0, after=0):
    """Right-align inside the box. If there is not enough room, clip inside the box.

    It used to keep writing when it overlapped the left content and covered the right border.
    """
    start = max(after, x)
    avail = (x + w) - start
    if avail <= 0:
        return start
    if dwidth(text) > avail:
        text = dtrim(text, avail)
    return put(win, y, max(start, x + w - dwidth(text)), text, attr)


def stack_widths(width, values, total):
    if not total or width <= 0:
        return [0] * len(values)
    counts = [int(round(width * v / total)) for v in values]
    while sum(counts) > width:
        counts[counts.index(max(counts))] -= 1
    return counts


def draw_mem_stack(win, y, x, width, m):
    segs = [(m["app"], "app"), (m["wired"], "wired"),
            (m["compressed"], "comp"), (m["cached"], "cache")]
    counts = stack_widths(width, [v for v, _ in segs], m["total"])
    cx = x
    for count, (_v, key) in zip(counts, segs):
        if count > 0:
            cx = put(win, y, cx, G["block"] * count, col(key))
    if width - (cx - x) > 0:
        put(win, y, cx, G["track"] * (width - (cx - x)), col("dim"))


# ------------------------------------------------------------------- panels
# Every panel takes (win, y, x, w, ...), draws at box-inner coordinates and returns the rows used.

def panel_cpu(win, y, x, w, st):
    cores, mac = st["cores"], st["mac"]
    total = (sum(cores) / len(cores)) if cores else 0.0

    put(win, y, x, "%5.1f%%" % (total * 100), col(heat(total), curses.A_BOLD))
    bw = max(8, min(26, w - 34))
    nx = bar(win, y, x + 7, bw, total) + 2
    if mac:
        items = [("%d/%d MHz" % (mac.get("pcpu_freq_mhz", 0), mac.get("ecpu_freq_mhz", 0)),
                  col("dim")),
                 ("%.2f W" % mac.get("cpu_power", 0.0), col("dim"))]
        t = (mac.get("temp") or {}).get("cpu_temp_avg")
        if t:
            items.append(("%.0f%sC" % (t, G["deg"]), col(heat(min(1.0, t / 100.0)))))
        # items that would overflow the box are dropped whole (so the right border is not covered)
        for text, attr in items:
            if nx + dwidth(text) > x + w:
                break
            nx = put(win, y, nx, text, attr) + 2
    y += 1 + CPU_SUM_GAP

    cx = x
    for label, idxs in st["layout"]:
        vals = [cores[i] for i in idxs if i < len(cores)]
        if not vals:
            continue
        avg = sum(vals) / len(vals)
        cx = put(win, y, cx, "%s " % label, col("dim"))
        cx = put(win, y, cx, spark(vals), col(heat(avg)))
        cx = put(win, y, cx, " %5.1f%%   " % (avg * 100), col("dim"))
    rjust_in(win, y, x, w, "load %.2f %.2f %.2f" % tuple(st["load"]), col("dim"), after=cx + 2)
    return 2 + CPU_SUM_GAP


def panel_cpu_cores(win, y, x, w, st, max_rows):
    """Per-core utilization + actual clock. Clipped so it never exceeds max_rows."""
    cores, mac = st["cores"], st["mac"]
    freqs = {}
    if mac:
        for gi, key in ((0, "ecpu_cores"), (1, "pcpu_cores")):
            if gi >= len(st["layout"]):
                continue
            idxs = st["layout"][gi][1]
            for i, c in enumerate(mac.get(key) or []):
                if i < len(idxs):
                    freqs[idxs[i]] = c.get("freq_mhz")

    # when wide enough, draw E cores in the left column and P cores in the right column
    if core_split(w, st):
        cw = (w - CORE_COL_GAP) // 2
        cols = [(x, cw, [g]) for g in st["layout"]]
        cols[1] = (x + cw + CORE_COL_GAP, w - cw - CORE_COL_GAP, cols[1][2])
    else:
        cols = [(x, w, st["layout"])]

    # The bar fills the remaining horizontal space.
    # Layout: label (x..x+11) | bar (x+12..) | 1 space | clock "%5d MHz" (9 cells)
    # The clock is only drawn with macmon, so room is reserved only then.
    total_used = 0
    for cx, cw, groups in cols:
        show_freq = freqs and cw >= CORE_FREQ_MIN
        reserve = 10 if show_freq else 1
        bw = max(4, cw - 12 - reserve)
        cy, used = y, 0
        for label, idxs in groups:
            for i in idxs:
                if i >= len(cores) or used + ROW_STEP > max_rows + (ROW_STEP - 1):
                    continue
                v = cores[i]
                put(win, cy, cx, "%s%-2d" % (label, i), col("dim"))
                put(win, cy, cx + 5, "%5.1f%%" % (v * 100), col(heat(v)))
                bar(win, cy, cx + 12, bw, v)
                f = freqs.get(i) if show_freq else None
                if f:
                    put(win, cy, cx + 13 + bw, "%5d MHz" % f, col("dim"))
                cy += ROW_STEP
                used += ROW_STEP
        total_used = max(total_used, used)
    return max(0, total_used - (ROW_STEP - 1))


def panel_gpu(win, y, x, w, st):
    g, mac = st["gpu"], st["mac"]
    if not g:
        put(win, y, x, t("gpu_na"), col("dim"))
        return 1

    util = g["util"] if g["util"] is not None else 0
    frac = util / 100.0
    put(win, y, x, "%5.1f%%" % util, col(heat(frac), curses.A_BOLD))
    bw = max(8, min(26, w - 24))
    nx = bar(win, y, x + 7, bw, frac, key="gpu") + 2
    if mac:
        nx = put(win, y, nx, "%.2f W" % mac.get("gpu_power", 0.0), col("dim")) + 2
        temp = (mac.get("temp") or {}).get("gpu_temp_avg")
        if temp:
            put(win, y, nx, "%.0f%sC" % (temp, G["deg"]), col(heat(min(1.0, temp / 100.0))))
    y += 1

    parts = []
    if mac:
        parts.append("%d MHz" % mac.get("gpu_freq_mhz", 0))
    parts += ["renderer %s%%" % g["renderer"], "tiler %s%%" % g["tiler"],
              "VRAM %s" % human(g["vram_used"])]
    put(win, y, x, "   ".join(parts), col("dim"))
    return 2


def panel_mem(win, y, x, w, m):
    used_pct = (100.0 * m["used"] / m["total"]) if m["total"] else 0
    put(win, y, x, "%5.1f%%" % used_pct, col(heat(used_pct / 100.0), curses.A_BOLD))
    nx = put(win, y, x + 7, "%s / %s" % (human(m["used"]), human(m["total"]))) + 3
    # when short on width, drop items from the end (better than clipping mid-item)
    items = [("app", m["app"]), ("wired", m["wired"]), ("comp", m["compressed"]),
             ("cached", m["cached"]), ("free", m["free"])]
    while items:
        legend = "   ".join("%s %s" % (k, human(v)) for k, v in items)
        if dwidth(legend) <= (x + w) - nx:
            break
        items.pop()
    if items:
        rjust_in(win, y, x, w, legend, col("dim"), after=nx)
    y += 1
    draw_mem_stack(win, y, x, w, m)
    return 2


def panel_mem_detail(win, y, x, w, m, max_rows):
    rows = [("App Memory", m["app"], "app"), ("Wired", m["wired"], "wired"),
            ("Compressed", m["compressed"], "comp"), ("Cached Files", m["cached"], "cache"),
            ("Free", m["free"], "free")]
    if m["other"] > 0:
        rows.append(("Kernel/other", m["other"], "dim"))
    barmax = max(8, min(30, w - 40))
    used = 0
    for lab, val, key in rows[:max(0, (max_rows + ROW_STEP - 1) // ROW_STEP)]:
        put(win, y, x, G["block"], col(key))
        put(win, y, x + 2, lab)
        put(win, y, x + 17, human(val).rjust(10))
        share = (val / m["total"]) if m["total"] else 0
        put(win, y, x + 29, "%5.1f%%" % (share * 100), col("dim"))
        put(win, y, x + 37, G["tick"] + G["dash"] * int(round(barmax * share)), col(key))
        y += ROW_STEP
        used += ROW_STEP
    return max(0, used - (ROW_STEP - 1))


def panel_pressure(win, y, x, w, m, hist):
    pct = (100 - m["free_pct"]) if m["free_pct"] is not None else 0
    lk = "warn" if (m["level_key"] == "ok" and pct >= 80) else m["level_key"]
    put(win, y, x, "%3d%%" % pct, col(lk, curses.A_BOLD))
    nx = put(win, y, x + 6, "[%s]" % m["level"], col(lk))
    gw = max(6, (x + w) - (nx + 3))
    s = spark(hist[-gw:], scale=100.0)
    put(win, y, x + w - len(s), s, col(lk))
    return 1


def panel_swap(win, y, x, w, m):
    used = m["swap"].get("used", 0)
    key = "ok" if used == 0 else ("warn" if used < (1 << 30) else "crit")
    nx = put(win, y, x, "%s / %s" % (human(used), human(m["swap"].get("total", 0))),
             col(key, curses.A_BOLD)) + 3
    put(win, y, nx, "compression %.2fx   pageins %s   swapouts %s" % (
        m["ratio"], m["pageins"], m["swapouts"]), col("dim"))
    return 1


def panel_io(win, y, x, w, m):
    put(win, y, x, "compression %.2fx  (%s → %s)" % (
        m["ratio"], human(m["stored"]), human(m["occupied"])), col("dim"))
    y += 1
    put(win, y, x, "pageins %s   pageouts %s   swapins %s   swapouts %s" % (
        m["pageins"], m["pageouts"], m["swapins"], m["swapouts"]), col("dim"))
    return 2


def panel_power(win, y, x, w, mac, max_rows=2):
    put(win, y, x, "cpu %.2f W   gpu %.2f W   ane %.2f W   ram %.2f W   sys %.2f W" % (
        mac.get("cpu_power", 0), mac.get("gpu_power", 0), mac.get("ane_power", 0),
        mac.get("ram_power", 0), mac.get("sys_power", 0)), col("dim"))
    fans = mac.get("fans") or []
    if fans and max_rows >= 2:
        put(win, y + 1, x, "   ".join(
            "%s %d/%d rpm" % (f.get("name", "fan"), f.get("rpm", 0), f.get("max_rpm", 0))
            for f in fans), col("dim"))
        return 2
    return 1


def panel_procs(win, y, x, w, max_rows, st):
    grouped = st["group"]
    hattr = col("dim") | curses.A_UNDERLINE
    cmd_x = x + 28
    put(win, y, x, "MEM".rjust(10), hattr)
    put(win, y, x + 12, "CPU%".rjust(6), hattr)
    put(win, y, x + 20, ("N".rjust(6) if grouped else "PID".rjust(6)), hattr)
    put(win, y, cmd_x, "COMMAND".ljust(max(8, w - 28)), hattr)
    y += 1

    for r in st["procs"][:max(0, (max_rows - 1 + ROW_STEP - 1) // ROW_STEP)]:
        put(win, y, x, human(r["mem"]).rjust(10), col("app"))
        put(win, y, x + 12, ("%5.1f" % r["cpu"]).rjust(6),
            col(heat(min(1.0, r["cpu"] / 100.0))) if r["cpu"] >= 1 else col("dim"))
        put(win, y, x + 20, (str(r["n"]) if grouped else r["pid"]).rjust(6), col("dim"))
        put(win, y, cmd_x, r["name"])
        y += ROW_STEP
    return max_rows


def procs_title(st):
    """Box titles are ASCII-only (Hangul would push the border on the same line)."""
    return "PROCESSES  by %s  %s" % (
        "CPU" if st["sort"] == "cpu" else "MEM",
        "(grouped by name)" if st["group"] else "(per PID)")


# ------------------------------------------------------------------- layouts

def layout_all(win, y, bottom, w, st):
    """Per-core CPU + memory details on one screen. Side by side when wide, stacked when narrow."""
    m = st["mem"]
    wide = w >= WIDE_COLS
    # on wide screens, narrow MEMORY and widen CPU by that much so E/P cores sit side by side
    cw = max(46, int((w - 3) * 0.64)) if wide else w - 2
    cpu_want = core_rows(cw - 4, st) + 4 + CPU_SUM_GAP + CPU_HEAD_GAP   # 2 summary rows (+gap) + gap + cores + border
    mem_want = rows_for(6 if m["other"] > 0 else 5) + 4

    def room(need):
        return (bottom - y) - need >= PROC_MIN_H

    def fill_cpu(by, bx, bw, bh):
        used = panel_cpu(win, by, bx, bw, st)
        panel_cpu_cores(win, by + used + CPU_HEAD_GAP, bx, bw, st,
                        bh - 2 - used - CPU_HEAD_GAP)

    def fill_mem(by, bx, bw, bh):
        used = panel_mem(win, by, bx, bw, m)
        used += panel_mem_detail(win, by + used, bx, bw, m, bh - 2 - used)
        # fill the space left over from matching the CPU box height with compression/IO
        left = bh - 2 - used
        if left >= 3:
            put(win, by + used + 1, bx, G["h"] * bw, col("dim"))
            panel_io(win, by + used + 2, bx, bw, m)

    def gpu_row(y):
        if (bottom - y) - 4 < PROC_MIN_H:
            return y
        if wide and st["mac"]:
            gw = max(42, int((w - 3) * 0.50))
            by, bx, bw = box(win, y, 1, gw, 4, "GPU", "gpu")
            panel_gpu(win, by, bx, bw, st)
            by, bx, bw = box(win, y, 2 + gw, w - 3 - gw, 4, "POWER", "warn")
            panel_power(win, by, bx, bw, st["mac"], max_rows=2)
        else:
            by, bx, bw = box(win, y, 1, w - 2, 4, "GPU", "gpu")
            panel_gpu(win, by, bx, bw, st)
        return y + 4 + BOX_GAP

    if wide:
        avail = (bottom - y) - PROC_MIN_H
        if avail >= 6:
            bh = min(max(cpu_want, mem_want), avail)   # match both box heights side by side
            by, bx, bw = box(win, y, 1, cw, bh, "CPU", "cpu")
            fill_cpu(by, bx, bw, bh)
            by, bx, bw = box(win, y, 2 + cw, w - 3 - cw, bh, "MEMORY", "app")
            fill_mem(by, bx, bw, bh)
            y += bh + BOX_GAP
        y = gpu_row(y)
    else:
        # when stacked: CPU > GPU > MEMORY > PRESSURE
        avail = (bottom - y) - PROC_MIN_H
        if avail >= 6:
            bh = min(cpu_want, avail)
            by, bx, bw = box(win, y, 1, w - 2, bh, "CPU", "cpu")
            fill_cpu(by, bx, bw, bh)
            y += bh + BOX_GAP
        y = gpu_row(y)
        avail = (bottom - y) - PROC_MIN_H
        if avail >= 6:
            bh = min(mem_want, avail)
            by, bx, bw = box(win, y, 1, w - 2, bh, "MEMORY", "app")
            fill_mem(by, bx, bw, bh)
            y += bh + BOX_GAP

    if room(3):
        if wide:
            pw = max(34, int((w - 3) * 0.38))
            by, bx, bw = box(win, y, 1, pw, 3, "PRESSURE", "warn")
            panel_pressure(win, by, bx, bw, m, st["hist"])
            by, bx, bw = box(win, y, 2 + pw, w - 3 - pw, 3, "SWAP", "comp")
            panel_swap(win, by, bx, bw, m)
        else:
            by, bx, bw = box(win, y, 1, w - 2, 3, "PRESSURE", "warn")
            panel_pressure(win, by, bx, bw, m, st["hist"])
        y += 3 + BOX_GAP

    ph = bottom - y
    if ph >= 3:
        by, bx, bw = box(win, y, 1, w - 2, ph, procs_title(st), "accent")
        panel_procs(win, by, bx, bw, ph - 2, st)


def layout_overview(win, y, bottom, w, st):
    m = st["mem"]
    wide = w >= WIDE_COLS

    def room(need):
        return (bottom - y) - need >= PROC_MIN_H

    cpu_h = 4 + CPU_SUM_GAP
    if wide and room(cpu_h):
        cw = max(50, int((w - 3) * 0.56))
        by, bx, bw = box(win, y, 1, cw, cpu_h, "CPU", "cpu")
        panel_cpu(win, by, bx, bw, st)
        by, bx, bw = box(win, y, 2 + cw, w - 3 - cw, cpu_h, "GPU", "gpu")
        panel_gpu(win, by, bx, bw, st)
        y += cpu_h + BOX_GAP
    else:
        if room(cpu_h):
            by, bx, bw = box(win, y, 1, w - 2, cpu_h, "CPU", "cpu")
            panel_cpu(win, by, bx, bw, st)
            y += cpu_h + BOX_GAP
        if room(4):
            by, bx, bw = box(win, y, 1, w - 2, 4, "GPU", "gpu")
            panel_gpu(win, by, bx, bw, st)
            y += 4 + BOX_GAP

    if room(4):
        by, bx, bw = box(win, y, 1, w - 2, 4, "MEMORY", "app")
        panel_mem(win, by, bx, bw, m)
        y += 4 + BOX_GAP

    if room(3):
        if wide:
            pw = max(34, int((w - 3) * 0.38))
            by, bx, bw = box(win, y, 1, pw, 3, "PRESSURE", "warn")
            panel_pressure(win, by, bx, bw, m, st["hist"])
            by, bx, bw = box(win, y, 2 + pw, w - 3 - pw, 3, "SWAP", "comp")
            panel_swap(win, by, bx, bw, m)
            y += 3 + BOX_GAP
        else:
            by, bx, bw = box(win, y, 1, w - 2, 3, "PRESSURE", "warn")
            panel_pressure(win, by, bx, bw, m, st["hist"])
            y += 3 + BOX_GAP
            if room(3):
                by, bx, bw = box(win, y, 1, w - 2, 3, "SWAP", "comp")
                panel_swap(win, by, bx, bw, m)
                y += 3 + BOX_GAP

    ph = bottom - y
    if ph >= 3:
        by, bx, bw = box(win, y, 1, w - 2, ph, procs_title(st), "accent")
        panel_procs(win, by, bx, bw, ph - 2, st)


def layout_mem(win, y, bottom, w, st):
    m = st["mem"]
    wide = w >= WIDE_COLS

    def room(need):
        return (bottom - y) - need >= PROC_MIN_H

    if room(3):
        if wide:
            pw = max(34, int((w - 3) * 0.38))
            by, bx, bw = box(win, y, 1, pw, 3, "PRESSURE", "warn")
            panel_pressure(win, by, bx, bw, m, st["hist"])
            by, bx, bw = box(win, y, 2 + pw, w - 3 - pw, 3, "SWAP", "comp")
            panel_swap(win, by, bx, bw, m)
        else:
            by, bx, bw = box(win, y, 1, w - 2, 3, "PRESSURE", "warn")
            panel_pressure(win, by, bx, bw, m, st["hist"])
        y += 3 + BOX_GAP

    nrows = 6 if m["other"] > 0 else 5
    want = rows_for(nrows) + 4            # header + stack bar + items + border
    avail = (bottom - y) - PROC_MIN_H
    if avail >= 6:
        bh = min(want, avail)
        by, bx, bw = box(win, y, 1, w - 2, bh, "MEMORY", "app")
        used = panel_mem(win, by, bx, bw, m)
        panel_mem_detail(win, by + used, bx, bw, m, bh - 2 - used)
        y += bh + BOX_GAP

    if room(4):
        by, bx, bw = box(win, y, 1, w - 2, 4, "COMPRESSION / IO", "comp")
        panel_io(win, by, bx, bw, m)
        y += 4 + BOX_GAP

    ph = bottom - y
    if ph >= 3:
        by, bx, bw = box(win, y, 1, w - 2, ph, procs_title(st), "accent")
        panel_procs(win, by, bx, bw, ph - 2, st)


def layout_cpu(win, y, bottom, w, st):
    want = core_rows(w - 6, st) + 4 + CPU_SUM_GAP + CPU_HEAD_GAP   # 2 summary rows (+gap) + gap + cores + border
    avail = (bottom - y) - PROC_MIN_H
    if avail >= 5:
        bh = min(want, avail)
        by, bx, bw = box(win, y, 1, w - 2, bh, "CPU", "cpu")
        used = panel_cpu(win, by, bx, bw, st)
        panel_cpu_cores(win, by + used + CPU_HEAD_GAP, bx, bw, st,
                        bh - 2 - used - CPU_HEAD_GAP)
        y += bh + BOX_GAP

    def room(need):
        return (bottom - y) - need >= PROC_MIN_H

    wide = w >= WIDE_COLS
    mac = st["mac"]
    if room(4):
        if wide and mac:
            gw = max(46, int((w - 3) * 0.50))
            by, bx, bw = box(win, y, 1, gw, 4, "GPU", "gpu")
            panel_gpu(win, by, bx, bw, st)
            by, bx, bw = box(win, y, 2 + gw, w - 3 - gw, 4, "POWER", "warn")
            panel_power(win, by, bx, bw, mac, max_rows=2)
            y += 4 + BOX_GAP
        else:
            by, bx, bw = box(win, y, 1, w - 2, 4, "GPU", "gpu")
            panel_gpu(win, by, bx, bw, st)
            y += 4 + BOX_GAP
            if mac and room(4):
                by, bx, bw = box(win, y, 1, w - 2, 4, "POWER", "warn")
                panel_power(win, by, bx, bw, mac, max_rows=2)
                y += 4 + BOX_GAP
            elif mac and room(3):
                by, bx, bw = box(win, y, 1, w - 2, 3, "POWER", "warn")
                panel_power(win, by, bx, bw, mac, max_rows=1)
                y += 3 + BOX_GAP

    ph = bottom - y
    if ph >= 3:
        by, bx, bw = box(win, y, 1, w - 2, ph, procs_title(st), "accent")
        panel_procs(win, by, bx, bw, ph - 2, st)


def draw(win, st):
    win.erase()
    h, w = win.getmaxyx()
    m = st["mem"]

    title = " %s " % (st["chip"] or "macOS")
    put(win, 0, 1, title, col("accent", curses.A_BOLD))
    bits = [human(m["total"])]
    if len(st["layout"]) > 1:
        bits.append("%dP+%dE" % (len(st["layout"][1][1]), len(st["layout"][0][1])))
    else:
        bits.append("%d CPU" % sum(len(i) for _l, i in st["layout"]))
    if st["gpu"] and st["gpu"]["cores"]:
        bits.append("GPU %d cores" % st["gpu"]["cores"])
    if st["mac"]:
        bits.append("%.1f W" % st["mac"].get("sys_power", 0.0))
    # when short on width, wrap item by item onto the next line so all stay visible (each right-aligned)
    items = [("  ·  " if i else "", b) for i, b in enumerate(bits)]
    items.append(("   ", time.strftime("%H:%M:%S")))
    lines, cur, avail = [], "", w - dwidth(title) - 4
    for sep, text in items:
        if cur and dwidth(cur + sep + text) > avail:
            lines.append(cur)
            cur, avail = text, w - 3
        else:
            cur = cur + sep + text if cur else text
    lines.append(cur)
    for i, line in enumerate(lines):
        put(win, i, max(1, w - dwidth(line) - 2), line + " ", col("dim"))

    # the bottom shortcut bar also wraps item by item when short on width
    tabs = [("1", t("tab_all"), "all"), ("2", t("tab_overview"), "overview"),
            ("3", t("tab_mem"), "mem"), ("4", t("tab_cpu"), "cpu")]
    keys = ["%s %s%s" % (k, lab, G["mark"] if st["view"] == v else "") for k, lab, v in tabs]
    keys += ["s " + t("key_sort"), "g " + t("key_group"), "p " + t("key_pause"),
             "+/- %.1fs" % st["interval"], "q " + t("key_quit")]
    if st["paused"]:
        keys.insert(0, "[PAUSED]")
    footer, cur = [], ""
    for k in keys:
        if cur and dwidth(" %s   %s " % (cur, k)) > w - 2:
            footer.append(cur)
            cur = k
        else:
            cur = "%s   %s" % (cur, k) if cur else k
    footer.append(cur)

    bottom = h - len(footer)
    top = len(lines)
    if st["view"] == "cpu":
        layout_cpu(win, top, bottom, w, st)
    elif st["view"] == "mem":
        layout_mem(win, top, bottom, w, st)
    elif st["view"] == "overview":
        layout_overview(win, top, bottom, w, st)
    else:
        layout_all(win, top, bottom, w, st)

    for i, line in enumerate(footer):
        put(win, bottom + i, 1, " %s " % line, col("dim", curses.A_REVERSE))
    win.noutrefresh()
    curses.doupdate()


# ================================================================ main loop

def avoid_ncurses_rep_bug():
    """Work around the REP (repeat) + multibyte bug in macOS ncurses.

    When terminfo has the rep capability, ncurses shortens runs of the same character
    to "repeat the previous character N times" (CSI Pn b). The old ncurses that ships
    with macOS emits only the last byte of a multibyte character on this path.
    For example 60 x '▁' (\\xe2\\x96\\x81) goes out as '\\x81' + CSI 60 b, and the
    terminal draws the lone continuation byte \\x81 60 times, breaking borders and bars.

    Only some terminfo entries advertise rep (e.g. xterm-ghostty), so only then is
    TERM swapped for an entry without rep. Everything else, like color count, is unchanged.
    """
    term = os.environ.get("TERM", "")
    if not term:
        return None
    try:
        out = subprocess.run(["infocmp", "-1", term],
                             capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return None
    if not re.search(r"^\s*rep=", out, re.M):
        return None
    for alt in ("xterm-256color", "xterm-color", "xterm"):
        try:
            if subprocess.run(["infocmp", alt], capture_output=True,
                              timeout=3).returncode == 0:
                os.environ["TERM"] = alt
                return (term, alt)
        except Exception:
            pass
    return None


def run(win, args, feed):
    curses.curs_set(0)
    curses.halfdelay(1)          # 0.1s - keys respond immediately, data refreshes every interval
    init_colors()
    cpu = CPUSampler()
    procs = ProcSampler(args.top_every)

    st = {
        "view": args.view, "group": args.group, "sort": args.sort,
        "paused": False, "interval": args.interval,
        "chip": sysctl("machdep.cpu.brand_string") or None,
        "layout": cpu.layout, "cores": [], "load": [0, 0, 0],
        "gpu": None, "mac": None, "mem": None, "hist": [],
        "procs": [], "procs_raw": [],
    }
    due = 0.0

    while True:
        if not st["paused"] and time.time() >= due:
            st["mem"] = read_memory()
            st["cores"] = cpu.sample()
            st["load"] = load_avg()
            st["gpu"] = read_gpu()
            st["mac"] = feed.data
            fp = st["mem"]["free_pct"]
            st["hist"].append((100 - fp) if fp is not None else 0)
            del st["hist"][:-600]
            raw = procs.sample()
            st["procs_raw"] = raw
            st["procs"] = sort_procs(group_procs(raw) if st["group"] else raw, st["sort"])
            due = time.time() + st["interval"]
        if st["mem"]:
            draw(win, st)

        ch = win.getch()
        if ch == -1:
            continue
        if ch in (ord("q"), ord("Q"), 27):
            return
        if ch in (ord("1"), ord("2"), ord("3"), ord("4"), ord("\t")):
            order = ["all", "overview", "mem", "cpu"]
            if ch == ord("\t"):
                st["view"] = order[(order.index(st["view"]) + 1) % len(order)]
            else:
                st["view"] = order[ch - ord("1")]
            st["sort"] = "cpu" if st["view"] == "cpu" else "mem"
            st["procs"] = sort_procs(st["procs"], st["sort"])
        elif ch in (ord("s"), ord("S")):
            st["sort"] = "cpu" if st["sort"] == "mem" else "mem"
            st["procs"] = sort_procs(st["procs"], st["sort"])
        elif ch in (ord("g"), ord("G")):
            st["group"] = not st["group"]
            st["procs"] = sort_procs(
                group_procs(st["procs_raw"]) if st["group"] else st["procs_raw"], st["sort"])
        elif ch in (ord("p"), ord("P"), ord(" ")):
            st["paused"] = not st["paused"]
        elif ch in (ord("r"), ord("R")):
            due = 0
        elif ch in (ord("+"), ord("=")):
            st["interval"] = min(30.0, st["interval"] + 0.5)
        elif ch in (ord("-"), ord("_")):
            st["interval"] = max(0.5, st["interval"] - 0.5)
        elif ch == curses.KEY_RESIZE:
            curses.update_lines_cols()


def print_once(args, feed):
    cpu = CPUSampler()
    procs = ProcSampler(args.top_every)
    procs.sample()                    # CPU% needs a delta, so warm up once
    # wait briefly for the first macmon sample (skip if macmon is absent)
    deadline = time.time() + (2.0 if getattr(feed, "available", False) else 0.0)
    while feed.data is None and time.time() < deadline:
        time.sleep(0.1)
    time.sleep(min(1.0, max(0.3, args.interval / 2)))
    cores = cpu.sample()
    m = read_memory()
    g = read_gpu()
    mac = feed.data
    layout = cpu.layout

    print("chip           %s" % (sysctl("machdep.cpu.brand_string") or "-"))
    tot = (sum(cores) / len(cores) * 100) if cores else 0
    print("cpu            %.1f%%" % tot)
    for label, idxs in layout:
        vals = [cores[i] for i in idxs if i < len(cores)]
        if vals:
            print("  %-12s %.1f%%  (%s)" % (
                label, sum(vals) / len(vals) * 100,
                " ".join("%.0f" % (v * 100) for v in vals)))
    print("load           %.2f %.2f %.2f" % tuple(load_avg()))
    if g:
        print("gpu            %s%%   renderer %s%%  tiler %s%%  vram %s  cores %s" % (
            g["util"], g["renderer"], g["tiler"], human(g["vram_used"]), g["cores"]))
    if mac:
        temp = mac.get("temp") or {}
        print("freq           P %d MHz   E %d MHz   GPU %d MHz" % (
            mac.get("pcpu_freq_mhz", 0), mac.get("ecpu_freq_mhz", 0), mac.get("gpu_freq_mhz", 0)))
        print("power          cpu %.2f W  gpu %.2f W  ane %.2f W  sys %.2f W" % (
            mac.get("cpu_power", 0), mac.get("gpu_power", 0),
            mac.get("ane_power", 0), mac.get("sys_power", 0)))
        print("temp           cpu %.1f°C  gpu %.1f°C" % (
            temp.get("cpu_temp_avg", 0), temp.get("gpu_temp_avg", 0)))
    print("memory used    %s / %s   (app + wired + compressed)" % (
        human(m["used"]), human(m["total"])))
    print("  app memory   %s" % human(m["app"]))
    print("  wired        %s" % human(m["wired"]))
    print("  compressed   %s  (%s)" % (
        human(m["compressed"]), t("once_compressed") % (m["ratio"], human(m["stored"]))))
    print("cached files   %s   (%s)" % (human(m["cached"]), t("once_reclaimable")))
    print("free           %s" % human(m["free"]))
    pct = (100 - m["free_pct"]) if m["free_pct"] is not None else 0
    print("pressure       %d%%  [%s]" % (pct, m["level"]))
    print("swap           %s / %s   swapins %s  swapouts %s" % (
        human(m["swap"].get("used", 0)), human(m["swap"].get("total", 0)),
        m["swapins"], m["swapouts"]))
    print()
    rows = procs.sample()
    rows = sort_procs(group_procs(rows) if args.group else rows, args.sort)
    head = "N" if args.group else "PID"
    print("%10s  %6s  %6s  %s" % ("MEM", "CPU%", head, "COMMAND"))
    for r in rows[:12]:
        print("%10s  %6.1f  %6s  %s" % (
            human(r["mem"]), r["cpu"], (r["n"] if args.group else r["pid"]), r["name"]))


def main():
    # the language must be known before argparse builds the help text
    globals()["LANG"] = detect_lang(sys.argv[1:])

    ap = argparse.ArgumentParser(prog="mac-dash", description=t("desc"))
    ap.add_argument("-i", "--interval", type=float, default=2.0, help=t("h_interval"))
    ap.add_argument("-1", "--once", action="store_true", help=t("h_once"))
    ap.add_argument("-g", "--group", action="store_true", help=t("h_group"))
    ap.add_argument("-s", "--sort", choices=("mem", "cpu"), default="mem", help=t("h_sort"))
    ap.add_argument("-v", "--view", choices=("all", "overview", "mem", "cpu"),
                    default="all", help=t("h_view"))
    ap.add_argument("--gap", choices=tuple(GAP_PRESETS), default="tight", help=t("h_gap"))
    ap.add_argument("--keep-term", action="store_true", help=t("h_keep_term"))
    ap.add_argument("--border", choices=tuple(BORDER_STYLES), default="rounded",
                    help=t("h_border"))
    ap.add_argument("--dim-attr", action="store_true", help=t("h_dim_attr"))
    ap.add_argument("--ascii", action="store_true", help=t("h_ascii"))
    ap.add_argument("--no-macmon", action="store_true", help=t("h_no_macmon"))
    ap.add_argument("--top-every", type=int, default=5, metavar="N", help=t("h_top_every"))
    ap.add_argument("--lang", choices=tuple(MESSAGES), help=t("h_lang"))
    ap.add_argument("--version", action="version", version="mac-dash " + __version__)
    args = ap.parse_args()
    if args.dim_attr:
        globals()["FORCE_DIM_ATTR"] = True
    gap = os.environ.get("MAC_DASH_GAP")
    if gap not in GAP_PRESETS:
        gap = args.gap
    globals()["BOX_GAP"], globals()["ROW_STEP"] = GAP_PRESETS[gap]

    # By default SIGTERM/SIGHUP skip the cleanup code and leave the macmon child behind.
    # (SIGKILL cannot be caught anyway, but then the stdout pipe closes and macmon exits on its own)
    def _bye(signum, _frame):
        raise SystemExit(128 + signum)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _bye)
        except (ValueError, OSError):
            pass

    style = os.environ.get("MAC_DASH_BORDER")
    if style not in BORDER_STYLES:
        style = args.border
    G.pop("hb", None)
    G.pop("vr", None)
    G.update(BORDER_STYLES[style])
    if args.ascii or os.environ.get("MAC_DASH_ASCII"):
        G.update(GLYPH_ASCII)

    # when invoked as memtui, start in the memory view (keeps the old habit working)
    if os.path.basename(sys.argv[0]).startswith("memtui") and args.view == "all":
        args.view = "mem"
    if args.view == "cpu" and "-s" not in sys.argv and "--sort" not in sys.argv:
        args.sort = "cpu"

    # must run before curses initializes (before setupterm reads TERM)
    if not args.keep_term:
        avoid_ncurses_rep_bug()

    if args.no_macmon:
        feed = type("NoFeed", (), {"data": None, "stop": lambda self: None})()
    else:
        feed = MacmonFeed(int(max(0.5, args.interval) * 1000))
    try:
        if args.once or not sys.stdout.isatty():
            print_once(args, feed)
        else:
            curses.wrapper(run, args, feed)
    except KeyboardInterrupt:
        pass                      # Ctrl+C is treated as a normal exit, same as q
    finally:
        feed.stop()


if __name__ == "__main__":
    main()
