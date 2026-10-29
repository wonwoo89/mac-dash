# mac-dash

macOS CPU / GPU / 메모리 실시간 TUI 대시보드.

[English](README.md)

- **sudo·의존성 없음** — 표준 라이브러리(`curses` + `ctypes`)만 쓰는 파이썬 파일 하나.
- **활성 상태 보기와 같은 메모리 수치** — `사용 = 앱 + 와이어드 + 압축`, 캐시된 파일, 메모리 압력, 스왑.
- **코어별 CPU** — Apple Silicon 에서는 E 코어와 P 코어를 나눠 표시.
- **GPU 사용률** — `ioreg` 에서 읽은 Device / Renderer / Tiler % 와 사용 중인 VRAM.
- **프로세스** — 물리 풋프린트(활성 상태 보기 *메모리* 열과 같은 값)와 순간 CPU%, PID 별 또는 이름별 합산.
- **[macmon](https://github.com/vladkens/macmon) 연동(선택)** — `macmon` 이 `PATH` 에 있으면 CPU/GPU 주파수·전력·온도·팬을 함께 표시.
- 한국어·영어 UI.

## 설치

```sh
brew install wonwoo89/tap/mac-dash

# 선택: 주파수·전력·온도·팬 패널
brew install macmon
```

스크립트를 바로 실행해도 된다.

```sh
curl -fsSLo mac-dash https://raw.githubusercontent.com/wonwoo89/mac-dash/main/mac-dash.py
chmod +x mac-dash && ./mac-dash
```

macOS 와 Python 3.9 이상이 필요하다.

## 사용법

```sh
mac-dash              # TUI, 전체 뷰
mac-dash -v mem       # 메모리 뷰로 시작
mac-dash --once       # 현재 상태를 한 번 출력하고 종료 (stdout 이 TTY 가 아닐 때도 이 모드)
mac-dash -g -s cpu    # 프로세스를 이름별로 합산하고 CPU 순 정렬
```

### 단축키

| 키 | 동작 |
| --- | --- |
| `1` `2` `3` `4` / `Tab` | 전체 / 개요 / 메모리 / CPU 뷰 |
| `s` | 정렬 전환 (메모리 ↔ CPU) |
| `g` | 프로세스 이름별 합산 전환 |
| `p` / `Space` | 일시 정지 |
| `r` | 즉시 갱신 |
| `+` / `-` | 갱신 주기 변경 (0.5초 – 30초) |
| `q` / `Esc` | 종료 |

### 옵션

| 옵션 | 설명 |
| --- | --- |
| `-i, --interval SEC` | 갱신 주기 (기본 2.0) |
| `-1, --once` | TUI 없이 현재 상태만 출력 |
| `-g, --group` | 프로세스를 이름별로 합산 |
| `-s, --sort {mem,cpu}` | 프로세스 정렬 기준 |
| `-v, --view {all,overview,mem,cpu}` | 시작 뷰 |
| `--gap {tight,normal,loose}` | 박스·목록 사이 여백 |
| `--border STYLE` | `rounded`(기본), `heavy`, `double`, `square`, `block`, `solid`, `ascii`, `none` |
| `--ascii` | 막대·스파크라인을 ASCII 문자로 그림 |
| `--dim-attr` | 보조 텍스트를 색 8 대신 SGR dim 으로 그림 |
| `--keep-term` | `TERM` 을 그대로 둠 (ncurses REP 회피를 끔) |
| `--no-macmon` | macmon 연동 끄기 |
| `--top-every N` | root 소유 프로세스 메모리를 N 회마다 `top` 으로 보강 (기본 5) |
| `--lang {en,ko}` | UI 언어 |
| `--version` | 버전 출력 |

### 환경 변수

| 변수 | 설명 |
| --- | --- |
| `MAC_DASH_LANG` | `en` 또는 `ko` |
| `MAC_DASH_BORDER` | 기본 테두리 스타일 |
| `MAC_DASH_GAP` | 기본 여백 프리셋 |
| `MAC_DASH_ASCII` | 값이 있으면 ASCII 글리프 사용 |

언어는 `--lang` → `MAC_DASH_LANG` → `LC_ALL` / `LC_MESSAGES` → macOS 시스템 언어 → `LANG` 순으로 정한다.

## 수치 계산 방식

| 항목 | 출처 |
| --- | --- |
| 메모리 | `vm_stat`, `memory_pressure`, `sysctl vm.swapusage` — 앱 = anonymous − purgeable, 캐시 = file-backed + purgeable |
| CPU | `host_processor_info` 의 코어별 tick 델타, 코어 그룹은 `sysctl hw.perflevel*` |
| GPU | `ioreg` `AGXAccelerator` 의 `PerformanceStatistics` |
| 프로세스 메모리 | `proc_pid_rusage` 의 `ri_phys_footprint`, 다른 사용자 소유 프로세스는 `top` 으로 보강 |
| 프로세스 CPU% | `ps` 누적 CPU 시간을 갱신 주기로 나눈 델타 |

## 문제 해결

- **테두리나 막대가 깨질 때** — `--border heavy`, `--border solid`, `--ascii` 를 시도한다.
  macOS 기본 ncurses 가 반복되는 멀티바이트 문자를 망가뜨리기 때문에, terminfo 가 `rep` 을 광고하면 mac-dash 가 `TERM` 을 자동으로 바꾼다. `--keep-term` 으로 끌 수 있다.
- **보조 텍스트가 흐릴 때** — `--dim-attr` 를 시도한다.

## 라이선스

[MIT](LICENSE)
