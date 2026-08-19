# CYD 무선 매크로 패드 (Wireless Macro Pad)

CYD 보드(**ESP32-2432S028**, 2.8" 320x240 터치 LCD)를 **무선 매크로 패드(Stream Deck 클론)** 로 바꾸는 프로젝트.

화면에 4×3 터치 버튼 그리드가 보이고, 버튼을 누르면 Wi-Fi(UDP)로 Mac 호스트에 이벤트가 전달된다. 호스트는 설정된 액션을 실행한다.

- **키보드 단축키** (예: `cmd+shift+4`)
- **문구/텍스트 입력** (한글 포함, IME 무관 — 클립보드 + Cmd+V 방식)
- **앱 실행 / URL 열기** (예: `Google Chrome`, `https://claude.ai`)

2페이지 × 12버튼 = 총 24버튼 구성. 버튼 라벨은 호스트 GUI에서 편집하고, 설정을 적용하면 디바이스에 즉시 반영된다.

## 프로젝트 구조

```
CYD_Macro_Pad/
├── CYD_Macro_Pad/
│   └── CYD_Macro_Pad.ino            # ESP32 펌웨어 (단일 파일, Arduino IDE)
└── host_macro_pad/
    ├── macro_pad_gui.py             # 호스트 GUI (설정 편집 + UDP 리스너 + 액션 실행)
    ├── _input_helper.py             # 키보드 입력 격리 헬퍼 (pynput 전용 서브프로세스)
    ├── requirements.txt             # pynput
    └── macro_config.json            # 설정 파일 (실행 시 자동 생성, gitignore 대상)
```

## 시작하기

### 1. 펌웨어 업로드 (Arduino IDE)

1. `CYD_Macro_Pad/CYD_Macro_Pad.ino`를 Arduino IDE로 연다.
2. **보드:** `ESP32 Dev Module` · **Flash:** `4MB (32Mb)` · **파티션:** `Huge APP (3MB No OTA/1MB SPIFFS)` · **업로드 속도:** `921600` (불안정하면 `115200`)
3. 라이브러리 매니저에서 **LovyanGFX** 설치 (JPEGDEC는 불필요).
4. 업로드 후 시리얼 모니터(115200)에서 `[WiFi] Connected!` + `[AsyncUDP] Listening on port 8890` 확인.

> 첫 부팅 시 Wi-Fi를 터치로 설정한다: AP 목록 선택 → 가상 키보드로 비밀번호 입력 → NVS에 저장.
> 나중에 바꾸려면 **하단 바를 2.5초 길게 누르면** 재설정 화면이 열린다.

### 2. 호스트 실행 (Mac)

```bash
cd "CYD_Macro_Pad/host_macro_pad"
pip install -r requirements.txt
python3 macro_pad_gui.py
```

> **macOS 접근성 권한 (필수):** 키보드 입력(pynput)은 호스트 GUI가 아니라
> 격리된 헬퍼 프로세스(`_input_helper.py`)에서 수행된다. 키보드를 제어하려면
> **시스템설정 → 개인정보 보호 및 보안 → 손쉬운 사용**에서 터미널(또는 Python)을 켜야 한다.
> 권한이 없으면 단축키/문구 액션이 조용히 무시된다.
>
> **크래시 격리:** pynput은 네이티브(Quartz) 코드라 드물게 프로세스를 통째로 죽일 수 있다.
> 그래서 키보드 입력은 항상 별도 서브프로세스에서 실행된다 — 헬퍼가 죽어도 GUI는
> 살아남고 이벤트 로그에 실패만 표시한다.

### 3. 버튼 설정

1. 호스트 GUI에서 각 버튼의 **라벨**과 **동작**을 입력한다.
   - 동작 종류: `shortcut` / `text` / `app`
   - 예: 라벨 `Copy` + `shortcut` + `cmd+c` / 라벨 `인사` + `text` + `안녕하세요!` / 라벨 `Chrome` + `app` + `Google Chrome`
2. **💾 설정 적용 (Apply)** 클릭 → 디바이스에 라벨이 표시된다.
3. **● 리스너 시작** 클릭 → 이제 디바이스의 버튼을 누르면 액션이 실행된다.

> **IP는 몰라도 된다.** CYD IP 필드를 **비워두고** 리스너를 시작하면, 디바이스가 3초마다
> 보내는 비콘(`MPBE`)을 호스트가 받아 IP를 자동으로 채우고 설정을 전송한다. (같은 네트워크 전제.
> 다른 네트워크라면 IP를 직접 입력 — 자동 검색은 라우터를 못 넘는다.)
>
> 라벨은 영문/숫자만 (기본 폰트가 한글 글리프 미지원). 한글 **문구 입력**은 액션으로 동작한다.
> `주기 재전송 (20s)`을 켜두면 디바이스 재부팅이나 IP 변경 후에도 설정이 자동 복구된다.

## 와이어 프로토콜 요약

| 구분 | 방향 | 포트 | 헤더 `>IBBBB` |
|------|------|------|----------------|
| 설정 패킷 | 호스트 → 디바이스 | `8890` | `magic=0x4D434647("MCFG")`, `page`, `count`, `0`, `0` + 버튼별 `>BB`+라벨 |
| 이벤트 패킷 | 디바이스 → 호스트 | `8890` | `magic=0x4D504144("MPAD")`, `page`, `button_id`, `0`, `0` |
| 비콘 패킷 | 디바이스 → 호스트 (브로드캐스트) | `8890` | `magic=0x4D504245("MPBE")`, `0`, `0`, `0`, `0` |

- 디바이스는 **가장 최근 설정 패킷의 소스 IP/포트**로 이벤트를 전송한다.
- **자동 검색:** 디바이스가 3초마다 서브넷 브로드캐스트로 `MPBE` 비콘을 보낸다. 호스트가 이를 받으면 CYD IP를 자동으로 채우고 설정을 푸시한다. → **같은 네트워크라면 IP를 몰라도 된다** (IP 필드 비워두기). 다른 네트워크면 직접 입력.
- 공유 상수: `PAGES=2`, `GRID_COLS=4`, `GRID_ROWS=3`, `BUTTONS_PER_PAGE=12`, `LABEL_MAX=24`.
- 자세한 계약은 저장소 루트의 `CLAUDE.md`를 참고.

## 팁 / 트러블슈팅

- **버튼을 눌렀는데 아무 일도 없나요?** → 호스트가 리스너 시작 상태인지, 접근성 권한이 켜져 있는지 확인.
- **디바이스 라벨이 안 바뀌나요?** → 호스트 CYD IP가 디바이스와 같은 네트워크인지, Apply를 눌렀는지 확인.
- **액션이 느리게 동작하나요?** → UDP는 fire-and-forget이라 보통 즉시 동작한다. 버튼 탭 사이에 15ms 폴링 스로틀이 있다.
- **Wi-Fi가 바뀌었나요?** → 하단 바를 2.5초 길게 눌러 재설정.
- **오프라인 패킷 검증:** `python3 macro_pad_gui.py --test-packets`
- **오프라인 액션 검증 (디바이스 없이):** `python3 macro_pad_gui.py --test-action shortcut cmd+c` 또는 `--test-action text 안녕` — 격리 헬퍼 경로로 키보드 입력이 실제 동작하는지 확인한다.
