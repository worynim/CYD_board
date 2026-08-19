# CYD Wireless Portable Monitor (CYD 무선 휴대용 모니터)

CYD(ESP32-2432S028, 2.8인치 320x240 LCD) 보드를 활용하여 Mac 및 Windows PC의 화면을 Wi-Fi로 실시간 스트리밍받아 출력하는 초저지연 무선 포터블 모니터 프로젝트입니다.

---

## 🌟 주요 기능 (Key Features)

1. **초저지연 초고속 스트리밍 (Ultra Low-Latency UDP)**
   - `AsyncUDP` 비동기 통신과 80MHz 고속 SPI + 하드웨어 DMA 전송 파이프라인을 적용하여 수십 밀리초(ms) 단위의 실시간 미러링 제공.
2. **터치스크린 Wi-Fi 스캔 및 설정 UI**
   - 주변 Wi-Fi AP를 자동 검색하여 터치 리스트 형태로 선택 가능.
   - 온스크린 가상 키보드를 통해 비밀번호를 입력하고 NVS 플래시(Preferences)에 영구 저장.
   - 동작 중 화면을 3초간 길게 터치(Long-touch)하면 언제든 Wi-Fi 재설정 모드 진입.
3. **화면 비율(Aspect Ratio) 제어 지원**
   - **원본 비율 유지 (Letterbox/Pillarbox)**: 모니터 비율을 유지하고 남는 공간에 깔끔한 검은색 여백 처리.
   - **꽉 채우기 (Stretch)**: CYD 해상도에 맞게 늘려서 화면 가득 채움.
   - **비율 맞춤 크롭 (Crop & Fill)**: 왜곡 없이 중앙 영역을 잘라내어 화면을 가득 채움.
4. **원격 화면 회전 (Rotation)**
   - 자동 감지(모니터 가로/세로 비율에 맞춰 자동 전환) 및 가로/세로/180도 반전 수동 제어 지원.
5. **호스트 GUI 컨트롤 패널 (Tkinter 기반)**
   - IP, 포트, 모니터 선택, 화면 비율, 회전 방향, JPEG 품질, 목표 FPS 조절 및 실시간 네트워크 대역폭 통계 모니터링 제공.
   - CYD 화면 우측 하단 FPS 오버레이 표시 ON/OFF 지원 (기본 OFF).

---

## 📁 프로젝트 구조 (Project Structure)

```text
Portable monitor/
├── CYD_Wireless_Monitor/
│   └── CYD_Wireless_Monitor.ino   # CYD(ESP32) 아두이노 펌웨어
├── host_streamer/
│   ├── streamer_gui.py            # Mac/Windows 호스트 GUI 스트리머 (Tkinter)
│   └── requirements.txt           # Python 필수 라이브러리 목록
├── PLAN.md                        # 2초 딜레이/네트워크 유실 진단 및 FEC 계획
├── OPTIMIZATION_PLAN.md           # 코드 최적화 로드맵
└── README.md                      # 프로젝트 설명서 (본 파일)
```

---

## 🛠️ 준비 사항 및 환경 설정

### 1. CYD(ESP32) 펌웨어 업로드 (Arduino IDE)

1. **필요 라이브러리 설치**:
   - Arduino IDE 메뉴: `스케치` → `라이브러리 포함하기` → `라이브러리 관리...`
   - **`LovyanGFX`** 검색 후 최신 버전 설치.
   - **`JPEGDEC`** (Larry Bank) 검색 후 설치 — 화면 렌더링에 사용되는 고속 JPEG 디코더.
   - *(AsyncUDP 및 Preferences 라이브러리는 ESP32 보드 패키지에 기본 내장되어 있습니다)*

2. **Arduino IDE 보드 설정**:
   - **보드 (Board)**: `ESP32 Dev Module` (또는 `ESP32-WROOM-DA Module`)
   - **Flash Size**: `4MB (32Mb)`
   - **Partition Scheme**: `Huge APP (3MB No OTA/1MB SPIFFS)` 또는 `Default 4MB`
   - **Upload Speed**: `921600` (실패 시 `115200`)

3. **업로드 및 초기 Wi-Fi 연결**:
   - [CYD_Wireless_Monitor.ino](file:///Users/jjgb/Library/CloudStorage/GoogleDrive-worynim@gmail.com/내%20드라이브/__전자공방/study/VibeCoding/CYD_board/Portable%20monitor/CYD_Wireless_Monitor/CYD_Wireless_Monitor.ino)를 열고 업로드합니다.
   - 보드 부팅 후 화면에 나타나는 Wi-Fi 검색 목록에서 공유기를 선택하고 비밀번호를 입력합니다.
   - 연결이 완료되면 화면에 **할당된 IP 주소**가 표시됩니다.

---

### 2. 호스트 PC 실행 (Mac / Windows)

1. **Python 라이브러리 설치**:
   ```bash
   cd host_streamer
   pip install -r requirements.txt
   ```

2. **GUI 컨트롤 패널 실행**:
   ```bash
   python3 streamer_gui.py
   ```

3. **스트리밍 시작**:
   - GUI 창에서 CYD 화면에 표시된 **IP 주소**를 확인합니다.
   - 캡처할 **모니터**, **화면 비율 모드**, **회전 방향**, **품질/FPS**를 설정한 후 **`▶ 스트리밍 시작`** 버튼을 클릭합니다.

---

## 💡 유용한 팁 (Tips & Troubleshooting)

- **Wi-Fi 재설정**: CYD 화면을 **3초 이상 길게 터치(Long-touch)**하거나 부팅 시 화면을 누르고 있으면 Wi-Fi 검색 및 재설정 화면으로 진입합니다.
- **가로선/딜레이 최소화**: Wi-Fi 공유기와 CYD 보드, PC 간 신호가 양호한 상태에서 구동하는 것을 권장하며, 품질 슬라이더는 `40~50`으로 설정할 때 가장 최적의 대역폭과 프레임 레이트를 발휘합니다.
