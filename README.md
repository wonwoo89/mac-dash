# mac-dash

Real-time CPU / GPU / memory TUI dashboard for macOS.

[한국어](README.ko.md)

- **No sudo, no dependencies** — a single Python file using only the standard library (`curses` + `ctypes`).
- **Memory numbers that match Activity Monitor** — `Used = App + Wired + Compressed`, plus cached files, memory pressure and swap.
- **Per-core CPU** — E-cores and P-cores shown separately on Apple Silicon.
- **GPU utilization** — device / renderer / tiler % and VRAM in use, read from `ioreg`.
- **Processes** — physical footprint (the same value as Activity Monitor's *Memory* column) and instantaneous CPU%, per PID or grouped by name.
- **Optional [macmon](https://github.com/vladkens/macmon) integration** — adds CPU/GPU frequency, power, temperature and fans when `macmon` is on `PATH`.
- English and Korean UI.

## Install

```sh
brew install wonwoo89/tap/mac-dash

# optional: frequency / power / temperature / fan panels
brew install macmon
```

Or run the script directly:

```sh
curl -fsSLo mac-dash https://raw.githubusercontent.com/wonwoo89/mac-dash/main/mac-dash.py
chmod +x mac-dash && ./mac-dash
```

Requires macOS and Python 3.9+.

## Usage

```sh
mac-dash              # TUI, all view
mac-dash -v mem       # start in the memory view
mac-dash --once       # print a snapshot and exit (also used when stdout is not a TTY)
mac-dash -g -s cpu    # group processes by name, sort by CPU
```

### Keys

| Key | Action |
| --- | --- |
| `1` `2` `3` `4` / `Tab` | all / overview / memory / CPU view |
| `s` | toggle sort (memory ↔ CPU) |
| `g` | toggle grouping by process name |
| `p` / `Space` | pause |
| `r` | refresh now |
| `+` / `-` | change refresh interval (0.5s – 30s) |
| `q` / `Esc` | quit |

### Options

| Option | Description |
| --- | --- |
| `-i, --interval SEC` | refresh interval (default 2.0) |
| `-1, --once` | print the current state once without the TUI |
| `-g, --group` | aggregate processes by name |
| `-s, --sort {mem,cpu}` | process sort key |
| `-v, --view {all,overview,mem,cpu}` | initial view |
| `--gap {tight,normal,loose}` | spacing between boxes and rows |
| `--border STYLE` | `rounded` (default), `heavy`, `double`, `square`, `block`, `solid`, `ascii`, `none` |
| `--ascii` | draw bars and sparklines with ASCII characters |
| `--dim-attr` | draw secondary text with SGR dim instead of color 8 |
| `--keep-term` | keep `TERM` as is (disables the ncurses REP workaround) |
| `--no-macmon` | disable macmon integration |
| `--top-every N` | refresh root-owned process memory via `top` every N ticks (default 5) |
| `--lang {en,ko}` | UI language |
| `--version` | print the version |

### Environment variables

| Variable | Description |
| --- | --- |
| `MAC_DASH_LANG` | `en` or `ko` |
| `MAC_DASH_BORDER` | default border style |
| `MAC_DASH_GAP` | default spacing preset |
| `MAC_DASH_ASCII` | any value enables ASCII glyphs |

The language is chosen from `--lang`, then `MAC_DASH_LANG`, then `LC_ALL` / `LC_MESSAGES`, then the macOS system language, then `LANG`.

## How the numbers are computed

| Metric | Source |
| --- | --- |
| Memory | `vm_stat`, `memory_pressure`, `sysctl vm.swapusage` — App = anonymous − purgeable, Cached = file-backed + purgeable |
| CPU | per-core tick deltas from `host_processor_info`, core groups from `sysctl hw.perflevel*` |
| GPU | `PerformanceStatistics` of `AGXAccelerator` in `ioreg` |
| Process memory | `proc_pid_rusage` `ri_phys_footprint`; processes owned by other users are filled in from `top` |
| Process CPU% | delta of cumulative CPU time from `ps` over the refresh interval |

## Troubleshooting

- **Broken borders or bars** — try `--border heavy`, `--border solid` or `--ascii`.
  mac-dash already swaps `TERM` when the terminfo advertises `rep`, because the ncurses shipped with macOS mangles repeated multibyte characters; `--keep-term` turns that off.
- **Faint secondary text** — try `--dim-attr`.

## License

[MIT](LICENSE)
