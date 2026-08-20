/**
 * @file CYD_Macro_Pad.ino
 * @brief CYD(ESP32-2432S028) 무선 매크로 패드 (커스텀 LGFX 클래스 + 80MHz SPI/DMA 안정화 버전)
 *
 * 1. 커스텀 LGFX 클래스로 패널(ILI9341/ST7789)을 명시 지정하고 80MHz SPI + 하드웨어 DMA 구동
 *    (LGFX_AUTODETECT 미사용 — 패널은 아래 CYD_PANEL_ST7789 매크로로 선택)
 * 2. 주변 Wi-Fi AP 터치 목록 선택 및 비밀번호 가상 키보드 입력
 * 3. 호스트가 보내는 버튼 라벨 설정 수신 (UDP 8890) → 4x3 터치 버튼 그리드 렌더
 * 4. 버튼 터치 → 호스트로 UDP 이벤트 전송 ("MPAD") → 호스트가 단축키/문구/앱 실행
 *
 * 의존 라이브러리: LovyanGFX, JPEGDEC (버튼 이름 이미지 렌더, G)
 * 보드 설정: ESP32 Dev Module · Flash 4MB · Partition "Huge APP (3MB No OTA/1MB SPIFFS)"
 *
 * 와이어 프로토콜 (host_macro_pad/macro_pad_gui.py와 정확히 일치해야 함):
 *   - 설정 패킷 (호스트→디바이스, v3): >IBBBB magic=0x4D434647("MCFG"), page, count, num_pages, rsvd
 *                                 + page_name_len u8 + page_name(≤PAGE_NAME_MAX, 0 = 변경 없음) [A]
 *                                 + count개 엔트리(>BBBBB + label + action_value:
 *                                   button_id, label_len, color_idx, action_type, action_len)
 *                                   라벨 ≤24B, action_value ≤ACTION_VAL_MAX(128)B
 *                                   action_type: 0=shortcut 1=text 2=app [H]
 *                                 count=0 = ACK(설정 없음, 호스트 IP 학습용) [H]
 *                                 한 페이지가 MTU 초과 시 count/bid로 여러 패킷으로 분할 [H]
 *   - 설정 요청 (호스트→디바이스, H): >IBBBB magic=0x4D524551("MREQ"), 0,0,0,0
 *                                 → 디바이스가 MCFG 포맷 config 덤프 + MIMG 이미지 덤프를 회신
 *   - 이미지 패킷 (호스트→디바이스, G): >IBBBB magic=0x4D494D47("MIMG"), page, button_id, format, 0
 *                                 format: 0 = JPEG 바이트(버튼 71x61, 단일 UDP 패킷 ≤1400B)
 *                                         1 = 이미지 제거(clear, 페이로드 없음) → 텍스트/색 사각형 폴백
 *                                 — 한글 라벨/업로드 이미지 버튼은 설정 푸시 후 전송, ASCII/빈 라벨은 clear.
 *                                 [H] LittleFS(/btns/p{p}_{b}.jpg)에 저장, 현재 페이지만 RAM 캐시.
 *   - 이벤트 패킷 (디바이스→호스트): >IBBBB magic=0x4D504144("MPAD"), page, button_id, 0, 0
 *   - 디스커버리 비콘 (디바이스→서브넷 브로드캐스트): >IBBBB magic=0x4D504245("MPBE"), 0, 0, 0, 0
 *     Wi-Fi 연결 후 3초 주기로 전송 — 호스트 리스너가 ACK(MCFG count=0)로 응답 [H]
 *   - 디바이스는 가장 최근 설정 패킷의 소스 IP/포트로 이벤트를 전송
 *   - [H] 전체 설정은 LittleFS /config.bin에 저장 — 부팅/페이지 전환 시 플래시에서 복원(오프라인 동작).
 */

#include <WiFi.h>
#include <AsyncUDP.h>
#include <Preferences.h>
#include <esp_wifi.h>
#include <WiFiUdp.h>
#include <vector>
#include <math.h>      // [PLAN 7] roundButtonCorners() 코너 비트 반폭 계산용 sqrtf
#include <LovyanGFX.hpp>
#include <JPEGDEC.h>   // [G] 버튼 이름 이미지(JPEG) 디코더. Arduino 라이브러리 매니저 "JPEGDEC" 필요.
#include <LittleFS.h>  // [H] 전체 설정/이미지 내장 저장 (Huge APP 파티션의 1MB SPIFFS 파티션을 마운트)

// ==========================================
// 1. 객체 및 설정 정의
// ==========================================
// LGFX 커스텀 설정: 패널 SPI를 80MHz + DMA로 직접 구동 (아래 CYD_SPI_FREQ_WRITE 참조).
// 패널 종류 선택 — 현재 이 보드는 ST7789 패널이므로 CYD_PANEL_ST7789가 활성화되어 있다.
// ILI9341 패널 보드라면 아래 #define을 주석 처리하면 된다 (offset_rotation도 함께 전환됨):
//   - ST7789 : offset_rotation = 0 (아래 패널 config)
//   - ILI9341: offset_rotation = 2
#define CYD_PANEL_ST7789

// 화면 SPI 클럭. 노이즈/색상 이상/글리치 발생 시 아래 값으로 낮춰 테스트하세요:
//   80000000 (80MHz) → 60000000 (60MHz) → 40000000 (40MHz, 자동감지 기본)
#define CYD_SPI_FREQ_WRITE 80000000
class LGFX : public lgfx::LGFX_Device
{
  lgfx::Bus_SPI _bus_instance;
#ifdef CYD_PANEL_ST7789
  lgfx::Panel_ST7789 _panel_instance;
#else
  lgfx::Panel_ILI9341 _panel_instance;
#endif
  lgfx::Light_PWM _light_instance;
  lgfx::Touch_XPT2046 _touch_instance;

public:
  LGFX(void)
  {
    { // 버스(패널 SPI): 80MHz + DMA
      auto cfg = _bus_instance.config();
      cfg.spi_host    = SPI2_HOST;          // VSPI_HOST
      cfg.spi_mode    = 0;
      cfg.freq_write  = CYD_SPI_FREQ_WRITE;  // (자동감지 기본 40MHz → 상향)
      cfg.freq_read   = 16000000;
      cfg.spi_3wire   = false;
      cfg.use_lock    = true;
      cfg.dma_channel = SPI_DMA_CH_AUTO;    // 하드웨어 DMA 활성화
      cfg.pin_sclk    = 14;
      cfg.pin_mosi    = 13;
      cfg.pin_miso    = 12;
      cfg.pin_dc      = 2;
      _bus_instance.config(cfg);
      _panel_instance.setBus(&_bus_instance);
    }
    { // 패널
      // (설치된 LovyanGFX 버전은 Panel config에 freq_write가 없음 → 클럭은 버스 config에서만 설정)
      auto cfg = _panel_instance.config();
      cfg.pin_cs          = 15;
      cfg.pin_rst         = -1;
#ifdef CYD_PANEL_ST7789
      cfg.offset_rotation = 0;              // ST7789: 패널 오프셋 0
#else
      cfg.offset_rotation = 2;              // ILI9341: CYD 오프셋 2
#endif
      cfg.invert          = false;
      cfg.rgb_order       = false;
      cfg.dlen_16bit      = false;
      cfg.bus_shared      = false;
      _panel_instance.config(cfg);
    }
    { // 백라이트 (GPIO21 PWM)
      auto cfg = _light_instance.config();
      cfg.pin_bl       = 21;
      cfg.invert       = false;
      cfg.freq         = 44100;
      cfg.pwm_channel  = 7;
      _light_instance.config(cfg);
      _panel_instance.setLight(&_light_instance);
    }
    { // 터치 XPT2046: 소프트웨어 SPI (패널 SPI와 독립 → 클럭 상향 영향 없음)
      auto cfg = _touch_instance.config();
      cfg.x_min       =  300;
      cfg.x_max       = 3900;
      cfg.y_min       = 3700;
      cfg.y_max       =  200;
      cfg.pin_int     = -1;
      cfg.bus_shared  = false;
      cfg.spi_host    = -1;                 // -1 = 소프트웨어 SPI
#ifdef CYD_PANEL_ST7789
      cfg.offset_rotation = 2;
#else
      cfg.offset_rotation = 0;
#endif
      cfg.pin_sclk    = 25;
      cfg.pin_mosi    = 32;
      cfg.pin_miso    = 39;
      cfg.pin_cs      = 33;
      _touch_instance.config(cfg);
      _panel_instance.setTouch(&_touch_instance);
    }
    setPanel(&_panel_instance);
  }
};

static LGFX lcd;

// ==========================================
// 2. 와이어 프로토콜 상수 (호스트와 일치)
// ==========================================
#define UDP_PORT         8890            // 매크로 패드 전용 포트 (스트리밍 8888과 독립)
#define MAGIC_CONFIG     0x4D434647      // "MCFG" 설정 패킷 (호스트→디바이스)
#define MAGIC_EVENT      0x4D504144      // "MPAD" 이벤트 패킷 (디바이스→호스트)
#define MAGIC_BEACON     0x4D504245      // "MPBE" 디스커버리 비콘 (디바이스→브로드캐스트)
#define MAGIC_OK         0x4D504F4B      // "MPOK" 액션 성공 피드백 (호스트→디바이스, B)
#define MAGIC_ERR        0x4D504552      // "MPER" 액션 실패 피드백 (호스트→디바이스, B)
#define MAGIC_IMAGE      0x4D494D47      // "MIMG" 버튼 이름 이미지 (호스트→디바이스, G)
#define FEEDBACK_MS      300             // 버튼 플래시 지속 시간 (B)
#define BEACON_INTERVAL_MS  3000         // 비콘 재전송 주기 (호스트가 IP 자동 검색)
#define MAX_PAGES        8               // 최대 페이지 수 (호스트와 일치)
#define DEFAULT_PAGES    2               // 부팅 시 페이지 수 (numPages 초기값)
#define GRID_COLS        4
#define GRID_ROWS        3
#define BUTTONS_PER_PAGE (GRID_COLS * GRID_ROWS)  // 12
#define LABEL_MAX        24              // 라벨 최대 바이트 (호스트와 동일)
#define PAGE_NAME_MAX    20              // 페이지 이름 최대 바이트 (호스트와 동일, A)
#define BTN_COLOR_COUNT  10              // 버튼 팔레트 색 수 (호스트 COLOR_NAMES와 일치)
#define IMG_MAX_BYTES    2048            // [G] 단일 이미지(JPEG) 바이트 상한 (방어 — 호스트는 ≤1400 맞춤)
#define IMG_REDRAW_DELAY 50              // [G] 이미지 배치 도착 후 재렌더 debounce(ms) — 버스트 1회 합침
#define IMG_BUDGET_BYTES (96 * 1024)     // [G] 이미지 RAM 총 예산 (내부 DRAM, 최대 96장×1KB).
                                         //     [H] 거부 로직 제거 — 이미지는 LittleFS(/btns)에 저장되고
                                         //     현재 페이지만 RAM에 캐시(≤12×1400B)하므로 전역 예산 불필요.
#define MAGIC_REQUEST     0x4D524551      // "MREQ" 설정 덤프 요청 (호스트→디바이스, H)
#define ACTION_VAL_MAX    128             // 액션 값(action_value) 최대 바이트 (호스트와 일치, H)
#define CFG_MAGIC         0x4D504346      // "MPCF" config.bin 매직 (H)
#define CFG_VERSION       3               // config.bin 버전 (H)
#define CONFIG_BIN_PATH   "/config.bin"   // 전체 설정 저장 파일 (H)
#define BTNS_DIR          "/btns"         // 버튼 이미지 저장 디렉터리 (H)
#define CONFIG_SAVE_DEBOUNCE_MS 500       // [H] config.bin 저장 debounce — 다중 패킷 푸시를 1회 쓰기로 (웨어 방지)
#define LABELS_REDRAW_DELAY 50            // [H] labelsDirty 재렌더 debounce — 다중 패킷 푸시 플리커 방지
#define MCFG_CHUNK_MAX      1200          // [H] MCFG 푸시/덤프 청크 상한 (UDP 단일 패킷 안전)

// ==========================================
// 3. UI 지오메트리 (320x240 가로 정방향, 회전 코드 3)
// ==========================================
#define STATUS_H        28
#define STATUS_TOP      212              // 상태바 상단 y
#define GRID_TOP        6
#define GRID_LEFT       6
#define GRID_RIGHT      314              // 그리드 영역 배타적 우측
#define GRID_BOTTOM     206              // 그리드 영역 배타적 하단
#define BTN_W           71
#define BTN_H           61
#define BTN_GAP_X       8
#define BTN_GAP_Y       8
// [PLAN 7] 버튼 모서리 라운드 반경. radius 6은 작아서 Chamfer처럼 보이고, 이미지 버튼은
//     호스트가 JPEG에 구운 라운드(radius 6)가 4:2:0 손실 압축 후 거의 사라져 각지게 보였다.
//     10이면 압축 후에도/텍스트 버튼 모두 확실한 라운드가 보인다. 호스트 BTN_RADIUS와 일치.
#define BTN_RADIUS      10
#define PREV_X          4
#define PREV_Y          214
#define PREV_W          96              // [PLAN 6] 페이지 전환 버튼 좌우 확대 (누르기 쉽게)
#define PREV_H          22
#define NEXT_X          220
#define NEXT_Y          214
#define NEXT_W          96
#define NEXT_H          22

#define LONG_PRESS_MS   2500             // 상태바 길게 → Wi-Fi 재설정
#define MAX_TAP_MS      800              // 이보다 길게 누른 탭은 무시

// [C] 백라이트 절전 + 주변 밝기 자동 조절
#define LDR_PIN         34               // GPIO34 CDS 조도 센서 (아날로그 ADC1)
#define IDLE_DIM_MS     60000            // 무터치 60s 후 백라이트 디밍 (off 아님)
#define BRIGHT_FULL     200              // 기본 밝기 (최대)
#define BRIGHT_DIM      40               // 디밍 밝기
#define BRIGHT_MIN      60               // 조도 기반 밝기 하한 (어두운 곳)

// ==========================================
// 3.5 버튼 색상 팔레트 (호스트 COLOR_NAMES/COLOR_HEX와 순서·값 정확히 일치)
// ==========================================
// 버튼 구분용 10색. textWhite=false면 검정 글자(밝은 배경), true면 흰 글자.
struct BtnColor {
  uint8_t r, g, b;
  bool    textWhite;
};
static const BtnColor BTN_PALETTE[BTN_COLOR_COUNT] = {
  {100, 116, 139, true },   //  0 gray   #64748B
  {239,  68,  68, true },   //  1 red    #EF4444
  {249, 115,  22, true },   //  2 orange #F97316
  {234, 179,   8, true },   //  3 yellow #EAB308
  { 34, 197,  94, true },   //  4 green  #22C55E
  { 20, 184, 166, true },   //  5 teal   #14B8A6
  { 59, 130, 246, true },   //  6 blue   #3B82F6
  {168,  85, 247, true },   //  7 purple #A855F7
  {236,  72, 153, true },   //  8 pink   #EC4899
  {248, 250, 252, false},   //  9 white  #F8FAFC → 검정 글자
};

// ==========================================
// 4. 전역 상태
// ==========================================
AsyncUDP udp;                  // 설정 수신 (listen 전용)
WiFiUDP  udpSend;              // 이벤트 전송 (AsyncUDP connect()의 단일연결/수신필터 문제 회피)
Preferences prefs;

String stored_ssid = "";
String stored_pass = "";

// 버튼 라벨/색 저장소 (호스트가 설정 패킷으로 채움)
char labels[MAX_PAGES][BUTTONS_PER_PAGE][LABEL_MAX + 1] = {};
uint8_t btnColors[MAX_PAGES][BUTTONS_PER_PAGE] = {};   // 0..BTN_COLOR_COUNT-1 (기본 0=gray)
uint8_t numPages = DEFAULT_PAGES;   // 현재 페이지 수 (설정 패킷 헤더로 갱신)
volatile bool labelsDirty = false;   // AsyncUDP 콜백에서 세우고 loop()에서 소비
uint8_t currentPage = 0;
char pageNames[MAX_PAGES][PAGE_NAME_MAX + 1] = {};   // 페이지 이름 (설정 패킷으로 채움, A)
Preferences prefsPad;   // 마지막 페이지/페이지 수 복원용 NVS (네임스페이스 "cyd_mpad", E)

// [H] 버튼 액션 저장 (호스트가 v3 MCFG로 전송). 실행은 항상 호스트 — 여기선 저장만.
uint8_t btnActionType[MAX_PAGES][BUTTONS_PER_PAGE] = {};   // 0=shortcut 1=text 2=app
char    btnActionVal[MAX_PAGES][BUTTONS_PER_PAGE][ACTION_VAL_MAX + 1] = {};   // 96×129 = 12,384B

// [H] LittleFS 상태 (config.bin / /btns 이미지)
bool fsMounted = false;                        // LittleFS 마운트 성공 여부
volatile bool configSaveDirty = false;         // 콜백이 세우고 loop()가 디바운스 후 1회 저장
volatile unsigned long configDirtyTime = 0;    // 마지막 설정 변경 시각 (저장 디바운스 기준)
volatile unsigned long labelsDirtyTime = 0;    // 마지막 라벨 변경 시각 (재렌더 디바운스 기준, H)

// 호스트 주소 (가장 최근 설정 패킷의 소스로 학습)
IPAddress hostIP;
uint16_t hostPort = 0;
bool hostKnown = false;

// [STAT] 계측
static uint32_t statEvents = 0;    // 전송한 이벤트 수
static uint32_t statConfigs = 0;   // 수신한 설정 패킷 수
static uint32_t statBeacons = 0;   // 보낸 디스커버리 비콘 수

// [C] 백라이트 절전 + 조도 자동 밝기 상태
static unsigned long lastTouchTime = 0;   // 마지막 터치 시각 (무터치 디밍 판정)
static uint8_t lastBrightness = 0;        // 현재 백라이트 밝기 (변경 시에만 setBrightness)
static unsigned long lastLdrTime = 0;     // 조도 ADC 읽기 throttle (1초)

// [B] 실행 결과 피드백 (MPOK/MPER 수신 → 버튼 플래시)
static int8_t feedbackPage = -1;   // -1 = 플래시 없음
static int8_t feedbackBtn = -1;
static bool feedbackOk = false;
static unsigned long feedbackUntil = 0;
static bool feedbackPending = false;   // AsyncUDP 콜백이 세우고 loop()가 소비 (레이스 방지)

// [G] 버튼 이름 이미지(JPEG) 저장소 — 호스트 MIMG 패킷이 채운다. RAM 동적 할당.
// (PLAN H에서 LittleFS 저장으로 이전 예정 — 여기선 RAM 캐시. 부팅 후 첫 푸시까지는 텍스트 폴백)
static JPEGDEC jpeg;                                  // JPEG 디코더 (JPEGDEC, Larry Bank)
uint8_t* imgJpeg[MAX_PAGES][BUTTONS_PER_PAGE] = {};   // JPEG 바이트 버퍼 (nullptr = 없음)
uint16_t imgSize[MAX_PAGES][BUTTONS_PER_PAGE] = {};   // 버퍼 바이트 수
uint32_t imgHeapUsed = 0;                             // 이미지 저장에 쓰인 힙 총량(바이트)
volatile bool imagesDirty = false;                    // 이미지 도착 → loop()가 debounce 후 재렌더
volatile unsigned long lastImageTime = 0;             // 마지막 이미지 도착 시각 (debounce 기준)
static uint32_t statImgRecv = 0;                      // [STAT] 수신 이미지 패킷 수
static uint32_t statImgNew = 0;                       // [STAT] 실제 저장(변경)된 이미지 수

// [G] 이미지 패킷을 loop()로 넘기는 pending 큐. AsyncUDP 콜백(loop()와 다른 코어)은
//     원본 JPEG를 임시 버퍼에 복사해 여기 넣기만 하고, imgJpeg/imgSize/imgHeapUsed 변경과
//     free는 전부 loop()의 applyPendingImages()가 단일 task로 수행한다. 코어 간 공유 상태를
//     없애 이중 해제/use-after-free(heap_caps_free 어설션 → 무한 리셋)를 원천 차단한다.
#define IMG_PENDING_QUEUE 128                         // 배치(96개 최대) 초과 여유
typedef struct {
  uint8_t  page, bid;        // 대상 버튼
  uint8_t  fmt;              // 0 = JPEG 저장 · 1 = clear
  uint8_t* buf;              // fmt=0: JPEG 바이트 (malloc) — 소유권은 loop()로 이전
  uint16_t len;              // fmt=0: JPEG 길이
} PendingImage;
static PendingImage imgPending[IMG_PENDING_QUEUE] = {};
static volatile uint16_t imgPendingHead = 0, imgPendingTail = 0;
static portMUX_TYPE imgPendingMux = portMUX_INITIALIZER_UNLOCKED;

// [H] MREQ 수신 → loop()로 이전 (AsyncUDP 콜백에서 LittleFS I/O/UDP 전송 금지 — 교차 코어 안전).
//     콜백은 mux 아래 필드만 세우고, loop()가 소비해 실제 덤프를 전송한다.
typedef struct {
  IPAddress ip;          // 덤프를 보낼 호스트 주소
  uint16_t  port;
  bool      pending;     // loop()가 소비하면 false
} PendingDump;
static PendingDump dumpReq;
static portMUX_TYPE dumpReqMux = portMUX_INITIALIZER_UNLOCKED;

// ==========================================
// 5. 함수 선언
// ==========================================
void runTouchWifiSetup();
String selectWifiFromList();
String getTouchInput(const String& prompt, bool isPassword);
void drawKeyboard(const String& title, const String& currentVal, bool isPassword, bool isShift);
void onConfigPacket(AsyncUDPPacket packet);
void sendEvent(uint8_t page, uint8_t buttonId);
void sendBeacon();
void drawBootScreen(const char* msg);
void drawErrorScreen(const char* msg);
void drawReadyScreen();
void drawGrid(uint8_t page);
void drawButton(uint8_t page, uint8_t idx, bool pressed);
void drawButtonImage(uint8_t page, uint8_t idx, int x, int y, bool pressed);
void drawButtonText(uint8_t page, uint8_t idx, int x, int y, bool pressed);
void drawButtonFlash(uint8_t page, uint8_t idx, uint16_t fill);
void drawStatusBar();
void updateBrightness();
void onFeedbackPacket(AsyncUDPPacket packet);
void onImagePacket(AsyncUDPPacket packet);
void applyPendingImages();
int jpegBtnCallback(JPEGDRAW* pDraw);
uint16_t countImages();
void handleTouch();
bool hitButton(uint16_t tx, uint16_t ty, int* col, int* row);
// [H] LittleFS 헬퍼
int writeButtonImageFlash(uint8_t page, uint8_t bid, const uint8_t* buf, uint16_t len);   // 1=저장 0=동일생략 -1=실패
bool deleteButtonImageFlash(uint8_t page, uint8_t bid);
void loadPageImages(uint8_t page);
void freePageImages(uint8_t page);
bool loadConfigFromFlash(void);
void saveConfigToFlash(void);
void sendConfigDump(IPAddress ip, uint16_t port);
void sendImageDump(IPAddress ip, uint16_t port);

// ==========================================
// 6. Setup 함수
// ==========================================
void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n--- CYD Wireless Macro Pad ---");

  lcd.init();
  lcd.setRotation(3);   // 가로 정방향 (320x240)
  lcd.setBrightness(BRIGHT_FULL);   // [C] 기본 밝기
  lastTouchTime = millis();
  lastBrightness = BRIGHT_FULL;

  prefs.begin("cyd_wifi", false);
  stored_ssid = prefs.getString("ssid", "");
  stored_pass = prefs.getString("pass", "");

  // [E][H] 마지막 페이지/전체 설정 복원:
  //  - 전체 설정(라벨/색/액션/페이지 이름/num_pages)은 LittleFS /config.bin 우선 (H)
  //  - config.bin 부재/손상 시 NVS(cyd_mpad)의 페이지 수로 폴백 (E)
  //  - currentPage(마지막 페이지)는 항상 NVS lastPage에서 읽어 config.bin numPages에 클램프
  prefsPad.begin("cyd_mpad", false);
  uint8_t savedPage = prefsPad.getUChar("lastPage", 0);

  // [H] LittleFS 마운트 — udp.listen() 이전에 완료해 AsyncUDP 콜백과 FS/힙 레이스 방지.
  //     이 1MB 파티션은 이번 기능(H)이 처음 쓰는 영역이라, 미사용 잔재(이전 데이터/랜덤 비트)가
  //     있으면 "Corrupted dir pair"(-84 = LFS_ERR_CORRUPT)로 마운트에 실패한다. 일부 코어는
  //     자동 포맷을 하지 않으므로, 실패 시 포맷 후 1회 재시도해 자가치유한다. 그래도 실패하면
  //     순수 RAM 모드로 동작한다 (기존처럼 설정은 푸시로 유지, 저장/불러오기만 비활성).
  fsMounted = LittleFS.begin();
  if (!fsMounted) {
    Serial.println("[FS] LittleFS 마운트 실패 — 포맷 후 재시도");
    bool formatted = LittleFS.format();
    fsMounted = formatted && LittleFS.begin();
    if (!fsMounted) {
      Serial.println(formatted ? "[FS] 포맷 후에도 마운트 실패 — 저장 비활성 (RAM 모드)"
                               : "[FS] 포맷 실패 — 저장 비활성 (RAM 모드)");
    } else {
      Serial.println("[FS] 포맷 후 마운트 OK");
    }
  } else {
    Serial.println("[FS] LittleFS 마운트 OK");
  }
  if (fsMounted && !LittleFS.exists(BTNS_DIR)) LittleFS.mkdir(BTNS_DIR);

  bool loaded = false;
  if (fsMounted) loaded = loadConfigFromFlash();
  if (!loaded) {
    uint8_t savedPages = prefsPad.getUChar("numPages", DEFAULT_PAGES);
    if (savedPages < 1) savedPages = 1;
    if (savedPages > MAX_PAGES) savedPages = MAX_PAGES;
    numPages = savedPages;
  }
  if (savedPage >= numPages) savedPage = 0;
  currentPage = savedPage;

  // [H] 현재 페이지 이미지 플래시 → RAM 로드 (부팅 즉시 이전 화면 복원, 이미지 포함 오프라인)
  if (fsMounted) loadPageImages(currentPage);

  uint16_t tx, ty;
  bool forceSetup = lcd.getTouch(&tx, &ty);

  if (stored_ssid.length() == 0 || forceSetup) {
    runTouchWifiSetup();
  }

  drawBootScreen("Wi-Fi Connecting...");

  WiFi.setAutoReconnect(true);
  WiFi.persistent(true);
  WiFi.mode(WIFI_STA);
  WiFi.begin(stored_ssid.c_str(), stored_pass.c_str());

  int attempt = 0;
  while (WiFi.status() != WL_CONNECTED && attempt < 30) {
    delay(500);
    Serial.print(".");
    attempt++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    WiFi.setSleep(false);
    esp_wifi_set_ps(WIFI_PS_NONE);

    Serial.println("\n[WiFi] Connected!");
    Serial.print("[WiFi] IP Address: ");
    Serial.println(WiFi.localIP());

    sendBeacon();   // 부팅 직후 1회 디스커버리 비콘 (호스트 자동 검색용)

    if (udp.listen(UDP_PORT)) {
      Serial.printf("[AsyncUDP] Listening on port %d\n", UDP_PORT);
      udp.onPacket(onConfigPacket);
      drawReadyScreen();
      drawGrid(currentPage);
    } else {
      drawErrorScreen("AsyncUDP Bind Failed!");
    }
  } else {
    Serial.println("\n[WiFi] Connection Failed!");
    drawErrorScreen("Wi-Fi Connection Failed!\nTouch to Re-setup");

    while (!lcd.getTouch(&tx, &ty)) {
      delay(50);
    }
    runTouchWifiSetup();
    ESP.restart();
  }
}

// ==========================================
// 7. Wi-Fi 검색 및 터치 리스트 선택 화면 (소스 프로젝트에서 복사)
// ==========================================
String selectWifiFromList() {
  lcd.fillScreen(lcd.color565(15, 23, 42));

  lcd.fillRoundRect(8, 8, 304, 38, 6, lcd.color565(30, 41, 59));
  lcd.drawRoundRect(8, 8, 304, 38, 6, lcd.color565(59, 130, 246));
  lcd.setTextColor(TFT_WHITE);
  lcd.setTextSize(1);
  lcd.setTextDatum(MC_DATUM);
  lcd.drawString("Scanning Wi-Fi Networks...", 160, 27);

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  delay(100);

  int n = WiFi.scanNetworks();
  if (n <= 0) {
    lcd.drawString("No Networks Found. Touch to Retry.", 160, 120);
    uint16_t tx, ty;
    while (!lcd.getTouch(&tx, &ty)) delay(50);
    return selectWifiFromList();
  }

  std::vector<String> ssids;
  for (int i = 0; i < n; ++i) {
    String s = WiFi.SSID(i);
    if (s.length() > 0) {
      bool exists = false;
      for (const auto& existing : ssids) {
        if (existing == s) { exists = true; break; }
      }
      if (!exists && ssids.size() < 15) {
        ssids.push_back(s);
      }
    }
  }

  int page = 0;
  const int itemsPerPage = 4;
  int totalPages = (ssids.size() + itemsPerPage - 1) / itemsPerPage;

  while (true) {
    lcd.fillScreen(lcd.color565(15, 23, 42));

    lcd.fillRoundRect(8, 6, 304, 32, 4, lcd.color565(30, 41, 59));
    lcd.setTextColor(TFT_WHITE);
    lcd.setTextSize(1);
    lcd.setTextDatum(MC_DATUM);
    char titleBuf[64];
    snprintf(titleBuf, sizeof(titleBuf), "Select Wi-Fi (%d/%d)", page + 1, totalPages);
    lcd.drawString(titleBuf, 160, 22);

    int startIdx = page * itemsPerPage;
    for (int i = 0; i < itemsPerPage; i++) {
      int itemIdx = startIdx + i;
      int y = 44 + i * 40;
      if (itemIdx < ssids.size()) {
        lcd.fillRoundRect(8, y, 304, 36, 6, lcd.color565(51, 65, 85));
        lcd.drawRoundRect(8, y, 304, 36, 6, lcd.color565(100, 116, 139));

        lcd.setTextColor(lcd.color565(248, 250, 252));
        lcd.setTextSize(1);
        lcd.setTextDatum(ML_DATUM);
        String name = ssids[itemIdx];
        if (name.length() > 24) name = name.substring(0, 24) + "..";
        lcd.drawString(name, 20, y + 18);   // (기본 폰트: 이모지 미지원 → 제거)
      }
    }

    lcd.fillRoundRect(8, 190, 90, 42, 6, page > 0 ? lcd.color565(37, 99, 235) : lcd.color565(71, 85, 105));
    lcd.setTextColor(TFT_WHITE);
    lcd.setTextDatum(MC_DATUM);
    lcd.drawString("< Prev", 53, 211);

    lcd.fillRoundRect(106, 190, 108, 42, 6, lcd.color565(15, 118, 110));
    lcd.drawString("Re-Scan", 160, 211);    // (이모지 제거)

    lcd.fillRoundRect(222, 190, 90, 42, 6, page < totalPages - 1 ? lcd.color565(37, 99, 235) : lcd.color565(71, 85, 105));
    lcd.drawString("Next >", 267, 211);

    while (true) {
      uint16_t tx, ty;
      if (lcd.getTouch(&tx, &ty)) {
        while (lcd.getTouch(&tx, &ty)) delay(20);

        if (ty >= 44 && ty < 184) {
          int clickedItem = (ty - 44) / 40;
          int actualIdx = startIdx + clickedItem;
          if (actualIdx < ssids.size()) {
            return ssids[actualIdx];
          }
        }
        else if (ty >= 190 && ty <= 235) {
          if (tx >= 8 && tx < 98 && page > 0) {
            page--;
            break;
          } else if (tx >= 106 && tx < 214) {
            return selectWifiFromList();
          } else if (tx >= 222 && tx <= 312 && page < totalPages - 1) {
            page++;
            break;
          }
        }
      }
      delay(20);
    }
  }
}

// ==========================================
// 8. 터치 가상 키보드 (소스 프로젝트에서 복사)
// ==========================================
const char keys_lower[4][10] = {
  {'1','2','3','4','5','6','7','8','9','0'},
  {'q','w','e','r','t','y','u','i','o','p'},
  {'a','s','d','f','g','h','j','k','l','-'},
  {'z','x','c','v','b','n','m','_','.','@'}
};

const char keys_upper[4][10] = {
  {'!','@','#','$','%','^','&','*','(',')'},
  {'Q','W','E','R','T','Y','U','I','O','P'},
  {'A','S','D','F','G','H','J','K','L','/'},
  {'Z','X','C','V','B','N','M','+','=','?'}
};

void drawKeyboard(const String& title, const String& currentVal, bool isPassword, bool isShift) {
  lcd.fillScreen(lcd.color565(15, 23, 42));

  lcd.fillRoundRect(8, 8, 304, 52, 6, lcd.color565(30, 41, 59));
  lcd.drawRoundRect(8, 8, 304, 52, 6, lcd.color565(59, 130, 246));

  lcd.setTextColor(lcd.color565(148, 163, 184));
  lcd.setTextSize(1);
  lcd.setTextDatum(TL_DATUM);
  lcd.drawString(title, 16, 14);

  String displayVal = "";
  if (isPassword) {
    for (size_t i = 0; i < currentVal.length(); i++) displayVal += "*";
  } else {
    displayVal = currentVal;
  }
  if (displayVal.length() > 22) {
    displayVal = displayVal.substring(displayVal.length() - 22);
  }

  lcd.setTextColor(TFT_WHITE);
  lcd.setTextSize(2);
  lcd.drawString(displayVal + "_", 16, 32);

  for (int r = 0; r < 4; r++) {
    for (int c = 0; c < 10; c++) {
      int x = 8 + c * 30 + (c > 0 ? c * 1 : 0);
      int y = 68 + r * 30;
      char ch = isShift ? keys_upper[r][c] : keys_lower[r][c];

      lcd.fillRoundRect(x, y, 29, 27, 4, lcd.color565(51, 65, 85));
      lcd.drawRoundRect(x, y, 29, 27, 4, lcd.color565(100, 116, 139));
      lcd.setTextColor(TFT_WHITE);
      lcd.setTextSize(1);
      lcd.setTextDatum(MC_DATUM);
      lcd.drawChar(ch, x + 14, y + 13);
    }
  }

  lcd.fillRoundRect(8, 194, 60, 38, 4, isShift ? lcd.color565(37, 99, 235) : lcd.color565(71, 85, 105));
  lcd.setTextColor(TFT_WHITE);
  lcd.setTextDatum(MC_DATUM);
  lcd.drawString("Shift", 38, 213);

  lcd.fillRoundRect(74, 194, 85, 38, 4, lcd.color565(51, 65, 85));
  lcd.drawString("Space", 116, 213);

  lcd.fillRoundRect(165, 194, 65, 38, 4, lcd.color565(220, 38, 38));
  lcd.drawString("DEL", 197, 213);

  lcd.fillRoundRect(236, 194, 76, 38, 4, lcd.color565(22, 163, 74));
  lcd.drawString("OK", 274, 213);
}

String getTouchInput(const String& prompt, bool isPassword) {
  String input = "";
  bool isShift = false;
  drawKeyboard(prompt, input, isPassword, isShift);

  while (true) {
    uint16_t tx, ty;
    if (lcd.getTouch(&tx, &ty)) {
      while (lcd.getTouch(&tx, &ty)) delay(20);

      if (ty >= 68 && ty < 188) {
        int r = (ty - 68) / 30;
        int c = (tx - 8) / 31;
        if (r >= 0 && r < 4 && c >= 0 && c < 10) {
          char ch = isShift ? keys_upper[r][c] : keys_lower[r][c];
          input += ch;
          drawKeyboard(prompt, input, isPassword, isShift);
        }
      }
      else if (ty >= 194 && ty <= 235) {
        if (tx >= 8 && tx < 68) {
          isShift = !isShift;
          drawKeyboard(prompt, input, isPassword, isShift);
        }
        else if (tx >= 74 && tx < 159) {
          input += " ";
          drawKeyboard(prompt, input, isPassword, isShift);
        }
        else if (tx >= 165 && tx < 230) {
          if (input.length() > 0) {
            input.remove(input.length() - 1);
            drawKeyboard(prompt, input, isPassword, isShift);
          }
        }
        else if (tx >= 236 && tx <= 312) {
          break;
        }
      }
    }
    delay(20);
  }
  return input;
}

// Wi-Fi 설정 모드 진입. 부팅 시 터치(forceSetup) 또는 상태바 길게 누름 시 호출된다.
// 흐름: AP 목록 스캔 → 터치 선택 → 가상 키보드 비밀번호 입력 → NVS 저장 → (호출부에서 ESP.restart)
void runTouchWifiSetup() {
  lcd.setRotation(3);  // 설정 UI는 항상 가로 정방향(320x240, 코드 3) 기준으로 그린다

  String selectedSsid = selectWifiFromList();
  String new_pass = getTouchInput("Password for: " + selectedSsid, true);

  if (selectedSsid.length() > 0) {
    stored_ssid = selectedSsid;
    stored_pass = new_pass;

    prefs.putString("ssid", stored_ssid);
    prefs.putString("pass", stored_pass);
    Serial.println("[Setup] Wi-Fi 설정이 NVS 플래시에 저장되었습니다.");
  }
}

// ==========================================
// 9. 설정 패킷 수신 (호스트→디바이스)
// ==========================================
void onConfigPacket(AsyncUDPPacket packet) {
  size_t len = packet.length();
  if (len < 8) return;

  const uint8_t* d = packet.data();
  uint32_t magic = ((uint32_t)d[0] << 24) | ((uint32_t)d[1] << 16) | ((uint32_t)d[2] << 8) | (uint32_t)d[3];

  // [B] 액션 실행 결과 피드백 (MPOK/MPER): 성공/실패를 버튼 플래시로 표시
  if (magic == MAGIC_OK || magic == MAGIC_ERR) {
    onFeedbackPacket(packet);
    return;
  }

  // [G] 버튼 이름 이미지 (MIMG): JPEG 저장 (호스트가 설정 푸시 후 전송)
  if (magic == MAGIC_IMAGE) {
    onImagePacket(packet);
    return;
  }

  // [H] 설정 덤프 요청 (MREQ): 소스 IP/포트로 MCFG config 덤프 + MIMG 이미지 덤프 회신.
  //     실제 전송은 loop()가 수행 (AsyncUDP 콜백에서 LittleFS I/O/UDP 전송 금지).
  if (magic == MAGIC_REQUEST) {
    hostIP = packet.remoteIP();
    hostPort = packet.remotePort();
    hostKnown = true;
    portENTER_CRITICAL(&dumpReqMux);
    dumpReq.ip = packet.remoteIP();
    dumpReq.port = packet.remotePort();
    dumpReq.pending = true;
    portEXIT_CRITICAL(&dumpReqMux);
    return;
  }

  if (magic != MAGIC_CONFIG) return;

  // 호스트 주소 학습: 가장 최근 설정 패킷의 소스로 이벤트 전송 대상을 갱신
  hostIP = packet.remoteIP();
  hostPort = packet.remotePort();
  hostKnown = true;

  uint8_t page = d[4];
  uint8_t count = d[5];
  if (page >= MAX_PAGES) page = MAX_PAGES - 1;
  if (count > BUTTONS_PER_PAGE) count = BUTTONS_PER_PAGE;

  // 페이지 수 갱신 (헤더 3번째 필드 = num_pages) — 상태바 "PAGE x/y" 반영
  // [H] count==0(ACK)일 때는 상태 변화 없이 IP만 학습하도록 게이트 — 호스트 비콘 ACK가
  //     numPages를 잘못 클램프하지 않게 한다 (ACK의 num_pages는 항상 호스트의 실제 페이지 수).
  if (count > 0) {
    uint8_t np = d[6];
    if (np < 1) np = 1;
    if (np > MAX_PAGES) np = MAX_PAGES;
    if (np != numPages) {
      numPages = np;
      if (currentPage >= numPages) currentPage = numPages - 1;
      labelsDirty = true;
      labelsDirtyTime = millis();
      configSaveDirty = true;   // [H] numPages 변경도 config.bin에 반영
      configDirtyTime = millis();
    }
  }

  bool changed = false;
  // [A] page_name: 헤더(8B) 뒤에 page_name_len u8 + name bytes. len==0 = "변경 없음"(H 명세 동일).
  size_t off = 8;
  if (off + 1 > len) return;
  uint8_t nameLen = d[off];
  off += 1;
  if (nameLen > 0) {
    if (nameLen > PAGE_NAME_MAX) nameLen = PAGE_NAME_MAX;
    if (off + nameLen > len) nameLen = len - off;
    char tmpName[PAGE_NAME_MAX + 1];
    memcpy(tmpName, d + off, nameLen);
    tmpName[nameLen] = '\0';
    if (strcmp(tmpName, pageNames[page]) != 0) {
      strncpy(pageNames[page], tmpName, PAGE_NAME_MAX + 1);
      changed = true;
    }
    off += nameLen;
  }

  // 엔트리는 가변 길이(>BBBBB + label bytes + action_value bytes, v3) → 고정 오프셋이 아니라 순차 탐색해야 한다.
  for (uint8_t i = 0; i < count; i++) {
    if (off + 5 > len) break;
    uint8_t bid   = d[off];
    uint8_t llen  = d[off + 1];
    uint8_t col   = d[off + 2];
    uint8_t atype = d[off + 3];
    uint8_t alen  = d[off + 4];
    off += 5;
    if (bid >= BUTTONS_PER_PAGE) continue;
    if (col >= BTN_COLOR_COUNT) col = 0;
    if (atype > 2) atype = 0;                          // [H] 0=shortcut 1=text 2=app
    if (off + llen > len) llen = len - off;
    if (llen > LABEL_MAX) llen = LABEL_MAX;
    if (off + llen + alen > len) alen = len - off - llen;
    if (alen > ACTION_VAL_MAX) alen = ACTION_VAL_MAX;  // [H] action_value ≤ 128B

    if (btnColors[page][bid] != col) {
      btnColors[page][bid] = col;
      changed = true;
    }

    char tmp[LABEL_MAX + 1];
    memcpy(tmp, d + off, llen);
    tmp[llen] = '\0';

    if (strcmp(tmp, labels[page][bid]) != 0) {
      strncpy(labels[page][bid], tmp, LABEL_MAX + 1);
      changed = true;
    }
    off += llen;

    // [H] 액션 저장 — 실행은 항상 호스트(pynput/pbcopy 격리)가 하므로 여기선 저장만.
    char tav[ACTION_VAL_MAX + 1];
    memcpy(tav, d + off, alen);
    tav[alen] = '\0';
    off += alen;
    if (btnActionType[page][bid] != atype) {
      btnActionType[page][bid] = atype;
      changed = true;
    }
    if (strcmp(tav, btnActionVal[page][bid]) != 0) {
      strncpy(btnActionVal[page][bid], tav, ACTION_VAL_MAX + 1);
      changed = true;
    }
  }

  statConfigs++;
  if (changed) {
    labelsDirty = true;   // loop()가 디바운스 후 그리드 재렌더
    labelsDirtyTime = millis();
    configSaveDirty = true;   // [H] loop()가 디바운스 후 config.bin 1회 저장 (웨어 방지)
    configDirtyTime = millis();
    String hip = hostIP.toString();
    Serial.printf("[CFG] page=%u count=%u pages=%u host=%s:%u\n",
                  page, count, numPages, hip.c_str(), hostPort);
  }
}

// [B] 액션 실행 결과 피드백 수신 (호스트 → 디바이스): 해당 버튼을 초록(성공)/빨강(실패)으로 플래시
void onFeedbackPacket(AsyncUDPPacket packet) {
  const uint8_t* d = packet.data();
  uint32_t magic = ((uint32_t)d[0] << 24) | ((uint32_t)d[1] << 16) | ((uint32_t)d[2] << 8) | (uint32_t)d[3];
  bool ok = (magic == MAGIC_OK);
  uint8_t page = d[4];
  uint8_t btn = d[5];
  if (page >= MAX_PAGES || btn >= BUTTONS_PER_PAGE) return;

  // 현재 페이지의 버튼만 플래시 (다른 페이지면 화면에 안 보이므로 스킵)
  if (page != currentPage) return;

  feedbackPage = page;
  feedbackBtn = btn;
  feedbackOk = ok;
  feedbackUntil = millis() + FEEDBACK_MS;
  feedbackPending = true;   // loop()에서 실제 렌더 — AsyncUDP 콜백에서 직접 그리면 레이스
  Serial.printf("[FB] page=%u btn=%u %s\n", page, btn, ok ? "OK" : "ERR");
}

// ==========================================
// 9.5 버튼 이름 이미지 수신 (MIMG) — G
// ==========================================
// onImagePacket은 AsyncUDP 콜백에서 실행된다 (loop()와 다른 코어일 수 있음). 그래서
// imgJpeg/imgSize/imgHeapUsed/free를 절대 직접 건드리지 않고, 원본 JPEG를 임시 버퍼에
// 복사한 (page,bid,fmt,len)만 imgPending 큐에 넣는다. 실제 저장/해제/회계는 전부 loop()의
// applyPendingImages()가 단일 task로 수행 — 코어 간 경합으로 인한 이중 해제/use-after-free
// (heap_caps_free 어설션 → 무한 리셋)를 원천 차단한다.
void onImagePacket(AsyncUDPPacket packet) {
  size_t len = packet.length();
  if (len < 8) return;
  const uint8_t* d = packet.data();

  uint8_t page = d[4], bid = d[5], fmt = d[6];
  if (page >= MAX_PAGES || bid >= BUTTONS_PER_PAGE) return;

  PendingImage pi = { page, bid, fmt, nullptr, 0 };

  // [G] format 1 = 이미지 제거(clear): 해당 버튼은 펌웨어 텍스트/색 사각형으로 폴백.
  //     ASCII/빈 라벨 버튼에 대해 호스트가 매 푸시마다 보낸다. (한글→ASCII 전환 시
  //     스테일 이미지 제거) — 큐에 기록만 하고 free는 loop()가 수행한다.
  if (fmt == 1) {
    // 위 pi 기본값 그대로 (buf=nullptr, len=0)
  } else if (fmt == 0) {                       // format 0 = JPEG
    size_t jlen = len - 8;
    if (jlen == 0 || jlen > IMG_MAX_BYTES) return;
    uint8_t* buf = (uint8_t*)malloc(jlen);
    if (!buf) {
      Serial.println("[IMG] malloc 실패");
      return;
    }
    memcpy(buf, d + 8, jlen);
    pi.buf = buf;
    pi.len = (uint16_t)jlen;
  } else {
    return;                                    // 예약 형식
  }

  portENTER_CRITICAL(&imgPendingMux);
  uint16_t next = (uint16_t)((imgPendingHead + 1) % IMG_PENDING_QUEUE);
  if (next == imgPendingTail) {                // 가득 참 → 이번 요청은 폐기
    portEXIT_CRITICAL(&imgPendingMux);
    if (pi.buf) free(pi.buf);                  // loop()로 소유권을 넘기지 못했으므로 여기서 해제
    static bool warned = false;
    if (!warned) { Serial.println("[IMG] pending-queue overflow (요청 폐기)"); warned = true; }
    return;
  }
  imgPending[imgPendingHead] = pi;
  imgPendingHead = next;
  portEXIT_CRITICAL(&imgPendingMux);

  // 재렌더 여부(imagesDirty)는 applyPendingImages()가 실제 변경이 있을 때만 세운다.
  // 여기서 무조건 세우면 3초 비콘 재푸시(중복 이미지)마다 그리드가 전체 리렌더되어 깜빡인다.
  lastImageTime = millis();
}

// loop() 최상단에서 호출 — pending 큐를 비우며 모든 이미지 버퍼 변경/해제를 단일 task에서
// 수행한다. 여기서 free한 버퍼는 그려지기 전에 이미 imgJpeg에서 분리됐으므로 decode 중
// free 레이스가 없다. (기존 deferImgFree/drainImgFreeQueue의 교차 코어 경합을 제거)
void applyPendingImages() {
  while (true) {
    PendingImage pi;
    portENTER_CRITICAL(&imgPendingMux);
    if (imgPendingHead == imgPendingTail) {
      portEXIT_CRITICAL(&imgPendingMux);
      break;
    }
    pi = imgPending[imgPendingTail];
    imgPendingTail = (uint16_t)((imgPendingTail + 1) % IMG_PENDING_QUEUE);
    portEXIT_CRITICAL(&imgPendingMux);

    // 페이지 수 감소로 잘린 페이지 대상이면 폐기 (trim이 이미 해제한 범위)
    if (pi.page >= numPages) {
      if (pi.buf) free(pi.buf);
      continue;
    }

    if (pi.fmt == 1) {                         // clear: 플래시 파일 삭제 + 현재 페이지면 RAM 해제
      bool flashRemoved = deleteButtonImageFlash(pi.page, pi.bid);   // [H]
      uint8_t* cur = imgJpeg[pi.page][pi.bid];
      if (cur) {
        uint16_t sz = imgSize[pi.page][pi.bid];
        imgJpeg[pi.page][pi.bid] = nullptr;
        imgSize[pi.page][pi.bid] = 0;
        imgHeapUsed -= sz;
        free(cur);
        imagesDirty = true;                    // 실제 제거가 있었을 때만 재렌더 (중복 clear는 no-op)
        Serial.printf("[IMG] clear p=%u b=%u flash=%d\n", pi.page, pi.bid, (int)flashRemoved);
      }
    } else if (pi.fmt == 0 && pi.buf) {        // JPEG 저장
      uint8_t* cur = imgJpeg[pi.page][pi.bid];
      uint16_t curSize = imgSize[pi.page][pi.bid];
      statImgRecv++;

      // [H] 플래시 저장 (기존 파일과 memcmp → 동일하면 생략, 웨어 방지).
      //     비현재 페이지도 여기서 /btns/p{p}_{b}.jpg에 영구 저장된다.
      //     flashRes: 1=저장(신규/변경) 0=동일생략 -1=실패
      int flashRes = writeButtonImageFlash(pi.page, pi.bid, pi.buf, pi.len);

      // [H] RAM은 현재 페이지만 캐시 — 비현재 페이지는 플래시에만 저장하고
      //     페이지 전환 시 loadPageImages()로 로드한다 (오프라인 페이지 네비게이션).
      bool ramChanged = false;
      if (pi.page == currentPage) {
        // 동일 이미지면 스킵 — malloc/free 회전과 재렌더를 막는다 (변경 시에만 갱신 관례)
        if (cur && curSize == pi.len && memcmp(cur, pi.buf, pi.len) == 0) {
          free(pi.buf);                        // 중복 → 새 임시 버퍼만 폐기
        } else {
          imgSize[pi.page][pi.bid] = pi.len;   // 크기 → 포인터 순 (같은 세대 조합)
          imgJpeg[pi.page][pi.bid] = pi.buf;
          imgHeapUsed += (uint32_t)pi.len;
          imgHeapUsed -= curSize;
          statImgNew++;
          if (cur) free(cur);                  // 이전 버퍼 — 같은 task에서 해제
          ramChanged = true;
        }
      } else {
        // 비현재 페이지: 이전 캐시가 남아 있으면 해제 (플래시가 소스 — 다시 로드됨)
        if (cur) {
          imgJpeg[pi.page][pi.bid] = nullptr;
          imgSize[pi.page][pi.bid] = 0;
          imgHeapUsed -= curSize;
          free(cur);
        }
        free(pi.buf);                          // 임시 버퍼 소유권 해제
      }
      if (ramChanged) {
        imagesDirty = true;                    // 실제 저장/교체가 있었을 때만 재렌더
      }
      const char* flashLabel = (flashRes == 1) ? "저장" : (flashRes == 0 ? "동일생략" : "실패");
      Serial.printf("[IMG] p=%u b=%u %uB flash=%s(%d) ram=%d imgHeap=%uB\n",
                    pi.page, pi.bid, (unsigned)pi.len, flashLabel, flashRes, (int)ramChanged, (unsigned)imgHeapUsed);
    } else {
      if (pi.buf) free(pi.buf);                // 형식 불일치 등 — 방어적 해제
    }
  }
}

// [H] 버튼 이미지를 LittleFS /btns/p{page}_{bid}.jpg에 저장.
//     반환: 1=기록함(신규/변경), 0=동일 내용이라 생략(웨어 보호), -1=실패(FS 미마운트/쓰기 오류).
//     실패 시 구체적 원인을 [FS] 로그로 남긴다 (진단: 마운트 실패 vs 파일 오픈 실패 vs 부분 쓰기).
int writeButtonImageFlash(uint8_t page, uint8_t bid, const uint8_t* buf, uint16_t len) {
  if (!fsMounted) {
    Serial.printf("[FS] 이미지 저장 실패 — LittleFS 미마운트: /btns/p%u_%u.jpg (파티션 확인: Huge APP)\n",
                  page, bid);
    return -1;
  }
  char path[32];
  snprintf(path, sizeof(path), "/btns/p%u_%u.jpg", page, bid);
  if (LittleFS.exists(path)) {
    File f = LittleFS.open(path, "r");
    if (f) {
      bool same = (f.size() == len);
      if (same) {
        uint8_t chunk[256];
        uint16_t off = 0;
        while (same && off < len) {
          size_t rd = f.read(chunk, min((size_t)sizeof(chunk), (size_t)(len - off)));
          if (rd == 0) { same = false; break; }
          if (memcmp(chunk, buf + off, rd) != 0) same = false;
          off += (uint16_t)rd;
        }
      }
      f.close();
      if (same) return 0;       // 내용 동일 → 쓰지 않음 (웨어 보호)
    }
  }
  File w = LittleFS.open(path, "w");
  if (!w) {
    Serial.printf("[FS] 이미지 저장 실패 — %s 열기(쓰기) 오류\n", path);
    return -1;
  }
  size_t written = w.write(buf, len);
  w.close();
  if (written != len) {
    Serial.printf("[FS] 이미지 저장 실패 — 부분 쓰기 %u/%u (%s)\n", (unsigned)written, (unsigned)len, path);
    return -1;
  }
  return 1;
}

// [H] 버튼 이미지 파일 삭제. 파일이 있었고 삭제했으면 true, 없었으면 false.
bool deleteButtonImageFlash(uint8_t page, uint8_t bid) {
  if (!fsMounted) return false;
  char path[32];
  snprintf(path, sizeof(path), "/btns/p%u_%u.jpg", page, bid);
  if (!LittleFS.exists(path)) return false;
  LittleFS.remove(path);
  return true;
}

// [H] 페이지의 이미지를 플래시 → RAM으로 로드 (부팅/페이지 전환 시). 이미 로드된 슬롯은 유지.
//     loop()/setup() task에서만 호출 (교차 코어에서 힙 조작 금지).
void loadPageImages(uint8_t page) {
  if (!fsMounted) return;
  for (uint8_t b = 0; b < BUTTONS_PER_PAGE; b++) {
    if (imgJpeg[page][b]) continue;   // 이미 캐시됨
    char path[32];
    snprintf(path, sizeof(path), "/btns/p%u_%u.jpg", page, b);
    File f = LittleFS.open(path, "r");
    if (!f) continue;
    size_t sz = f.size();
    if (sz == 0 || sz > IMG_MAX_BYTES) { f.close(); continue; }
    uint8_t* buf = (uint8_t*)malloc(sz);
    if (!buf) { f.close(); continue; }
    if (f.read(buf, sz) != sz) { free(buf); f.close(); continue; }
    f.close();
    imgJpeg[page][b] = buf;
    imgSize[page][b] = (uint16_t)sz;
    imgHeapUsed += (uint32_t)sz;
  }
}

// [H] 페이지의 이미지 RAM 슬롯 해제 (페이지 이탈 시). 플래시 파일은 유지 — 다시 로드 가능.
void freePageImages(uint8_t page) {
  for (uint8_t b = 0; b < BUTTONS_PER_PAGE; b++) {
    uint8_t* cur = imgJpeg[page][b];
    if (cur) {
      uint16_t sz = imgSize[page][b];
      imgJpeg[page][b] = nullptr;
      imgSize[page][b] = 0;
      imgHeapUsed -= sz;
      free(cur);
    }
  }
}

// 현재 저장된 이미지 수 (nullptr 아닌 슬롯) — 예약 (필요 시 재사용)
uint16_t countImages() {
  uint16_t n = 0;
  for (uint8_t p = 0; p < MAX_PAGES; p++)
    for (uint8_t b = 0; b < BUTTONS_PER_PAGE; b++)
      if (imgJpeg[p][b]) n++;
  return n;
}// [H] 전체 설정(RAM) → LittleFS /config.bin (A.4 포맷). 내용이 실제로 바뀌었을 때만
//     loop()가 호출(디바운스) — 웨어 방지. 최악 ~15KB를 힙 임시 버퍼에 조립 후 1회 쓰기.
void saveConfigToFlash() {
  if (!fsMounted) return;
  uint8_t np = numPages;
  if (np < 1) np = 1;
  if (np > MAX_PAGES) np = MAX_PAGES;

  size_t cap = 6;
  for (uint8_t p = 0; p < np; p++) {
    cap += 1 + (size_t)strlen(pageNames[p]);
    for (uint8_t b = 0; b < BUTTONS_PER_PAGE; b++)
      cap += 1 + (size_t)strlen(labels[p][b]) + 3 + (size_t)strlen(btnActionVal[p][b]);
  }
  uint8_t* buf = (uint8_t*)malloc(cap);
  if (!buf) { Serial.println("[FS] config.bin 저장 실패 (메모리)"); return; }

  size_t off = 0;
  buf[off++] = (uint8_t)(CFG_MAGIC >> 24); buf[off++] = (uint8_t)(CFG_MAGIC >> 16);
  buf[off++] = (uint8_t)(CFG_MAGIC >> 8);  buf[off++] = (uint8_t)CFG_MAGIC;
  buf[off++] = CFG_VERSION;
  buf[off++] = np;
  for (uint8_t p = 0; p < np; p++) {
    size_t nlen = strlen(pageNames[p]);
    buf[off++] = (uint8_t)nlen;
    memcpy(buf + off, pageNames[p], nlen); off += nlen;
    for (uint8_t b = 0; b < BUTTONS_PER_PAGE; b++) {
      size_t llen = strlen(labels[p][b]);
      size_t alen = strlen(btnActionVal[p][b]);
      buf[off++] = (uint8_t)llen;
      memcpy(buf + off, labels[p][b], llen); off += llen;
      buf[off++] = btnColors[p][b];
      buf[off++] = btnActionType[p][b];
      buf[off++] = (uint8_t)alen;
      memcpy(buf + off, btnActionVal[p][b], alen); off += alen;
    }
  }

  File f = LittleFS.open(CONFIG_BIN_PATH, "w");
  if (f) {
    f.write(buf, off);
    f.close();
    Serial.printf("[FS] config.bin 저장: %u페이지 (%uB)\n", np, (unsigned)off);
  } else {
    Serial.println("[FS] config.bin 쓰기 실패");
  }
  free(buf);
}

// [H] LittleFS /config.bin → RAM 복원. 성공 시 numPages 설정, true 반환.
//     손상/부재 시 false → 호출자가 NVS 기본값으로 폴백 (디바이스는 그래도 동작).
bool loadConfigFromFlash() {
  if (!fsMounted) return false;
  if (!LittleFS.exists(CONFIG_BIN_PATH)) return false;
  File f = LittleFS.open(CONFIG_BIN_PATH, "r");
  if (!f) return false;
  size_t sz = f.size();
  if (sz < 6) { f.close(); return false; }
  uint8_t* buf = (uint8_t*)malloc(sz);
  if (!buf) { f.close(); return false; }
  if (f.read(buf, sz) != sz) { free(buf); f.close(); return false; }
  f.close();

  size_t off = 0;
  uint32_t magic = ((uint32_t)buf[0] << 24) | ((uint32_t)buf[1] << 16) |
                   ((uint32_t)buf[2] << 8) | (uint32_t)buf[3];
  uint8_t ver = buf[4];
  uint8_t np = buf[5];
  off = 6;
  if (magic != CFG_MAGIC || ver != CFG_VERSION) { free(buf); return false; }
  if (np < 1) np = 1;
  if (np > MAX_PAGES) np = MAX_PAGES;

  bool ok = true;
  for (uint8_t p = 0; p < np && ok; p++) {
    if (off + 1 > sz) { ok = false; break; }
    uint8_t nlen = buf[off++];
    if (nlen > PAGE_NAME_MAX) nlen = PAGE_NAME_MAX;
    if (off + nlen > sz) { ok = false; break; }
    if (nlen) { memcpy(pageNames[p], buf + off, nlen); pageNames[p][nlen] = '\0'; }
    else      { pageNames[p][0] = '\0'; }
    off += nlen;
    for (uint8_t b = 0; b < BUTTONS_PER_PAGE && ok; b++) {
      if (off + 1 > sz) { ok = false; break; }
      uint8_t llen = buf[off++];
      if (llen > LABEL_MAX) llen = LABEL_MAX;
      if (off + llen > sz) { ok = false; break; }
      if (llen) { memcpy(labels[p][b], buf + off, llen); labels[p][b][llen] = '\0'; }
      else      { labels[p][b][0] = '\0'; }
      off += llen;
      if (off + 3 > sz) { ok = false; break; }
      btnColors[p][b] = buf[off++];
      if (btnColors[p][b] >= BTN_COLOR_COUNT) btnColors[p][b] = 0;
      btnActionType[p][b] = buf[off++];
      if (btnActionType[p][b] > 2) btnActionType[p][b] = 0;
      uint8_t alen = buf[off++];
      if (alen > ACTION_VAL_MAX) alen = ACTION_VAL_MAX;
      if (off + alen > sz) { ok = false; break; }
      if (alen) { memcpy(btnActionVal[p][b], buf + off, alen); btnActionVal[p][b][alen] = '\0'; }
      else      { btnActionVal[p][b][0] = '\0'; }
      off += alen;
    }
  }
  free(buf);
  if (!ok) { Serial.println("[FS] config.bin 손상 — 기본값 사용"); return false; }

  numPages = np;
  if (currentPage >= numPages) currentPage = numPages - 1;   // 호출자가 NVS lastPage로 보정
  Serial.printf("[FS] config.bin 로드: %u페이지\n", np);
  return true;
}

// [H] MREQ 응답: 전체 설정을 v3 MCFG 포맷으로 페이지별 청크 전송 (페이지 0..numPages-1).
//     실행은 loop()에서 (콜백에서 UDP/LittleFS 금지). page_name은 페이지 첫 청크에만,
//     이후 청크는 name_len=0(변경 없음). num_pages는 모든 청크에 반복.
void sendConfigDump(IPAddress ip, uint16_t port) {
  if (port == 0) return;
  uint8_t pkt[1400];   // 단일 UDP 데이터그램
  for (uint8_t p = 0; p < numPages; p++) {
    size_t nameLen = strlen(pageNames[p]);
    if (nameLen > PAGE_NAME_MAX) nameLen = PAGE_NAME_MAX;
    size_t off = 0;
    pkt[off++] = 0x4D; pkt[off++] = 0x43; pkt[off++] = 0x46; pkt[off++] = 0x47;  // "MCFG"
    pkt[off++] = p;                    // page
    uint8_t countPos = off; off++;     // count — 전송 전에 채움
    pkt[off++] = numPages;             // num_pages
    pkt[off++] = 0;                    // rsvd
    pkt[off++] = (uint8_t)nameLen;     // page_name_len
    memcpy(pkt + off, pageNames[p], nameLen); off += nameLen;
    uint8_t count = 0;
    for (uint8_t b = 0; b < BUTTONS_PER_PAGE; b++) {
      size_t llen = strlen(labels[p][b]);
      size_t alen = strlen(btnActionVal[p][b]);
      size_t itemBytes = 5 + llen + alen;
      // 새 항목이 청크 상한을 넘으면 지금까지 것을 전송하고 새 청크 시작
      if (count > 0 && off + itemBytes > MCFG_CHUNK_MAX) {
        pkt[countPos] = count;
        udpSend.beginPacket(ip, port);
        udpSend.write(pkt, off);
        udpSend.endPacket();
        off = 0;                                   // 새 청크: 헤더 + name_len=0 (이름 생략)
        pkt[off++] = 0x4D; pkt[off++] = 0x43; pkt[off++] = 0x46; pkt[off++] = 0x47;
        pkt[off++] = p;
        countPos = off; off++;
        pkt[off++] = numPages;
        pkt[off++] = 0;
        pkt[off++] = 0;                            // page_name_len = 0
        count = 0;
      }
      if (off + itemBytes > sizeof(pkt)) break;    // 방어 — 단일 항목이 버퍼 초과 시 생략
      pkt[off++] = b;
      pkt[off++] = (uint8_t)llen;
      pkt[off++] = btnColors[p][b];
      pkt[off++] = btnActionType[p][b];
      pkt[off++] = (uint8_t)alen;
      memcpy(pkt + off, labels[p][b], llen); off += llen;
      memcpy(pkt + off, btnActionVal[p][b], alen); off += alen;
      count++;
    }
    pkt[countPos] = count;
    udpSend.beginPacket(ip, port);
    udpSend.write(pkt, off);
    udpSend.endPacket();
  }
}

// [H] MREQ 응답: /btns/p{page}_{bid}.jpg 이미지 덤프 (존재하는 이미지 버튼만 MIMG fmt=0).
//     파일 ≤IMG_MAX_BYTES라 단일 데이터그램 — WiFiUDP는 beginPacket~endPacket을 하나로 버퍼링.
//     패킷 사이에 delay()를 두어 WiFi/AP TX 큐가 비우도록 한다 — ESP32는 연속 대형 UDP 버스트에서
//     패킷 드랍이 발생할 수 있고, config(소형)는 통과해도 이미지(최대 1400B)가 유실되는 사례가 있다.
void sendImageDump(IPAddress ip, uint16_t port) {
  if (!fsMounted || port == 0) return;
  for (uint8_t p = 0; p < numPages; p++) {
    for (uint8_t b = 0; b < BUTTONS_PER_PAGE; b++) {
      char path[32];
      snprintf(path, sizeof(path), "/btns/p%u_%u.jpg", p, b);
      if (!LittleFS.exists(path)) continue;
      File f = LittleFS.open(path, "r");
      if (!f) continue;
      size_t sz = f.size();
      if (sz == 0 || sz > IMG_MAX_BYTES) { f.close(); continue; }
      uint8_t hdr[8] = {0x4D, 0x49, 0x4D, 0x47, p, b, 0, 0};   // "MIMG" page bid fmt=0 rsvd=0
      udpSend.beginPacket(ip, port);
      udpSend.write(hdr, 8);
      uint8_t chunk[256];
      while (sz > 0) {
        size_t rd = f.read(chunk, min((size_t)sizeof(chunk), sz));
        if (rd == 0) break;
        udpSend.write(chunk, rd);
        sz -= rd;
      }
      udpSend.endPacket();
      f.close();
      delay(15);   // [H] 버스트 유실 방지 — 최대 96개×15ms = 1.44s (호스트 5s 데드라인 안)
    }
  }
}

// ==========================================
// 10. 이벤트 전송 (디바이스→호스트)
// ==========================================
void sendEvent(uint8_t page, uint8_t buttonId) {
  if (!hostKnown || hostPort == 0 || WiFi.status() != WL_CONNECTED) return;

  uint8_t pkt[8];
  pkt[0] = 0x4D; pkt[1] = 0x50; pkt[2] = 0x41; pkt[3] = 0x44;  // "MPAD"
  pkt[4] = page; pkt[5] = buttonId; pkt[6] = 0; pkt[7] = 0;    // flags, rsvd

  udpSend.beginPacket(hostIP, hostPort);
  udpSend.write(pkt, 8);
  udpSend.endPacket();

  statEvents++;
  String hip = hostIP.toString();
  Serial.printf("[EVT] page=%u btn=%u -> %s:%u\n", page, buttonId, hip.c_str(), hostPort);
}

// ==========================================
// 10.5 디스커버리 비콘 (디바이스→서브넷 브로드캐스트)
// ==========================================
// 호스트가 IP를 모를 때 자동 검색할 수 있도록, Wi-Fi 연결 중엔 3초마다
// 서브넷 브로드캐스트 주소로 "MPBE" 비콘을 쏜다. 호스트 리스너는 이 비콘의
// 소스 IP를 보고 설정(라벨)을 다시 푸시해 데드록(서로 IP를 모름)을 푼다.
void sendBeacon() {
  if (WiFi.status() != WL_CONNECTED) return;

  // 서브넷 브로드캐스트 주소 = localIP | ~subnetMask (예: 192.168.19.255)
  IPAddress brd;
  brd[0] = WiFi.localIP()[0] | (~WiFi.subnetMask()[0] & 0xFF);
  brd[1] = WiFi.localIP()[1] | (~WiFi.subnetMask()[1] & 0xFF);
  brd[2] = WiFi.localIP()[2] | (~WiFi.subnetMask()[2] & 0xFF);
  brd[3] = WiFi.localIP()[3] | (~WiFi.subnetMask()[3] & 0xFF);

  uint8_t pkt[8];
  pkt[0] = 0x4D; pkt[1] = 0x50; pkt[2] = 0x42; pkt[3] = 0x45;  // "MPBE"
  pkt[4] = 0; pkt[5] = 0; pkt[6] = 0; pkt[7] = 0;

  udpSend.beginPacket(brd, UDP_PORT);
  udpSend.write(pkt, 8);
  udpSend.endPacket();
  statBeacons++;
}

// ==========================================
// 11. UI 렌더링
// ==========================================
inline int btn_x(int col) { return GRID_LEFT + col * (BTN_W + BTN_GAP_X); }
inline int btn_y(int row) { return GRID_TOP + row * (BTN_H + BTN_GAP_Y); }

bool hitButton(uint16_t tx, uint16_t ty, int* col, int* row) {
  if (ty < GRID_TOP || ty >= GRID_BOTTOM || tx < GRID_LEFT || tx >= GRID_RIGHT) return false;
  *col = (tx - GRID_LEFT) / (BTN_W + BTN_GAP_X);
  *row = (ty - GRID_TOP) / (BTN_H + BTN_GAP_Y);
  if (*col < 0 || *col >= GRID_COLS || *row < 0 || *row >= GRID_ROWS) return false;
  // gap 영역 배제: 실제 버튼 사각형 안쪽인지 확인
  int x = btn_x(*col), y = btn_y(*row);
  if (tx < x || tx >= x + BTN_W || ty < y || ty >= y + BTN_H) return false;
  return true;
}

void drawButton(uint8_t page, uint8_t idx, bool pressed) {
  int col = idx % GRID_COLS;
  int row = idx / GRID_COLS;
  int x = btn_x(col), y = btn_y(row);

  // [G] 이미지가 있으면 JPEG 렌더, 없으면 기존 텍스트 렌더 (폴백)
  if (imgJpeg[page][idx] && imgSize[page][idx] > 0) {
    drawButtonImage(page, idx, x, y, pressed);
    return;
  }
  drawButtonText(page, idx, x, y, pressed);
}

// [G] 텍스트 렌더 (기존 방식) — 이미지가 없거나 디코드 실패 시 폴백
void drawButtonText(uint8_t page, uint8_t idx, int x, int y, bool pressed) {
  // 팔레트 기반 색상 (btnColors[page][idx]는 onConfigPacket이 채움)
  uint8_t ci = btnColors[page][idx];
  if (ci >= BTN_COLOR_COUNT) ci = 0;
  const BtnColor& c = BTN_PALETTE[ci];
  uint16_t fill;
  if (pressed) {
    // 눌림: 각 채널 +50 (255 클램프) → 밝게 하이라이트
    fill = lcd.color565(min(255, c.r + 50), min(255, c.g + 50), min(255, c.b + 50));
  } else {
    fill = lcd.color565(c.r, c.g, c.b);
  }
  uint16_t border = pressed ? TFT_WHITE : lcd.color565(100, 116, 139);
  lcd.fillRoundRect(x, y, BTN_W, BTN_H, BTN_RADIUS, fill);
  lcd.drawRoundRect(x, y, BTN_W, BTN_H, BTN_RADIUS, border);

  lcd.setTextColor(c.textWhite ? TFT_WHITE : TFT_BLACK);
  lcd.setTextSize(1);
  lcd.setTextDatum(MC_DATUM);
  lcd.drawString(labels[page][idx], x + BTN_W / 2, y + BTN_H / 2);
}

// [G] 버튼 눌림 상태를 JPEG 디코드 콜백이 참조. 디코드는 loop()에서 동기식이므로
//     전역 플래그로 충분하다 (AsyncUDP 콜백은 이미지 버퍼를 건드리지 않음).
static bool btnDrawPressed = false;

int jpegBtnCallback(JPEGDRAW* pDraw) {
  // 눌림: 픽셀을 ×0.75 어둡게 오버레이.
  // 주의: setPixelType(RGB565_BIG_ENDIAN)이므로 pPixels는 고바이트 우선이다.
  // little-endian ESP32에서 uint16_t*로 읽으면 바이트가 뒤집혀 채널이 깨진다
  // → 바이트 단위로 조립/분해해 같은 순서(BE)로 다시 기록한다.
  if (btnDrawPressed) {
    uint8_t* b = (uint8_t*)pDraw->pPixels;
    int n = pDraw->iWidth * pDraw->iHeight;
    for (int i = 0; i < n; i++) {
      uint16_t v = (uint16_t)((b[i * 2] << 8) | b[i * 2 + 1]);  // BE → 논리 RGB565
      uint16_t r = (v >> 11) & 0x1F, g = (v >> 5) & 0x3F, bl = v & 0x1F;
      uint16_t d = (uint16_t)(((r * 3 / 4) << 11) | ((g * 3 / 4) << 5) | (bl * 3 / 4));
      b[i * 2] = (uint8_t)(d >> 8);                             // BE로 다시 기록
      b[i * 2 + 1] = (uint8_t)(d & 0xFF);
    }
  }
  lcd.pushImage(pDraw->x, pDraw->y, pDraw->iWidth, pDraw->iHeight, pDraw->pPixels);
  return 1;
}

// [PLAN 7] 이미지 버튼의 코너를 그리드 배경색으로 잘라 둥글게 만든다.
//     JPEG 4:2:0 손실 압축은 호스트가 구운 라운드 코너를 3~5px 번지게 해 각진 버튼으로
//     보이게 하므로, 호스트 쪽 라운딩만으로는 부족하다. 여기서 디바이스가 원
//     (반경 BTN_RADIUS, 중심 = 코너 + BTN_RADIUS) 바깥의 모서리 비트를 행 단위 fillRect로
//     정확히 덮어 크리스프한 라운드를 만든다. 호스트 rounded_rectangle(BTN_RADIUS)과
//     동일한 기하이므로 두 쪽이 겹쳐도 어긋나지 않는다.
void roundButtonCorners(int x, int y) {
  const uint16_t bg = lcd.color565(15, 23, 42);  // 그리드 배경색 (호스트 GRID_BG_HEX와 일치)
  const int R = BTN_RADIUS;
  // 코너 정사각형(각 변 R)에서 디스크 밖의 비트 부분. 행 v(모서리→중심, 0-based)마다
  // 디스크 반폭 half = sqrt(R²-(R-v)²)이므로 그 행의 비트 폭 w = R - half. 좌/우/상/하 대칭.
  for (int v = 0; v < R; v++) {
    int half = (int)sqrtf((float)(R * R - (R - v) * (R - v)));
    int w = R - half;
    if (w <= 0) continue;
    int rCol = x + BTN_W - w;           // 우측 대칭 열 (오른쪽에서 w열)
    int bRow = y + BTN_H - 1 - v;       // 하단 대칭 행 (아래에서 v번째)
    lcd.fillRect(x,    y + v, w, 1, bg);  // TL
    lcd.fillRect(rCol, y + v, w, 1, bg);  // TR
    lcd.fillRect(x,    bRow,  w, 1, bg);  // BL
    lcd.fillRect(rCol, bRow,  w, 1, bg);  // BR
  }
}

// [G] 버튼 JPEG를 디코드해 버튼 위치에 그린다. 디코드 실패 시 텍스트 폴백.
//     decode(x, y, 0)로 버튼 원점을 지정 → 콜백의 pDraw->x/y는 절대 화면 좌표.
void drawButtonImage(uint8_t page, uint8_t idx, int x, int y, bool pressed) {
  uint8_t* jpg = imgJpeg[page][idx];
  uint16_t sz = imgSize[page][idx];
  if (!jpg || sz == 0) {
    drawButtonText(page, idx, x, y, pressed);
    return;
  }

  btnDrawPressed = pressed;
  int rc = jpeg.openRAM(jpg, (int)sz, jpegBtnCallback);
  if (rc <= 0) {   // openRAM 실패(잘린 JPEG 등) → 텍스트 폴백
    Serial.printf("[IMG] 디코드 실패 p=%u b=%u rc=%d\n", page, idx, rc);
    drawButtonText(page, idx, x, y, pressed);
    return;
  }
  jpeg.setPixelType(RGB565_BIG_ENDIAN);
  // [FIX] 버튼 71×61은 8/16의 배수가 아니다. JPEGDEC는 MCU(4:2:0 = 16×16) 단위로
  // 패딩을 채워 콜백의 iWidth를 80(5 MCU×16 = 실제 71 + 패딩 9)으로 넘긴다.
  // 이대로 pushImage하면 우측 9px의 패딩 쓰레기가 갭·다음 버튼을 덮는다 → 버튼
  // 셀로 클립해 잘라낸다. (pushImage가 src_bitwidth=iWidth로 행 간격을 맞춰 읽으므로
  // iWidthUsed로 축소하면 행이 틀어지지만, 클립은 안전하다.)
  lcd.setClipRect(x, y, BTN_W, BTN_H);
  jpeg.decode(x, y, 0);
  lcd.clearClipRect();
  jpeg.close();

  // [PLAN 7] JPEG가 번진 코너를 배경색 비트로 덮어 크리스프한 라운드 복원.
  //     눌림 어둡게 처리(jpegBtnCallback의 픽셀 곱셈) 후에 그려야 코너가 항상 배경색.
  roundButtonCorners(x, y);

  if (pressed) {
    // 눌림: 밝은 테두리 2px 오버레이 (텍스트 버튼 pressed 시각과 일치)
    lcd.drawRoundRect(x + 1, y + 1, BTN_W - 2, BTN_H - 2, BTN_RADIUS, TFT_WHITE);
    lcd.drawRoundRect(x + 2, y + 2, BTN_W - 4, BTN_H - 4, BTN_RADIUS, TFT_WHITE);
  }
}

// [B] 버튼을 임시 색(피드백용)으로 렌더 — 원상 복귀는 drawButton(page, idx, false)
void drawButtonFlash(uint8_t page, uint8_t idx, uint16_t fill) {
  int col = idx % GRID_COLS;
  int row = idx / GRID_COLS;
  int x = btn_x(col), y = btn_y(row);

  // [G] 이미지 버튼: 이미지는 유지하고 피드백 색 테두리를 3px 오버레이 (성공/실패 신호)
  if (imgJpeg[page][idx] && imgSize[page][idx] > 0) {
    drawButtonImage(page, idx, x, y, false);
    lcd.drawRoundRect(x + 1, y + 1, BTN_W - 2, BTN_H - 2, BTN_RADIUS, fill);
    lcd.drawRoundRect(x + 2, y + 2, BTN_W - 4, BTN_H - 4, BTN_RADIUS, fill);
    lcd.drawRoundRect(x + 3, y + 3, BTN_W - 6, BTN_H - 6, BTN_RADIUS, fill);
    return;
  }

  lcd.fillRoundRect(x, y, BTN_W, BTN_H, BTN_RADIUS, fill);
  lcd.drawRoundRect(x, y, BTN_W, BTN_H, BTN_RADIUS, TFT_WHITE);
  lcd.setTextColor(TFT_WHITE);
  lcd.setTextSize(1);
  lcd.setTextDatum(MC_DATUM);
  lcd.drawString(labels[page][idx], x + BTN_W / 2, y + BTN_H / 2);
}

// [C] 백라이트 밝기 갱신: 무터치 시 디밍, 그 외엔 주변 조도(CDS)에 맞춰 자동 조절.
// LDR 극성은 보드마다 다를 수 있다 — CYD 기본 회로는 밝을수록 ADC 값이 낮아져
// (3.3V→CDS→핀→GND), 아래 map()은 "밝은 곳에서 화면도 밝게"다. 반대라면
// map(in, inMin, inMax)의 인자 순서(200/3600)를 뒤집는다.
void updateBrightness() {
  unsigned long now = millis();

  // 무터치 IDLE_DIM_MS 경과 → 디밍 (off 아님). 터치는 handleTouch가 lastTouchTime을 갱신.
  if (now - lastTouchTime >= IDLE_DIM_MS) {
    if (lastBrightness != BRIGHT_DIM) {
      lcd.setBrightness(BRIGHT_DIM);
      lastBrightness = BRIGHT_DIM;
    }
    return;
  }

  // 조도 기반 자동 밝기 (ADC 읽기는 1초 throttle — 매 loop마다 읽지 않음)
  if (now - lastLdrTime < 1000) return;
  lastLdrTime = now;

  int ldr = analogRead(LDR_PIN);                       // 0~4095, 밝을수록 낮음(CYD)
  int b = map(ldr, 200, 3800, BRIGHT_FULL, BRIGHT_MIN);
  b = constrain(b, BRIGHT_MIN, BRIGHT_FULL);
  if ((uint8_t)b != lastBrightness) {
    lcd.setBrightness((uint8_t)b);
    lastBrightness = (uint8_t)b;
  }
}

void drawStatusBar() {
  // 상태바 배경 (그리드 배경색과 동일하게 채워 이음새 제거)
  lcd.fillRect(0, STATUS_TOP, 320, 240 - STATUS_TOP, lcd.color565(15, 23, 42));

  // 이전/다음 페이지
  lcd.fillRoundRect(PREV_X, PREV_Y, PREV_W, PREV_H, 4,
                    currentPage > 0 ? lcd.color565(37, 99, 235) : lcd.color565(71, 85, 105));
  lcd.setTextColor(TFT_WHITE);
  lcd.setTextDatum(MC_DATUM);
  lcd.setTextSize(1);
  lcd.drawString("<", PREV_X + PREV_W / 2, PREV_Y + PREV_H / 2);

  lcd.fillRoundRect(NEXT_X, NEXT_Y, NEXT_W, NEXT_H, 4,
                    currentPage < numPages - 1 ? lcd.color565(37, 99, 235) : lcd.color565(71, 85, 105));
  lcd.drawString(">", NEXT_X + NEXT_W / 2, NEXT_Y + NEXT_H / 2);

  // 중앙: 페이지 이름 + x/y만 (IP 미표시 — [PLAN 6] 전환 버튼 확대로 중앙 폭이 좁아짐)
  char buf[40];
  const char* pname = pageNames[currentPage];
  if (pname[0] != '\0') {
    char nameBuf[9];                         // 중앙 폭 대비 8자 + null 여유
    strncpy(nameBuf, pname, 8);
    nameBuf[8] = '\0';
    snprintf(buf, sizeof(buf), "%s  %d/%d", nameBuf, currentPage + 1, numPages);
  } else {
    snprintf(buf, sizeof(buf), "PAGE %d/%d", currentPage + 1, numPages);
  }
  lcd.setTextColor(lcd.color565(148, 163, 184));
  lcd.drawString(buf, 160, STATUS_TOP + 14);
}

void drawGrid(uint8_t page) {
  lcd.fillScreen(lcd.color565(15, 23, 42));
  for (uint8_t i = 0; i < BUTTONS_PER_PAGE; i++) {
    drawButton(page, i, false);
  }
  drawStatusBar();
}

void drawBootScreen(const char* msg) {
  lcd.fillScreen(lcd.color565(15, 23, 42));
  lcd.setTextColor(TFT_WHITE);
  lcd.setTextDatum(MC_DATUM);
  lcd.setTextSize(2);
  lcd.drawString("Macro Pad", 160, 90);
  lcd.setTextSize(1);
  lcd.setTextColor(lcd.color565(148, 163, 184));
  lcd.drawString(msg, 160, 120);
}

void drawErrorScreen(const char* msg) {
  lcd.fillScreen(lcd.color565(80, 20, 20));
  lcd.setTextColor(TFT_WHITE);
  lcd.setTextDatum(MC_DATUM);
  lcd.setTextSize(1);
  const char* nl = strchr(msg, '\n');
  if (nl) {
    char line[40];
    int l0 = min((int)(nl - msg), 39);
    memcpy(line, msg, l0);
    line[l0] = '\0';
    lcd.drawString(line, 160, 108);
    lcd.drawString(nl + 1, 160, 132);
  } else {
    lcd.drawString(msg, 160, 120);
  }
}

void drawReadyScreen() {
  lcd.fillScreen(lcd.color565(15, 23, 42));
  lcd.setTextColor(TFT_WHITE);
  lcd.setTextDatum(MC_DATUM);
  lcd.setTextSize(2);
  lcd.drawString("Macro Pad", 160, 80);
  lcd.setTextSize(1);
  lcd.setTextColor(lcd.color565(148, 163, 184));
  char buf[48];
  snprintf(buf, sizeof(buf), "UDP %d  IP: %s", UDP_PORT, WiFi.localIP().toString().c_str());
  lcd.drawString(buf, 160, 110);
  lcd.drawString("Waiting for host config...", 160, 132);
  delay(1200);
}

// ==========================================
// 12. 터치 상태머신 (탭/롱프레스 구분)
// ==========================================
static unsigned long touchStart = 0;
static uint16_t pressX = 0, pressY = 0;
static bool pressInGrid = false;

void handleTouch() {
  uint16_t tx, ty;
  if (lcd.getTouch(&tx, &ty)) {
    if (touchStart == 0) {
      // 새 누름 시작
      touchStart = millis();
      lastTouchTime = touchStart;   // [C] 터치 시각 갱신 (디밍 취소)
      // [C] 디밍 상태였다면 즉시 조도 기반 밝기로 복귀
      if (lastBrightness == BRIGHT_DIM) {
        int ldr = analogRead(LDR_PIN);
        int b = map(ldr, 200, 3800, BRIGHT_FULL, BRIGHT_MIN);
        b = constrain(b, BRIGHT_MIN, BRIGHT_FULL);
        lcd.setBrightness((uint8_t)b);
        lastBrightness = (uint8_t)b;
      }
      pressX = tx;
      pressY = ty;
      pressInGrid = (ty < STATUS_TOP);
      if (pressInGrid) {
        int col, row;
        if (hitButton(pressX, pressY, &col, &row)) {
          drawButton(currentPage, row * GRID_COLS + col, true);  // 눌림 시각 피드백
        }
      }
    } else if (!pressInGrid && (millis() - touchStart) > LONG_PRESS_MS) {
      // 상태바에서 시작한 길게 누르기 → Wi-Fi 재설정
      touchStart = 0;
      runTouchWifiSetup();
      ESP.restart();
    }
    delay(15);   // 소프트웨어 SPI(XPT2046) 폴링 스로틀
    return;
  }

  // 터치 떼어짐
  if (touchStart != 0) {
    unsigned long dur = millis() - touchStart;
    touchStart = 0;
    if (dur > MAX_TAP_MS) return;   // 길게 누른 탭 무시

    if (pressInGrid) {
      int col, row;
      if (hitButton(pressX, pressY, &col, &row)) {
        int idx = row * GRID_COLS + col;
        drawButton(currentPage, idx, false);  // 원상 복귀
        sendEvent(currentPage, idx);
      }
    } else {
      // 상태바: 페이지 이동 — [H] 이미지 캐시를 페이지 단위로 스왑 (플래시에서 즉시 로드, 오프라인)
      if (pressX >= PREV_X && pressX < PREV_X + PREV_W && currentPage > 0) {
        uint8_t oldPage = currentPage;
        currentPage--;
        freePageImages(oldPage);
        loadPageImages(currentPage);
        drawGrid(currentPage);
      } else if (pressX >= NEXT_X && pressX < NEXT_X + NEXT_W && currentPage < numPages - 1) {
        uint8_t oldPage = currentPage;
        currentPage++;
        freePageImages(oldPage);
        loadPageImages(currentPage);
        drawGrid(currentPage);
      } else {
        drawStatusBar();
      }
    }
  }
}

// ==========================================
// 13. 메인 루프
// ==========================================
// [E] 마지막 페이지 복원용 상태: 페이지/페이지 수 변경을 5초 디바운스 후 1회만 NVS 저장
static uint8_t lastShownPage = 0xFF;
static uint8_t lastSavedPages = 0xFF;
static unsigned long pageChangeTime = 0;
static bool pageChangePending = false;
// [G] 페이지 수 감소로 범위 밖이 된 이미지 해제용 추적 (마지막으로 정리한 페이지 수)
static uint8_t lastTrimmedPages = 0xFF;

void loop() {
  unsigned long now = millis();

  // [G] 이미지 패킷 적용 (onImagePacket이 pending 큐에 넣은 것) — 단일 task에서
  //     저장/해제/회계 수행. 렌더보다 먼저 처리해 decode 중 free 레이스를 없앤다.
  applyPendingImages();

  // [H] config.bin 저장 debounce — 다중 패킷 푸시(8페이지×청크)를 1회 쓰기로 합침 (웨어 방지)
  if (configSaveDirty && now - configDirtyTime >= CONFIG_SAVE_DEBOUNCE_MS) {
    configSaveDirty = false;
    saveConfigToFlash();
  }

  // [H] MREQ 덤프 처리 (AsyncUDP 콜백이 mux 아래 큐에 넣음 — 여기서 실제 전송)
  bool doDump = false;
  IPAddress dumpIP;
  uint16_t dumpPort = 0;
  portENTER_CRITICAL(&dumpReqMux);
  if (dumpReq.pending) {
    doDump = true;
    dumpIP = dumpReq.ip;
    dumpPort = dumpReq.port;
    dumpReq.pending = false;
  }
  portEXIT_CRITICAL(&dumpReqMux);
  if (doDump) {
    String s = dumpIP.toString();
    Serial.printf("[MREQ] dump -> %s:%u\n", s.c_str(), dumpPort);
    sendConfigDump(dumpIP, dumpPort);
    sendImageDump(dumpIP, dumpPort);
  }

  // [G] 페이지 수 감소로 범위 밖이 된 페이지의 이미지 해제 + [H] 플래시 파일 정리
  if (numPages < lastTrimmedPages) {
    // [FIX] 부팅 첫 루프: lastTrimmedPages 초기값 0xFF(센티널)로 p가 imgJpeg[MAX_PAGES] 배열
    //     밖(8..254)까지 나가면 OOB 읽기로 쓰레기 포인터를 free → heap_caps_free 어설션 →
    //     무한 리셋. 상한을 MAX_PAGES로 클램프해 배열 범위 안에서만 정리한다.
    uint8_t trimHi = lastTrimmedPages;
    if (trimHi > MAX_PAGES) trimHi = MAX_PAGES;
    for (uint8_t p = numPages; p < trimHi; p++) {
      freePageImages(p);                        // [H] RAM 해제 (잘린 페이지는 현재 페이지가 아님 — 방어적)
      if (fsMounted) {
        for (uint8_t b = 0; b < BUTTONS_PER_PAGE; b++) {
          char path[32];
          snprintf(path, sizeof(path), "/btns/p%u_%u.jpg", p, b);
          if (LittleFS.exists(path)) LittleFS.remove(path);   // 잘린 페이지 이미지 삭제
        }
      }
    }
  }
  lastTrimmedPages = numPages;

  // 페이지 수 감소로 현재 페이지가 범위 밖이면 보정 (안전망 — onConfigPacket도 클램프)
  if (currentPage >= numPages) currentPage = numPages - 1;

  // [C] 백라이트 디밍/조도 자동 밝기 (60s 무터치 시 디밍, 터치 즉시 복귀)
  updateBrightness();

  // [E] 페이지 전환/수 변경 감지 → 5초 안정되면 1회 저장 (연타는 1회로 합침, NVS 웨어 방지)
  if (lastShownPage != currentPage || lastSavedPages != numPages) {
    lastShownPage = currentPage;
    lastSavedPages = numPages;
    pageChangeTime = now;
    pageChangePending = true;
  } else if (pageChangePending && now - pageChangeTime >= 5000) {
    prefsPad.putUChar("lastPage", currentPage);
    prefsPad.putUChar("numPages", numPages);
    pageChangePending = false;
  }

  // 호스트 설정 도착 시 그리드 재렌더 (플리커 방지: 변경 시에만 + [H] 다중 패킷 푸시 debounce)
  if (labelsDirty && now - labelsDirtyTime >= LABELS_REDRAW_DELAY) {
    labelsDirty = false;
    drawGrid(currentPage);
  }

  // [G] 이미지 배치 도착 → debounce 후 1회 재렌더 (버스트를 1회로 합침, 플리커 방지)
  if (imagesDirty && now - lastImageTime >= IMG_REDRAW_DELAY) {
    imagesDirty = false;
    drawGrid(currentPage);
  }

  // [B] 피드백 렌더: 상태는 AsyncUDP 콜백이 세우고, 그리기는 loop()에서 (레이스 방지).
  //     색상은 반드시 RGB565(lcd.color565)로 변환해 전달 — 24비트 hex를 그대로 넘기면 잘림.
  if (feedbackPending) {
    feedbackPending = false;
    if (feedbackPage >= 0 && feedbackPage == currentPage) {
      drawButtonFlash(feedbackPage, feedbackBtn,
                      feedbackOk ? lcd.color565(34, 197, 94) : lcd.color565(239, 68, 68));
    }
  }

  // [B] 피드백 플래시 종료 → 원상 복귀 (현재 페이지일 때만)
  if (feedbackPage >= 0 && now >= feedbackUntil) {
    int8_t p = feedbackPage, b = feedbackBtn;
    feedbackPage = -1;
    if (p == currentPage) drawButton(p, b, false);
  }

  handleTouch();

  // 디스커버리 비콘: 호스트가 IP를 자동 검색하도록 3초 주기 브로드캐스트
  static unsigned long lastBeaconTime = 0;
  if (now - lastBeaconTime >= BEACON_INTERVAL_MS) {
    sendBeacon();
    lastBeaconTime = now;
  }

  if (WiFi.status() != WL_CONNECTED) {
    delay(100);
  } else {
    yield();
  }
}
