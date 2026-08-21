# CYD Board Projects

**ESP32-2432S028 (CYD, 2.8인치 320×240 터치 LCD)** 한 보드를 활용한 두 개의 독립 프로젝트 저장소입니다.
각 프로젝트는 **ESP32 펌웨어(C++/Arduino) + Python 호스트** 구조로, 둘 다 **UDP**로 통신합니다.

> 두 프로젝트는 같은 보드를 쓰므로 **한 번에 하나의 펌웨어만** 올려 사용할 수 있습니다.
> 펌웨어/호스트는 서로 완전히 독립적입니다.

## 프로젝트 목록

| 프로젝트 | 설명 |
|----------|------|
| [📺 Portable monitor](Portable%20monitor/README.md) | CYD를 **무선 휴대용 모니터**로 — PC 화면을 Wi-Fi로 실시간 스트리밍 (UDP `8888`) |
| [⌨️ CYD_Macro_Pad](CYD_Macro_Pad/README.md) | CYD를 **무선 매크로 패드(Stream Deck 클론)**로 — 버튼 탭으로 Mac 액션 실행 (UDP `8890`) |

---

## 📺 Portable monitor — 무선 휴대용 모니터

PC(Mac/Windows)의 화면을 Wi-Fi로 실시간 스트리밍해 CYD에 표시하는 초저지연 포터블 모니터.

- **주요 기능:** 초저지연 UDP 스트리밍(JPEGDEC 고속 디코딩), 화면 비율 모드(letterbox/stretch/crop), 화면 회전, 호스트 GUI 컨트롤 패널, 화면 밝기 조절
- **실행:** `Portable monitor/host_streamer`에서 `pip install -r requirements.txt` → `python3 streamer_gui.py`
- **구성:** `CYD_Wireless_Monitor.ino`(펌웨어) + `host_streamer/streamer_gui.py`(호스트)
- 자세한 내용 → [Portable monitor/README.md](Portable%20monitor/README.md)

## ⌨️ CYD_Macro_Pad — 무선 매크로 패드

CYD 화면에 4×3 터치 버튼 그리드를 띄우고, 버튼을 누르면 Mac 호스트가 설정된 액션(단축키/문구/앱 실행)을 실행하는 Stream Deck 클론.

- **주요 기능:** 최대 8페이지×12버튼, 페이지 탭 드래그 순서 변경(자동 동기화) + 페이지 롤링 이동(마지막↔첫 페이지), F1~F20 단축키, 이미지 버튼/한글 라벨(고품질, 청킹 전송), 액션 실행 피드백(초록/빨강 점멸), 백라이트 자동 조절(CDS), 설정 내보내기/가져오기, 디바이스 내장 저장(LittleFS) + 디바이스에서 불러오기
- **실행:** `CYD_Macro_Pad/host_macro_pad`에서 `pip install -r requirements.txt` → `python3 macro_pad_gui.py`
  (macOS 키보드 입력은 **손쉬운 사용(Accessibility) 권한** 필요)
- **구성:** `CYD_Macro_Pad.ino`(펌웨어, LovyanGFX+JPEGDEC) + `host_macro_pad/macro_pad_gui.py`(호스트)
- 자세한 내용 → [CYD_Macro_Pad/README.md](CYD_Macro_Pad/README.md)

---

## 공통 사항

- **펌웨어 업로드 (Arduino IDE):** 보드 `ESP32 Dev Module` · Flash `4MB (32Mb)` · 업로드 속도 `921600` (불안정하면 `115200`)
  - 파티션은 `Huge APP (3MB No OTA/1MB SPIFFS)` **권장**이지만 **필수는 아니다** — 데이터 파티션이 있는
    스킴(예: 기본 `Default 4MB`, SPIFFS 1.5MB)이면 그대로 동작한다. 데이터 파티션이 없는 스킴에서는
    내장 저장(LittleFS)만 비활성되고 나머지는 정상 동작한다.
- **Wi-Fi 설정:** 첫 부팅 시 터치 UI로 AP 선택 + 비밀번호 입력 (NVS 저장). 나중에 바꾸려면 화면을 길게 눌러 재설정.
- **상세 가이드:** `CLAUDE.md` 에 두 프로젝트의 아키텍처·프로토콜·수정 시 주의사항이 정리되어 있습니다.
