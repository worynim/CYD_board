/**
 * @file CYD_Wireless_Monitor.ino
 * @brief CYD(ESP32-2432S028) 무선 모니터 (커스텀 LGFX 클래스 + 80MHz SPI/DMA 안정화 버전)
 *
 * 1. 커스텀 LGFX 클래스로 패널(ILI9341/ST7789)을 명시 지정하고 80MHz SPI + 하드웨어 DMA 구동
 *    (LGFX_AUTODETECT 미사용 — 패널은 아래 CYD_PANEL_ST7789 매크로로 선택)
 * 2. 주변 Wi-Fi AP 터치 목록 선택 및 비밀번호 가상 키보드 입력
 * 3. 원격 가로/세로 화면 회전 제어 및 FPS 오버레이 제어
 * 4. AsyncUDP 초저지연 프레임 수신 (1-패리티 FEC 복원 지원)
 * 5. JPEGDEC(Larry Bank) 고속 디코더로 렌더 병목 제거
 */

#include <WiFi.h>
#include <AsyncUDP.h>
#include <Preferences.h>
#include <esp_wifi.h>
#include <LovyanGFX.hpp>
#include <JPEGDEC.h>   // 고속 JPEG 디코더 (Larry Bank). Arduino 라이브러리 매니저에서 "JPEGDEC" 설치 필요.

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
static JPEGDEC jpeg;   // 고속 JPEG 디코더 인스턴스 (Larry Bank JPEGDEC 라이브러리)

// ---- [TEARSYNC] 밴드(블록) 체이스 저장소 ----
// JPEGDEC는 MCU 블록 단위로 LCD에 직접 푸시 → 한 프레임이 블록으로 조립돼 보이고,
// 패널 스캔과 경합하면 이분선(티어링)이 생긴다. 완성 프레임을 RAM에 저장하는 체이스는
// 이 보드에서 불가능이 실측 확정됐다(풀프레임 150KB: 힙 최대연속 107KB 세그먼트 벽 +
// Wi-Fi 접속 후 총량 149<154KB, 2026-08-24 — 슬롯 풀 320×480B도 총량에서 실패).
// → **밴드 체이스**: 디코더가 주는 MCU 블록(16×16, 512B)을 소규모 풀(~72KB)에 쌓고,
//   래스터 헤드(getScanLine)가 그 블록의 축 좌표를 **지나간 뒤에만** LCD로 방출한다.
//   가로 회전은 래스터가 논리 x축을 훑으므로 열 끝(x+16) 기준, 세로는 행 끝(y+16) 기준.
//   [2026-08-24 실측 실패] 풀 활성·lag=0으로 기구는 설계대로 동작했지만 MAP_FLIP 0/1
//   양쪽 모두 티어링 심함 → GSCAN 위상/원점이 논리 좌표와 불일치(또는 비동기).
//   RENDER_TEAR_SYNC 0으로 롤백 — 코드는 재검증용 보존, 상세는 위 스위치 주석.
static bool chaseReady = false;        // 블록 풀 확보 완료 (밴드 체이스 경로 사용 가능)
static bool chaseAllocTried = false;   // 최초 1회만 할당 시도 → 재시도/스팸 방지
AsyncUDP udp;
Preferences prefs;

const uint16_t UDP_PORT = 8888;

// JPEG 버퍼 크기. 풀해상도 320x240(런타임 할당된 RX/render 2중 버퍼) 기준.
// 원래 35000이었으나 JPEGDEC 라이브러리 정적 버퍼로 DRAM BSS 오버플로 발생 → 24576로 축소.
// 품질85의 노이즈 많은 장면은 초과 시 프레임 버려질 수 있음(기본값 품질45엔 무관).
#define MAX_JPEG_SIZE 24576
#define PACKET_PAYLOAD_SIZE 1400

// [INTERLEAVE] 디코드 중 MCU 줄 단위 즉시 푸시 — **실측 실패로 기본 꺼짐(2026-08-24)**.
// 가설: JPEGDEC 콜백에서 완성 줄을 곧바로 pushImage하면 SPI DMA가 다음 줄 IDCT와 겹쳐
// rt 54.5→~40ms. 결과: **rt 불변(품질 따라 50~65ms), 티어링은 오히려 악화**.
// 원인: LovyanGFX pushImage는 startWrite/endWrite 트랜잭션 안에서도 매 호출 DMA 완료를
// busy-wait → CPU·SPI 오버랩 불가. 공개 API로는 이 구조의 겹침을 만들 수 없음.
// 진짜 비동기가 필요하면 LGFX 쓰기를 우회한 spi_device_queue_trans 이중버퍼 직접 구동이
// 필요하지만 버스 공유 위험 대비 이득이 낮아 보류. 재제안 전에 이 실측 기억할 것.
#define RENDER_INTERLEAVE 0

// [TEARSYNC] 래스터 헤드 동기 블록 체이스 — **실측 실패로 기본 꺼짐 (2026-08-24)**.
// 구상: ST7789엔 scan-out 버퍼가 없어 GRAM을 컨트롤러가 상시 스캔한다. JPEGDEC 콜백의 MCU
// 블록(16×16)을 풀(~72KB)에 쌓고 getScanLine(0x45) 헤드가 그 축 좌표를 지나간 뒤에만
// pushImage하면 읽기-쓰기 경합(티어링)이 사라질 것이라 기대했다.
// 결과 (실기, 블록 풀 144슬롯 정상 활성 + lag=0 — 게이트 기구 자체는 설계대로 동작):
//   - TEAR_SYNC_MAP_FLIP 0/1 **양쪽 모두** 티어링 심함 (직접 모드 대비 개선 없음).
//   - 캘리브레이션([SCAN] 범위 0..257, 랩 ~20ms)은 항상 통과 → 주기는 맞지만 원점/위상이
//     논리 좌표와 어긋나 있거나(오프셋 미상), GSCAN이 가시 스캔과 근본적으로 비동기일 수 있다.
//   - 부수 실측: push 17.3→19.7ms (풀 복사+재푸시 오버헤드), rt·rend_fps 열화 없음.
// 결론: 공개 API(getScanLine)로 화면 스캔 위상을 맞추는 길은 이 보드+LGFX 조합에서 닫힘.
// 재제안 금지 — GSCAN 원점/위상을 확정하는 별도 진단 증거가 먼저다.
#define RENDER_TEAR_SYNC 0
#define TEAR_SYNC_GAP 24       // 헤드보다 뒤로 유지하는 안전 간격(표시축 단위)
#define TEAR_SYNC_MAP_FLIP 0   // GSCAN→논리축 방향 반전 (양방향 실패 실측 — 유효 보정값 없음)

// JPEG 수신/렌더 버퍼 — 힙 할당(ensureJpegBuffers). 정적(.bss) 대신 힙에 두는 이유:
// .bss 48KB를 비워 힙 여유 총량을 늘린다 (2026-08-24). 최대 연속 블록엔 영향이 없었지만
// (세그먼트 벽 107KB 불변 실측) BSS 헤드룸 확보로 그대로 유지한다.
static uint8_t* rxBuffer = nullptr;
static uint8_t* renderBuffer = nullptr;
// renderBufferSize는 Core0(콜백)에서 쓰고 Core1(loop 렌더)에서 읽는 크로스코어 값 → volatile.
// (hasNewFrame=true 직후에 쓰이므로 실질적으로는 순서상 안전하지만, 컴파일러 재정렬 방지 차원에서 명시)
static volatile size_t renderBufferSize = 0;
volatile bool isRendering = false;
volatile bool hasNewFrame = false;
// [FIX#R2] 렌더 중 완성된 프레임을 버리지 않고 "대기"시켜, 렌더 종료 직후 곧바로 스테이징한다.
// rxBuffer에는 완성된 프레임이 남아 있고, loop()가 이를 renderBuffer로 옮겨 바로 렌더한다.
// 이러면 디바이스가 자기 속도(≈16.7fps)로 항상 최신 프레임을 그려 "수초 지연"이 사라진다.
volatile bool pendingCommit = false;

// ---- [TEARSYNC] 스캔 동기화 상태 ----
#if RENDER_TEAR_SYNC
static bool tearSyncOk = false;        // 부팅 캘리브레이션 통과 (getScanLine 신뢰 가능)
static uint32_t statChaseLag = 0;      // 프레임 끝 강제 플러시로 밀린 잔여 블록 수 ([STAT] lag=)
#endif

static uint16_t expectedChunks = 0;
static uint16_t receivedChunks = 0;
static uint32_t latestFrameId = 0;
static uint8_t currentRotation = 3; // 기본 가로 정방향 (320x240). 코드 1이 가로 180° 반전 (프로토콜 표 참조)
static bool showFpsOverlay = false;

// ---- 재시작 감지용 타임아웃 ----
// 패킷이 이 시간(ms) 이상 오지 않으면 세션이 끊겼다고 판단
#define STREAM_TIMEOUT_MS 3000
unsigned long lastPacketTime = 0;
bool streamActive = false;

// ---- 청크 수신 추적: bitmask (최대 32청크까지 지원) ----
// 카운트 방식은 중복 수신 시 오동작 → 비트마스크로 교체
#define MAX_CHUNKS_BITMASK 32
static uint32_t chunkReceivedMask = 0;
static uint32_t chunkExpectedMask = 0;

// ---- FEC(1-패리티) ----
static bool fecEnabled = false;                   // 제어 패킷으로 정해지는 프로토콜 모드 (호스트가 통보)
static uint8_t parityBuffer[PACKET_PAYLOAD_SIZE]; // 호스트가 마지막 청크로 보내는 XOR 패리티
static bool fecHasParity = false;                 // 패리티 수신 여부
static bool frameCommitted = false;               // 현재 프레임이 완성되어 커밋됐는지
static uint16_t dataChunks = 0;                   // 데이터 청크 수 (FEC: expected-1, 레거시: expected)
static size_t lastPayloadLen = 0;                 // 레거시 모드: 마지막 데이터 청크의 실제 길이

// ---- 계측 (초당 1회 [STAT] 시리얼 출력) ----
static uint32_t statRecvFrames = 0;   // 수신 완성(커밋) 프레임
static uint32_t statRendFrames = 0;   // 실제 렌더 프레임
static uint32_t statDropFrames = 0;   // 유실로 폐기된 프레임
static uint32_t statReconstructs = 0; // FEC로 복원된 프레임
static uint32_t statMissChunks = 0;   // [FIX#R3] 드롭된 프레임의 미수신 청크 수 집계 (miss=)
static unsigned long renderTimeSum = 0;
static uint16_t renderCount = 0;

volatile unsigned long frameCount = 0;
unsigned long lastFpsTime = 0;
float currentFps = 0.0f;
bool isReceiving = false;

// Wi-Fi 정보
String stored_ssid = "";
String stored_pass = "";

// 함수 선언
void runTouchWifiSetup();
String selectWifiFromList();
String getTouchInput(const String& prompt, bool isPassword);
void drawBootScreen(const char* statusMsg);
void drawReadyScreen(String ipAddr);
void drawErrorScreen(const char* errMsg);
void onUdpPacketReceived(AsyncUDPPacket packet);
void ensureChasePool();
void ensureJpegBuffers();
#if RENDER_TEAR_SYNC
void calibrateScan();
#endif

// ==========================================
// 2. Setup 함수
// ==========================================
void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n--- CYD Wireless Monitor (Stable Auto-Detect) ---");

  lcd.init();
  lcd.setRotation(currentRotation);
  lcd.setBrightness(200);
#if RENDER_TEAR_SYNC
  calibrateScan();       // [TEARSYNC] getScanLine(0x45) 동작 검증 — 체이스 풀 확보 여부를 정하므로 먼저
#endif
  // rx/render JPEG 버퍼(48KB)만 여기서 받는다. 프레임 저장소(154KB)는 절대 부팅 직후에
  // 받지 않는다 — Wi-Fi 스택보다 먼저 154KB를 쓰면 접속이 실패한다 (2026-08-24 실측:
  // 잔여 98KB로도 IDF5 Wi-Fi가 연결 못함). 확보는 udp.listen 직후로 미룬다.
  ensureJpegBuffers();

  // [TEST] SPI 클럭 검증: fillScreen(320x240 전체 픽셀 푸시) 시간으로 클럭 효과 확인.
  //   80MHz → 약 15ms, 40MHz → 약 30ms. (클럭 상향이 실제 적용되는지 판별)
  unsigned long t0 = millis();
  lcd.fillScreen(lcd.color565(40, 80, 200));
  unsigned long fillMs = millis() - t0;
  Serial.printf("[TEST] fillScreen 320x240 = %lums (클럭 효과 확인: 80MHz≈15ms, 40MHz≈30ms)\n", fillMs);

  prefs.begin("cyd_wifi", false);
  stored_ssid = prefs.getString("ssid", "");
  stored_pass = prefs.getString("pass", "");

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

    if (udp.listen(UDP_PORT)) {
      Serial.printf("[AsyncUDP] Listening on port %d\n", UDP_PORT);
      udp.onPacket(onUdpPacketReceived);
      // 밴드 체이스 블록 풀(~75KB)은 Wi-Fi 스택이 자리 잡은 '뒤에' 확보한다.
      // 부팅 직후 대량 할당을 먼저 받으면 잔여 ~98KB로는 Wi-Fi 접속 자체가 실패한다 (실측).
      // 여기서도 잔여 heap < 48KB 안전선 검사가 동반되므로, 메모리가 부족한 부팅에선
      // 자동으로 직접 디코드 모드(풀 없음)로 떨어진다 — 스트리밍은 계속된다.
      ensureChasePool();
      drawReadyScreen(WiFi.localIP().toString());
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
// 3. Wi-Fi 검색 및 터치 리스트 선택 화면
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
        lcd.drawString("📶 " + name, 20, y + 18);
      }
    }

    lcd.fillRoundRect(8, 190, 90, 42, 6, page > 0 ? lcd.color565(37, 99, 235) : lcd.color565(71, 85, 105));
    lcd.setTextColor(TFT_WHITE);
    lcd.setTextDatum(MC_DATUM);
    lcd.drawString("< Prev", 53, 211);

    lcd.fillRoundRect(106, 190, 108, 42, 6, lcd.color565(15, 118, 110));
    lcd.drawString("🔄 Re-Scan", 160, 211);

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
// 4. 터치 가상 키보드
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

// Wi-Fi 설정 모드 진입. 부팅 시 터치(forceSetup) 또는 스트리밍 중 3초 long-touch 시 호출된다.
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
// 5. AsyncUDP 수신 콜백
// ==========================================
void resetSessionState() {
  // 세션 상태를 완전히 초기화합니다.
  // 재시작 또는 타임아웃 시 호출하여 낡은 frame_id 비교 문제를 방지합니다.
  latestFrameId = 0;
  expectedChunks = 0;
  receivedChunks = 0;
  dataChunks = 0;
  chunkReceivedMask = 0;
  chunkExpectedMask = 0;
  fecHasParity = false;
  frameCommitted = false;
  pendingCommit = false;   // [FIX#R2] 대기 중인 프레임이 있으면 폐기 (세션 리셋)
  lastPayloadLen = 0;
  // fecEnabled는 리셋하지 않음: 제어 패킷이 프로토콜 모드를 관리 (타임아웃 후에도 유지)
  Serial.println("[UDP] 세션 상태 초기화 완료. latestFrameId 리셋.");
}

// JPEG의 실제 끝(EOI = 0xFFD9) 위치를 버퍼 끝에서 역방향 스캔으로 찾습니다.
// 엔트로피 데이터 내부의 0xFF는 반드시 0x00으로 스터핑되므로, 마지막 0xFFD9가 진짜 EOI입니다.
size_t findJpegEoi(const uint8_t* buf, size_t scanEnd) {
  if (scanEnd < 2) return 0;
  for (size_t i = scanEnd - 2; i > 0; i--) {
    if (buf[i] == 0xFF && buf[i + 1] == 0xD9) return i + 2;
  }
  if (buf[0] == 0xFF && buf[1] == 0xD9) return 2;
  return 0;
}

// [FIX#R2] rxBuffer의 완성 프레임을 renderBuffer로 옮기는 실제 스테이징.
// EOI(0xFFD9)는 항상 마지막 블록에 있으므로(패딩은 마지막 블록에만 존재) 마지막 블록만 스캔합니다.
// 성공 시 hasNewFrame=true → loop()가 바로 렌더. (렌더 중 호출 시에는 절대 실행 안 됨)
bool stageCompletedFrame() {
  size_t dataLen = (size_t)dataChunks * PACKET_PAYLOAD_SIZE; // ≤ MAX_JPEG_SIZE
  if (dataLen > MAX_JPEG_SIZE) dataLen = MAX_JPEG_SIZE;

  if (dataLen >= 4 && rxBuffer[0] == 0xFF && rxBuffer[1] == 0xD8) {
    size_t scanStart = dataLen - PACKET_PAYLOAD_SIZE; // 마지막 블록 시작
    size_t eoiLen = findJpegEoi(rxBuffer + scanStart, PACKET_PAYLOAD_SIZE);
    size_t totalBytes = (eoiLen > 0) ? (scanStart + eoiLen) : dataLen; // EOI 미검출 → 전체 길이 폴백

    memcpy(renderBuffer, rxBuffer, totalBytes);
    renderBufferSize = totalBytes;
    hasNewFrame = true;
    frameCommitted = true;
    statRecvFrames++;
    return true;
  }
  Serial.printf("[UDP] JPEG 매직 바이트 불일치! 첫 2바이트: 0x%02X 0x%02X\n", rxBuffer[0], rxBuffer[1]);
  return false;
}

// 완성된 프레임을 renderBuffer로 옮기고 렌더 큐에 넣습니다. (FEC 이후 호출)
// [FIX#R1] 렌더(Core1) 중 renderBuffer를 덮어쓰면 JPEG 중간이 뒤섞여 화면이 크게 깨짐.
// (콜백은 Core0에서 호출됨)
// [FIX#R2] 렌더 중 완성된 프레임은 버리지 않고 pendingCommit만 세워 둔다. rxBuffer의
// 데이터를 보존해 렌더가 끝나는 즉시 loop()가 스테이징 → 디바이스가 자기 속도로 항상
// 최신 프레임을 그려 "버스트+프리즈" 지연이 사라진다.
void commitFrame() {
  if (isRendering) {
    pendingCommit = true;
    frameCommitted = true;  // 완성은 확정 (드롭 카운터 중복 방지)
    return;
  }
  stageCompletedFrame();
}

// 레거시(패딩 없는) 호스트용 커밋: 마지막 청크의 실제 길이로 정확히 잘라냅니다.
void commitLegacyFrame() {
  if (isRendering) return;   // [FIX#R1] 렌더 중 덮어쓰기 방지 (위 commitFrame 주석 참조)
  size_t totalBytes = ((size_t)(dataChunks - 1) * PACKET_PAYLOAD_SIZE) + lastPayloadLen;
  if (totalBytes >= 4 && totalBytes <= MAX_JPEG_SIZE &&
      rxBuffer[0] == 0xFF && rxBuffer[1] == 0xD8) {
    memcpy(renderBuffer, rxBuffer, totalBytes);
    renderBufferSize = totalBytes;
    hasNewFrame = true;
    frameCommitted = true;
    statRecvFrames++;
  } else {
    Serial.printf("[UDP] 레거시 프레임 길이 오류: %u (chunks=%u, last=%u)\n",
                  (unsigned)totalBytes, (unsigned)dataChunks, (unsigned)lastPayloadLen);
  }
}

// 유실된 데이터 청크 1개를 패리티 XOR로 복원합니다.
void reconstructMissingChunk(uint32_t missingMask) {
  int missingIdx = 0;
  while (!(missingMask & (1u << missingIdx))) missingIdx++;

  if (missingIdx >= dataChunks) return;

  size_t base = (size_t)missingIdx * PACKET_PAYLOAD_SIZE;
  for (size_t j = 0; j < PACKET_PAYLOAD_SIZE; j++) {
    uint8_t x = parityBuffer[j];
    for (int i = 0; i < dataChunks; i++) {
      if (i == missingIdx) continue;
      x ^= rxBuffer[(size_t)i * PACKET_PAYLOAD_SIZE + j];
    }
    rxBuffer[base + j] = x;
  }
  statReconstructs++;
  // [FIX#R3] FEC 프린트가 AsyncUDP 콜백을 막아 유실을 증폭하는 죽음의 나선 방지.
  // Serial.printf는 115200보드에서 한 줄당 ~3.5ms씩 콜백(Core0 TCPIP)을 블록.
  // FEC가 늘어날수록 블록→유실→FEC가 무한 증폭되어 "시간이 지나면 FPS 저하" 패턴을 만든다.
  // 최초 5건만 출력하고 이후 집계는 [STAT] fec= 카운트로만 남긴다.
  static uint8_t fecDbg = 0;
  if (fecDbg < 5) {
    Serial.printf("[FEC] 프레임 %u 청크 %d 복원 (패리티)\n", latestFrameId, missingIdx);
    fecDbg++;
  }
}

void onUdpPacketReceived(AsyncUDPPacket packet) {
  size_t len = packet.length();
  if (len < 8) return;

  const uint8_t* data = packet.data();

  uint32_t frameId = ((uint32_t)data[0] << 24) | ((uint32_t)data[1] << 16) | ((uint32_t)data[2] << 8) | (uint32_t)data[3];
  
  // ---- 제어 명령 패킷 (0xFFFFFFFF) ----
  // [FIX] 제어 패킷을 스트림 재시작 신호로도 활용: 세션 상태를 리셋합니다.
  // Python streamer가 스트리밍 시작 시 제어 패킷을 먼저 보내므로,
  // 이를 수신하는 순간 낡은 latestFrameId를 지워 재시작 후 드롭 문제를 방지합니다.
  if (frameId == 0xFFFFFFFF) {
    uint8_t reqRotation = data[4];
    if (reqRotation <= 7) {
      if (reqRotation != currentRotation) {
        currentRotation = reqRotation;
        lcd.setRotation(currentRotation);
        Serial.printf("[CTRL] 화면 회전 변경: %d\n", currentRotation);
      }
    }
    showFpsOverlay = (data[5] != 0);
    // 호스트가 FEC 프로토콜(패딩+패리티)을 쓰는지 통보 (data[6]).
    // 레거시(패딩 없음) 호스트와 섞여도 마지막 청크를 데이터로 해석해 화면 보존.
    fecEnabled = (data[6] != 0);
    Serial.printf("[CTRL] 제어 패킷 수신 → 세션 리셋, FEC 모드=%s\n", fecEnabled ? "ON" : "OFF(레거시)");
    resetSessionState();
    lastPacketTime = millis();
    return;
  }

  // ---- 데이터 패킷: 프레임 재조립 상태 전이 ----
  // 프레임 수명주기:
  //   1. 새 frame_id 수신 → latestFrameId 갱신 + 청크 마스크/카운터 초기화 (새 프레임 시작)
  //   2. 각 청크를 rxBuffer에 저장 (FEC 모드면 마지막 청크는 parityBuffer로 분리)
  //   3. 판정: dataMissing==0 → 즉시 커밋 · 데이터 1개 유실+패리티 → XOR 복원 후 커밋 · 그 외 → 대기
  // 커밋(commitFrame)은 렌더(Core1) 중이면 pendingCommit만 세우고, loop()가 렌더 종료 후 스테이징한다.
  // [FIX#R2] pendingCommit 동안 rxBuffer에는 "스테이징 대기 중인 완성 프레임"이 있다.
  // 새 프레임이 rxBuffer를 덮어쓰면 대기 프레임이 파괴되므로, loop()가 스테이징할 때까지
  // 데이터 패킷을 무시한다. (렌더 종료 후 loop()가 µs 안에 스테이징 → 손실은 수 프레임 한정)
  if (pendingCommit) {
    return;
  }

  // FEC 구성: totalChunks = 데이터 청크 N개 + 패리티 1개 (마지막 인덱스). 최소 2.
  uint16_t totalChunks = ((uint16_t)data[4] << 8) | (uint16_t)data[5];
  uint16_t chunkIdx    = ((uint16_t)data[6] << 8) | (uint16_t)data[7];
  size_t   payloadLen  = len - 8;

  if (totalChunks < 2) return;

  isReceiving = true;
  lastPacketTime = millis();
  streamActive = true;

  // ---- [FIX] frame_id 역전(재시작) 처리 ----
  // 기존 로직: frameId < latestFrameId면 무조건 드롭 → 재시작 시 몇 분간 먹통!
  // 수정 로직:
  //   - frameId가 크게 역전(gap > 5000)되면 새 세션 시작으로 판단 → 세션 리셋
  //   - 소폭 역전(gap <= 5000)은 네트워크 순서 역전(reorder)으로 판단 → 드롭
  if (frameId < latestFrameId) {
    uint32_t gap = latestFrameId - frameId;
    if (gap > 5000) {
      // 큰 역전 = 재시작으로 판단 (Python이 frame_id를 0부터 다시 시작)
      Serial.printf("[UDP] frame_id 큰 역전 감지 (gap=%u). 새 세션으로 판단, 리셋.\n", gap);
      resetSessionState();
      // 리셋 후 아래 로직으로 계속 진행
    } else {
      // 소폭 역전 = 네트워크 패킷 순서 역전 → 무시
      return;
    }
  }

  // 새 프레임 시작
  if (frameId != latestFrameId) {
    // 이전 프레임이 청크 유실로 완성되지 못했다면 드롭 카운트
    if (receivedChunks > 0 && !frameCommitted) {
      statDropFrames++;
      // [FIX#R3] 진단: 폐기된 프레임에서 실제로 몇 청크가 유실됐는지 집계.
      // 이 시점의 dataChunks/chunkExpectedMask/chunkReceivedMask는 "이전 프레임" 상태.
      // miss=1이 대부분이면 1-패리티로 충분, miss≥2가 상당수면 2-패리티 FEC 필요.
      if (dataChunks <= MAX_CHUNKS_BITMASK) {
        statMissChunks += __builtin_popcount(chunkExpectedMask & ~chunkReceivedMask);
      } else {
        statMissChunks += 1; // 카운터 폴백 모드: 비트마스크 추적 불가 → 최소 1 유실로 가정
      }
    }
    latestFrameId = frameId;
    expectedChunks = totalChunks;
    dataChunks     = fecEnabled ? (totalChunks - 1) : totalChunks;
    receivedChunks = 0;
    lastPayloadLen = 0;
    // [FIX] 비트마스크 초기화 (최대 MAX_CHUNKS_BITMASK 청크까지 추적)
    chunkReceivedMask = 0;
    fecHasParity      = false;
    frameCommitted    = false;
    if (totalChunks <= MAX_CHUNKS_BITMASK) {
      chunkExpectedMask = (totalChunks == 32) ? 0xFFFFFFFF : ((1u << totalChunks) - 1);
    } else {
      chunkExpectedMask = 0; // 청크 수 초과: bitmask 비활성화, count 방식으로 폴백
    }
  }

  // ---- 청크 저장 (FEC: 마지막 인덱스=패리티 / 레거시: 전부 데이터) ----
  if (chunkIdx < MAX_CHUNKS_BITMASK) {
    if (fecEnabled) {
      if (chunkIdx == totalChunks - 1) {
        // FEC 패리티 청크
        if (!(chunkReceivedMask & (1u << chunkIdx))) {
          chunkReceivedMask |= (1u << chunkIdx);
          receivedChunks++;
        }
        if (payloadLen <= PACKET_PAYLOAD_SIZE) {
          memcpy(parityBuffer, data + 8, payloadLen);
          fecHasParity = true;
        }
      } else {
        // FEC 데이터 청크 (호스트가 1400B로 제로패딩)
        size_t offset = (size_t)chunkIdx * PACKET_PAYLOAD_SIZE;
        if (offset + payloadLen <= MAX_JPEG_SIZE) {
          memcpy(rxBuffer + offset, data + 8, payloadLen);
          // bitmask 방식: 이미 받은 청크는 중복 카운트 없음
          if (!(chunkReceivedMask & (1u << chunkIdx))) {
            chunkReceivedMask |= (1u << chunkIdx);
            receivedChunks++;
          }
        }
      }
    } else {
      // 레거시: 모든 청크가 데이터 (마지막 청크는 가변 길이)
      size_t offset = (size_t)chunkIdx * PACKET_PAYLOAD_SIZE;
      if (offset + payloadLen <= MAX_JPEG_SIZE) {
        memcpy(rxBuffer + offset, data + 8, payloadLen);
        if (!(chunkReceivedMask & (1u << chunkIdx))) {
          chunkReceivedMask |= (1u << chunkIdx);
          receivedChunks++;
        }
      }
      if (chunkIdx == totalChunks - 1) {
        lastPayloadLen = payloadLen;   // 정확한 JPEG 길이 산출용
      }
    }
  } else {
    // 32개 이상 청크 (bitmask 범위 초과) → 카운트 방식 폴백 (유효 프레임에서 미사용)
    if (fecEnabled && chunkIdx == totalChunks - 1) {
      if (payloadLen <= PACKET_PAYLOAD_SIZE) {
        memcpy(parityBuffer, data + 8, payloadLen);
        fecHasParity = true;
      }
      receivedChunks++;
    } else {
      size_t offset = (size_t)chunkIdx * PACKET_PAYLOAD_SIZE;
      if (offset + payloadLen <= MAX_JPEG_SIZE) {
        memcpy(rxBuffer + offset, data + 8, payloadLen);
        receivedChunks++;
      }
      if (!fecEnabled && chunkIdx == totalChunks - 1) {
        lastPayloadLen = payloadLen;
      }
    }
  }

  // ---- 프레임 완성 판정 (FEC 복원 포함) ----
  if (expectedChunks <= MAX_CHUNKS_BITMASK && dataChunks > 0 && !frameCommitted) {
    uint32_t dataMask = (dataChunks >= 32) ? 0xFFFFFFFFu : ((1u << dataChunks) - 1);
    uint32_t dataMissing = dataMask & ~chunkReceivedMask;

    if (dataMissing == 0) {
      // 모든 데이터 청크 수신 완료 → 즉시 커밋
      if (fecEnabled) {
        commitFrame();            // FEC: 패딩 포함 → EOI 스캔으로 실제 길이 확정
      } else {
        commitLegacyFrame();      // 레거시: (N-1)*1400 + lastPayloadLen 정확 길이
      }
    } else if (fecEnabled && fecHasParity && (dataMissing & (dataMissing - 1)) == 0) {
      // FEC 데이터 1개 유실 + 패리티 보유 → XOR 복원 후 커밋
      reconstructMissingChunk(dataMissing);
      commitFrame();
    }
    // 그 외 (유실 2개+ 또는 패리티 부족) → 다음 청크/프레임 대기
  } else if (expectedChunks > MAX_CHUNKS_BITMASK && !frameCommitted) {
    // 청크 수 초과: FEC 없이 총 청크 수 기준 (안전망 전용)
    bool allReceived = (receivedChunks == expectedChunks);
    if (allReceived && expectedChunks > 0 && !isRendering) {   // [FIX#R1] 렌더 중 덮어쓰기 방지
      size_t totalBytes = ((expectedChunks - 1) * PACKET_PAYLOAD_SIZE) + payloadLen;
      if (totalBytes >= 4 && rxBuffer[0] == 0xFF && rxBuffer[1] == 0xD8) {
        memcpy(renderBuffer, rxBuffer, totalBytes);
        renderBufferSize = totalBytes;
        hasNewFrame = true;
        frameCommitted = true;
      }
    }
  }
}

// ==========================================
// 5.5 JPEG 고속 디코드 (JPEGDEC) — 디코드 병목 제거 + 오프스크린 티어링 방지
// ==========================================
// [계측] 렌더 내 "픽셀 푸시"(SPI blit) 시간(µs) 누적. [STAT]의 rt에서 이 값을 빼면
// 실제 JPEG 디코드 시간이 나옴 → 다음 병목(디코드 vs 푸시) 판별용.
// 스프라이트 모드: pushSprite()의 SPI 시간. 직접 모드(폴백): 콜백 pushImage()의 SPI 시간.
static unsigned long pushTimeSum = 0;

// jpegDbg: 처음 몇 프레임만 진단 출력 (통과 후엔 조용)
static int jpegDbg = 0;

// ---- [TEARSYNC] 밴드 체이스: MCU 블록 풀 + 헤드 게이트 ----
// 블록 1개 = 16×16 RGB565 = 512B. 화면은 (W/16)×(H/16) = 300블록/프레임 (양 회전 공통).
// 디코더가 MCU 순서대로 주므로 각 셀은 프레임당 정확히 1번 기입된다(MCU 패딩은 클립으로 절단,
// W/H가 16의 배수라 셀 경계와 일치 — 320×240/240×320 모두 성립).
#define CHASE_BLOCK_PX    16
#define CHASE_BLOCK_BYTES (CHASE_BLOCK_PX * CHASE_BLOCK_PX * 2)          // 512B
// 풀 크기: 최악 백로그 ~136블록(헤드 랩 직후 전체 미방출) + 여유 → 144슬롯.
// 하나의 연속 할당(≈75KB)이라 힙 최대연속(107KB 실측) 안에 들어오고, Wi-Fi 후 잔여
// ~149KB에서 받으면 ~75KB 남아 48KB 안전선을 통과한다.
#define CHASE_POOL_BLOCKS 144
struct ChaseBlock {
  uint16_t px[CHASE_BLOCK_PX * CHASE_BLOCK_PX];  // RGB565_BIG_ENDIAN 픽셀 (콜백 산출 그대로)
  int16_t x, y;                                  // 화면 셀 좌표 (좌상단, 16의 배수)
  uint8_t used;
};
static ChaseBlock* chasePool = nullptr;  // udp.listen 직후 1회 힙 할당

#if RENDER_TEAR_SYNC
static inline int tearSyncHeadPos(int axisLen);   // 정의는 §5.6 캘리브레이션 아래

// 확장 좌표 헤드 추적: raw GSCAN 랩마다 axisLen을 누적해 단조 증가값으로 만든다.
// 프레임 경계를 넘어 계속 유지된다(헤드는 쉬지 않고 도니까). 폴링은 400µs 스로틀 —
// 매 블록마다 SPI read하면 300회/프레임이 병목이 된다.
static int chaseBase = 0, chasePrevRaw = -1, chaseCachedExt = 0;
static uint32_t chaseLastPollUs = 0;

static int chaseHeadExt(int axisLen) {
  uint32_t now = micros();
  if (chaseLastPollUs != 0 && now - chaseLastPollUs < 400) return chaseCachedExt;
  chaseLastPollUs = now;
  int raw = tearSyncHeadPos(axisLen);
  if (chasePrevRaw >= 0 && raw < chasePrevRaw - 64) chaseBase += axisLen;  // 랩 감지
  chasePrevRaw = raw;
  chaseCachedExt = chaseBase + raw;
  return chaseCachedExt;
}

// 안전해진 블록을 LCD로 방출한다. force=false: 헤드 게이트 통과분만. force=true: 전부(프레임 끝).
// 게이트를 우회하는 강제 방출분만 [STAT] lag=으로 센다 (티어 가능성이 있는 유일한 분량).
static void chaseDrain(bool force) {
  if (!chaseReady) return;
  const bool landscape = lcd.width() > lcd.height();
  const int axisLen = landscape ? lcd.width() : lcd.height();   // 항상 320 (양 회전 공통)
  int ext = force ? INT32_MAX : chaseHeadExt(axisLen);
  for (int i = 0; i < CHASE_POOL_BLOCKS; i++) {
    ChaseBlock& b = chasePool[i];
    if (!b.used) continue;
    if (!force) {
      // 가로 회전: 래스터가 x축을 훑으므로 열 끝 기준. 세로: 행 끝 기준.
      const int endCoord = (landscape ? b.x : b.y) + CHASE_BLOCK_PX;
      if (ext < endCoord + TEAR_SYNC_GAP) continue;   // 헤드가 아직 안 지남 → 대기
    } else {
      statChaseLag++;
    }
    unsigned long t0 = micros();
    lcd.pushImage(b.x, b.y, CHASE_BLOCK_PX, CHASE_BLOCK_PX, b.px);
    pushTimeSum += (micros() - t0);
    b.used = 0;
  }
}

// 콜백에서 온 MCU 블록을 셀 단위로 풀에 적재하고 안전분을 즉시 방출한다.
static void chaseAccept(JPEGDRAW* pDraw) {
  const int W = lcd.width(), H = lcd.height();
  int x = pDraw->x, y = pDraw->y;
  int vw = pDraw->iWidth, vh = pDraw->iHeight;
  if (x + vw > W) vw = W - x;
  if (y + vh > H) vh = H - y;
  const uint16_t* src = (const uint16_t*)pDraw->pPixels;

  for (int cy = (y & ~(CHASE_BLOCK_PX - 1)); cy < y + vh; cy += CHASE_BLOCK_PX) {
    for (int cx = (x & ~(CHASE_BLOCK_PX - 1)); cx < x + vw; cx += CHASE_BLOCK_PX) {
      ChaseBlock* b = nullptr;
      for (int i = 0; i < CHASE_POOL_BLOCKS; i++) {
        if (!chasePool[i].used) { b = &chasePool[i]; break; }
      }
      if (b == nullptr) {
        // 풀 포화 (설계상 나오지 않음 — 최악 백로그 136 < 144): 게이트 우회 즉시 푸시.
        // 프레임 무결성을 티어보다 우선하는 안전망.
        unsigned long tf = micros();
        for (int r = 0; r < CHASE_BLOCK_PX && cy + r < y + vh; r++) {
          lcd.pushImage(cx, cy + r, CHASE_BLOCK_PX, 1,
                        src + (size_t)r * pDraw->iWidth + (cx - x));
        }
        pushTimeSum += (micros() - tf);
        statChaseLag++;
        continue;
      }
      b->x = (int16_t)cx;
      b->y = (int16_t)cy;
      for (int r = 0; r < CHASE_BLOCK_PX; r++) {
        memcpy(&b->px[(size_t)r * CHASE_BLOCK_PX],
               src + (size_t)r * pDraw->iWidth + (cx - x),
               CHASE_BLOCK_BYTES / CHASE_BLOCK_PX);
      }
      b->used = 1;
    }
  }
  chaseDrain(false);   // 새 블록으로 안전해진 것들이 있으면 곧바로 방출
}
#endif  // RENDER_TEAR_SYNC

#if RENDER_INTERLEAVE
static uint8_t* stripBuf = nullptr; // [INTERLEAVE] 줄 버퍼 (320x16 RGB565 = 10KB, 힙 할당).
                                    // 인터리브는 실측 실패(위 RENDER_INTERLEAVE 주석)로 0 고정 → 이 선언도 함께 꺼짐
#endif

// 밴드 체이스 블록 풀 확보 — udp.listen 직후 딱 1회 호출 (호출자가 위치를 책임진다).
// **Wi-Fi 접속 전에 호출 금지**: 부팅 직후 대량 할당은 Wi-Fi 스택을 굶긴다 (2026-08-24 실측:
// 슬롯 풀 154KB 선점 시 잔여 ~98KB로 접속 실패). 프레임마다 재시도도 금지 — heap 스캔+
// 시리얼 출력이 Core1 렌더를 지연시킨다.
// 하나의 연속 할당(≈75KB)이라 힙 최대연속(107KB 실측 벽) 안에 들어오고, 잔여 heap이
// 48KB 미만이 되면 Wi-Fi/lwIP가 불안정해지므로 그 경우 반납하고 직접 디코드 모드를 유지한다.
void ensureChasePool() {
#if RENDER_INTERLEAVE
  // 인터리브 모드: 줄 버퍼(10KB)만 최초 1회 힙 할당 → heap 절약.
  if (stripBuf == nullptr) {
    chaseAllocTried = true;
    stripBuf = (uint8_t*)heap_caps_malloc((size_t)320 * 16 * 2, MALLOC_CAP_8BIT);
    if (stripBuf == nullptr) {
      Serial.printf("[STRIP] 줄 버퍼 할당 실패 (free heap=%uKB) → 블록 직접 푸시 고정 (재시도 없음)\n",
                    (unsigned)(ESP.getFreeHeap() / 1024));
    } else {
      Serial.println("[STRIP] 줄 버퍼 활성 (320x16, 인터리브 푸시)");
    }
  }
  chaseReady = false;   // 체이스 미사용
  return;
#else
  if (chaseAllocTried || chaseReady) return;   // 래치: 1회만 시도
  chaseAllocTried = true;
#if RENDER_TEAR_SYNC
  if (!tearSyncOk) return;                     // 캘리브레이션 실패 → 직접 디코드 모드 유지
#endif

  chasePool = (ChaseBlock*)heap_caps_malloc(sizeof(ChaseBlock) * CHASE_POOL_BLOCKS, MALLOC_CAP_8BIT);
  if (chasePool != nullptr && ESP.getFreeHeap() < 48 * 1024) {
    heap_caps_free(chasePool);   // 안전선 미달 → 반납
    chasePool = nullptr;
  }
  if (chasePool == nullptr) {
    Serial.printf("[CHASE] 블록 풀 확보 실패 (free=%uKB, 최대연속=%uKB, 필요=%uKB) → 직접 디코드 모드\n",
                  (unsigned)(ESP.getFreeHeap() / 1024),
                  (unsigned)(heap_caps_get_largest_free_block(MALLOC_CAP_8BIT) / 1024),
                  (unsigned)(sizeof(ChaseBlock) * CHASE_POOL_BLOCKS / 1024));
    return;
  }
  for (int i = 0; i < CHASE_POOL_BLOCKS; i++) chasePool[i].used = 0;
  chaseReady = true;
  Serial.printf("[CHASE] 블록 풀 활성 (%d×512B=%uKB, 잔여 heap=%uKB)\n",
                CHASE_POOL_BLOCKS,
                (unsigned)(sizeof(ChaseBlock) * CHASE_POOL_BLOCKS / 1024),
                (unsigned)(ESP.getFreeHeap() / 1024));
#endif
}

// rx/render JPEG 버퍼(각 MAX_JPEG_SIZE)를 힙에서 확보. 부팅 직후(Wi-Fi 접속 전) 호출 —
// 이 버퍼 없이는 스트리밍 자체가 불가하므로 실패 시 재부팅으로 안전 정지한다.
// (체이스 블록 풀은 이보다 크고 Wi-Fi 스택을 굶길 수 있어 ensureChasePool로 udp.listen 직후 미룸)
void ensureJpegBuffers() {
  if (rxBuffer != nullptr) return;
  rxBuffer = (uint8_t*)heap_caps_malloc(MAX_JPEG_SIZE, MALLOC_CAP_8BIT);
  renderBuffer = (uint8_t*)heap_caps_malloc(MAX_JPEG_SIZE, MALLOC_CAP_8BIT);
  if (rxBuffer == nullptr || renderBuffer == nullptr) {
    Serial.printf("[JPEGBUF] 버퍼 할당 실패 (free=%uKB) → 재부팅\n",
                  (unsigned)(ESP.getFreeHeap() / 1024));
    delay(2000);
    ESP.restart();
  }
}

#if RENDER_INTERLEAVE
// ---- 인터리브 푸시용 줄 어셈블러 ----
// JPEGDEC는 위→아래로 MCU(16×16) 블록을 콜백한다. 가로 한 줄(전폭)이 모이면 곧바로 LCD로
// 푸시한다 — renderJpegFast가 startWrite/endWrite 트랜잭션으로 감싸므로 줄 푸시들이 하나의
// SPI DMA 흐름으로 큐잉되고, 다음 줄의 IDCT 디코드(CPU)와 겹친다.
// 줄 버퍼(stripBuf)는 ensureChasePool에서 최초 1회 힙 할당; 실패 시 블록 직접 푸시 폴백.
static int16_t stripY = -1;      // 누적 중인 줄의 화면 y (-1 = 진행 없음)
static int16_t stripFillW = 0;   // 현재 줄에 누적된 가로 폭(px)
static int16_t stripH = 0;       // 이 줄 묶음의 높이(px, MCU별 8 또는 16)

static void stripReset() { stripY = -1; stripFillW = 0; stripH = 0; }

static void stripFlush() {
  if (stripBuf == nullptr || stripFillW <= 0 || stripY < 0) return;
  unsigned long t0 = micros();
  lcd.pushImage(0, stripY, stripFillW, stripH, (const uint16_t*)stripBuf);
  pushTimeSum += (micros() - t0);
  stripFillW = 0;
}

static void stripAccumulate(JPEGDRAW* pDraw) {
  const int pw = lcd.width();   // 회전 반영 패널 폭 (가로 320 / 세로 모드 240)
  if (pDraw->y != stripY) {     // 새 줄 시작 → 이전 줄 플러시
    stripFlush();
    stripY = pDraw->y;
    stripFillW = 0;
    stripH = pDraw->iHeight;
  }
  int cw = pDraw->iWidth;
  if (cw > pw - stripFillW) cw = pw - stripFillW;   // MCU 패딩 클립 (iWidth가 초과분 포함 가능)
  if (cw <= 0) return;
  uint16_t* dst = (uint16_t*)stripBuf;
  const uint16_t* src = (const uint16_t*)pDraw->pPixels;
  for (int r = 0; r < pDraw->iHeight; r++) {
    memcpy(dst + (size_t)r * pw + stripFillW, src + (size_t)r * pDraw->iWidth, (size_t)cw * 2);
  }
  stripFillW += cw;
  if (stripFillW >= pw) stripFlush();
}
#endif

// JPEGDEC 디코드 콜백: 디코드된 RGB565 블록을 줄 단위 LCD(인터리브)/스프라이트(RAM)/LCD(직접)로.
// JPEGDEC 디코드는 동기식이라 이 콜백은 loop() 스레드에서 실행됨 → 렌더 경합 없음.
int jpegDrawCallback(JPEGDRAW* pDraw) {
#if RENDER_INTERLEAVE
  if (stripBuf != nullptr) {
    stripAccumulate(pDraw);
  } else {
    // 줄 버퍼 할당 실패 폴백: MCU 블록을 즉시 LCD로 푸시 (원래 직접 모드).
    unsigned long t0 = micros();
    lcd.pushImage(pDraw->x, pDraw->y, pDraw->iWidth, pDraw->iHeight, pDraw->pPixels);
    pushTimeSum += (micros() - t0);
  }
#else
#if RENDER_TEAR_SYNC
  if (chaseReady) {
    // [TEARSYNC] 밴드 체이스 경로: MCU를 16×16 셀로 풀에 적재하고 헤드가 지나간 셀부터 방출.
    chaseAccept(pDraw);
    return 1;
  }
#endif
  // 직접 모드 폴백: 블록 풀이 없으면 MCU 블록을 즉시 LCD로 푸시 (원래 방식).
  unsigned long t0 = micros();
  lcd.pushImage(pDraw->x, pDraw->y, pDraw->iWidth, pDraw->iHeight, pDraw->pPixels);
  pushTimeSum += (micros() - t0);
  return 1;
#endif
  return 1;
}

// ==========================================
// 5.6 [TEARSYNC] 스캔 동기화 표시 (래스터 체이스)
// ==========================================
#if RENDER_TEAR_SYNC

// 부팅 직후 1회 getScanLine() 동작 검증. ST7789의 0x45는 패널 구현 의존이라
// (a) 값이 유효 범위 내에서 (b) 래스터 주기(~16.7ms)마다 랩하며 (c) 충분한 폭을 순회하는지
// 40ms 샘플로 확인한다. 실패 시 tearSyncOk=false 고정 → 통상 원자 blit 경로로 동작.
void calibrateScan() {
  const uint32_t WIN_MS = 40;
  int last = -1, mn = 32767, mx = -1;
  uint32_t wraps = 0;
  uint32_t t0 = millis();
  while (millis() - t0 < WIN_MS) {
    int sl = lcd.getScanLine();
    if (sl < 0 || sl > 1023) {
      Serial.printf("[SCAN] 비정상 값 %d → 티어싱크 비활성 (원자 blit 유지)\n", sl);
      return;
    }
    if (last >= 0 && sl < last - 64) wraps++;
    if (sl < mn) mn = sl;
    if (sl > mx) mx = sl;
    last = sl;
    delayMicroseconds(150);
  }
  if (wraps >= 1 && (mx - mn) >= 200) {
    tearSyncOk = true;
    Serial.printf("[SCAN] 캘리브레이션 OK: 범위 %d..%d, 랩 %u회/40ms → 티어싱크 활성\n", mn, mx, (unsigned)wraps);
  } else {
    Serial.printf("[SCAN] 캘리브레이션 실패(범위 %d..%d, 랩 %u) → 원자 blit 유지\n", mn, mx, (unsigned)wraps);
  }
}

// GSCAN(네이티브 게이트선 위치) → 현재 회전의 표시축 논리 좌표.
// 가로/세로 모두 표시축 길이가 게이트선 수(320)와 1:1이라 스케일 변환은 없고 방향만 패널 의존.
// 체이스가 동작하는데 티어링이 그대로면 TEAR_SYNC_MAP_FLIP을 1로 전환한다.
static inline int tearSyncHeadPos(int axisLen) {
  int sl = lcd.getScanLine();
  if (sl < 0) sl = 0;
  if (sl > axisLen - 1) sl = axisLen - 1;
#if TEAR_SYNC_MAP_FLIP
  return (axisLen - 1) - sl;
#else
  return sl;
#endif
}

#endif  // RENDER_TEAR_SYNC

// renderBuffer의 JPEG을 JPEGDEC로 고속 디코드 후 화면에 그립니다.
// (호스트는 항상 풀해상도 320x240/240x320을 보냄 — 반해상도 모드 제거됨)
void renderJpegFast() {
  // v1.8.4의 openRAM()은 성공 시 1을 반환. JPEG_SUCCESS 상수와 값이 달라
  // 상수 비교가 아니라 "양수면 성공"으로 판정 (rc<=0 = 실패).
  int rc = jpeg.openRAM(renderBuffer, (int)renderBufferSize, jpegDrawCallback);
  if (rc <= 0) {
    if (jpegDbg < 3) {
      Serial.printf("[JPEG] openRAM 실패 rc=%d size=%u\n", rc, (unsigned)renderBufferSize);
      jpegDbg++;
    }
    return;
  }
  jpeg.setPixelType(RGB565_BIG_ENDIAN);
#if RENDER_INTERLEAVE
  // 인터리브: 트랜잭션 안에서 decode → 콜백의 줄 단위 pushImage가 하나의 SPI DMA 흐름으로
  // 큐잉돼 다음 줄 IDCT와 겹친다. endWrite에서 마지막 flush 대기 후 잔여 시간을 push로 계상.
  stripReset();
  lcd.startWrite();
  jpeg.decode(0, 0, 0);
  jpeg.close();
  stripFlush();              // 마지막 줄이 전폭 미달이면 여기서 플러시
  unsigned long tf = micros();
  lcd.endWrite();
  pushTimeSum += (micros() - tf);
#else
  jpeg.decode(0, 0, 0);
  jpeg.close();
#if RENDER_TEAR_SYNC
  if (chaseReady) {
    // 프레임 말미 강제 플러시: 헤드가 아직 안 지난 잔여 블록도 전부 방출해 프레임을 완결한다.
    // 게이트 우회분만큼 그 경계에서 티어 가능([STAT] lag=이 그 분량) — 미표시 영역을 다음
    // 프레임까지 남겨두는 것보단 낫다. 디코드 생산(~6.5줄/ms)이 스캔(~19줄/ms)보다 느려
    // 정상 프레임에선 대부분 콜백 중 이미 방출됐을 것 → lag≈0 기대.
    unsigned long tc = micros();
    chaseDrain(true);
    pushTimeSum += (micros() - tc);
  }
#endif
#endif
}

// ==========================================
// 6. Loop 함수
// ==========================================
void loop() {
  // [FIX#R2] 렌더 직후 대기 중이던 완성 프레임을 스테이징 → 다음 루프에서 즉시 렌더.
  // 렌더 중 완성돼 드롭되던 프레임을 놓치지 않고, 디바이스가 항상 최신 프레임을 그린다.
  if (pendingCommit) {
    // memcpy가 끝날 때까지 pendingCommit을 유지해야 콜백(Core0)이 rxBuffer를 못 덮어씀
    stageCompletedFrame();
    pendingCommit = false;
  }

  if (hasNewFrame) {
    isRendering = true;
    hasNewFrame = false;

    int w = lcd.width();
    int h = lcd.height();

    unsigned long rt0 = millis();
    renderJpegFast();  // JPEGDEC 고속 디코드. 내장 drawJpg 디코더(~60ms 병목) 대체.
    renderTimeSum += (millis() - rt0);
    renderCount++;
    statRendFrames++;

    if (showFpsOverlay && currentFps > 0) {
      char fpsStr[16];
      snprintf(fpsStr, sizeof(fpsStr), "%4.1f FPS", currentFps);
      int fx = w - 74;
      int fy = h - 20;
      lcd.fillRoundRect(fx, fy, 72, 18, 3, lcd.color565(15, 23, 42));
      lcd.drawRoundRect(fx, fy, 72, 18, 3, lcd.color565(59, 130, 246));
      lcd.setTextColor(lcd.color565(52, 211, 153), lcd.color565(15, 23, 42));
      lcd.setTextSize(1);
      lcd.setTextDatum(MC_DATUM);
      lcd.drawString(fpsStr, fx + 36, fy + 9);
    }

    frameCount++;
    isRendering = false;
  }

  // ---- [FIX] 스트림 타임아웃 감지: 패킷이 오랫동안 없으면 세션 상태 리셋 ----
  // 제어 패킷 수신 시 리셋되지 않는 경우(Python 재시작이 매우 빠를 때)를 대비한 이중 안전망
  if (streamActive && lastPacketTime > 0) {
    unsigned long now = millis();
    if (now - lastPacketTime > STREAM_TIMEOUT_MS) {
      Serial.printf("[TIMEOUT] %lums 동안 패킷 없음. 세션 상태 리셋.\n", now - lastPacketTime);
      resetSessionState();
      streamActive = false;
      isReceiving  = false;
    }
  }

  if (isReceiving) {
    unsigned long now = millis();
    if (now - lastFpsTime >= 1000) {
      currentFps = (frameCount * 1000.0f) / (now - lastFpsTime);
      frameCount = 0;
      lastFpsTime = now;
    }
  }

  // ---- [STAT] 초당 1회 계측 출력 ----
  // recv=수신 완성 프레임, rend=렌더 프레임, drop=유실 폐기, fec=FEC 복원,
  // rt=렌더 평균 소요 ms, push=프레임당 픽셀 푸시 평균 ms (rt-push≈디코드 시간)
  static unsigned long lastStatTime = 0;
  unsigned long statNow = millis();
  if (statNow - lastStatTime >= 1000) {
    float rtAvg = (renderCount > 0) ? (float)renderTimeSum / (float)renderCount : 0.0f;
    float pushAvg = (renderCount > 0) ? (float)pushTimeSum / 1000.0f / (float)renderCount : 0.0f;
#if RENDER_TEAR_SYNC
    Serial.printf("[STAT] recv=%u rend=%u drop=%u fec=%u miss=%u rt=%.1fms push=%.1fms rend_fps=%.1f lag=%u\n",
                  statRecvFrames, statRendFrames, statDropFrames, statReconstructs, statMissChunks,
                  rtAvg, pushAvg, currentFps, (unsigned)statChaseLag);
#else
    Serial.printf("[STAT] recv=%u rend=%u drop=%u fec=%u miss=%u rt=%.1fms push=%.1fms rend_fps=%.1f\n",
                  statRecvFrames, statRendFrames, statDropFrames, statReconstructs, statMissChunks,
                  rtAvg, pushAvg, currentFps);
#endif
    renderTimeSum = 0;
    renderCount = 0;
    pushTimeSum = 0;
    statRecvFrames = 0;
    statRendFrames = 0;
    statDropFrames = 0;
    statReconstructs = 0;
    statMissChunks = 0;
#if RENDER_TEAR_SYNC
    statChaseLag = 0;
#endif
    lastStatTime = statNow;
  }

  // ---- 스트리밍 중 3초 long-touch → Wi-Fi 재설정 모드 ----
  // lcd.getTouch()는 소프트웨어 SPI(XPT2046) 폴링이라 매 루프마다 Core1 시간을 소모하고
  // 렌더와 경합한다. [OPT P1-2] 스트리밍 중에는 50ms 간격으로 스로틀한다 — 3초 long-touch
  // 판정은 millis 기준이라 폴링 주기와 무관(실제 3.0~3.05s에 발화). 폴링을 건너뛴 구간에서는
  // touchStart를 건드리지 않아 길게 누른 채 스로틀을 지나쳐도 카운트가 유지된다.
  // 비스트리밍(idle) 중엔 기존대로 매 루프 폴링 → 설정 UI 반응성 무변화.
  static unsigned long touchStart = 0;
  static unsigned long lastTouchPoll = 0;
  unsigned long touchNow = millis();
  if (!streamActive || (touchNow - lastTouchPoll) >= 50) {
    lastTouchPoll = touchNow;
    uint16_t tx, ty;
    if (lcd.getTouch(&tx, &ty)) {
      if (touchStart == 0) touchStart = touchNow;
      if (touchNow - touchStart > 3000) {
        touchStart = 0;
        runTouchWifiSetup();
        ESP.restart();
      }
    } else {
      touchStart = 0;
    }
  }

  if (WiFi.status() != WL_CONNECTED) {
    delay(100);
  } else {
    yield();
  }
}

// ==========================================
// 7. UI 렌더링 헬퍼
// ==========================================
void drawBootScreen(const char* statusMsg) {
  lcd.fillScreen(TFT_BLACK);
  lcd.fillRoundRect(10, 10, lcd.width() - 20, 45, 8, lcd.color565(30, 30, 60));
  lcd.drawRoundRect(10, 10, lcd.width() - 20, 45, 8, lcd.color565(80, 80, 150));
  lcd.setTextColor(TFT_WHITE);
  lcd.setTextSize(2);
  lcd.setTextDatum(MC_DATUM);
  lcd.drawString("CYD Portable Monitor", lcd.width() / 2, 32);

  lcd.setTextColor(TFT_CYAN);
  lcd.setTextSize(1);
  lcd.drawString(statusMsg, lcd.width() / 2, 120);
}

void drawReadyScreen(String ipAddr) {
  lcd.fillScreen(lcd.color565(15, 23, 42));
  
  lcd.fillRoundRect(10, 10, lcd.width() - 20, 45, 8, lcd.color565(30, 41, 59));
  lcd.drawRoundRect(10, 10, lcd.width() - 20, 45, 8, lcd.color565(59, 130, 246));
  lcd.setTextColor(TFT_WHITE);
  lcd.setTextSize(2);
  lcd.setTextDatum(MC_DATUM);
  lcd.drawString("Wireless Display", lcd.width() / 2, 32);

  lcd.fillRoundRect(15, 70, lcd.width() - 30, 120, 6, lcd.color565(30, 41, 59));
  lcd.drawRoundRect(15, 70, lcd.width() - 30, 120, 6, lcd.color565(16, 185, 129));

  lcd.setTextColor(lcd.color565(52, 211, 153));
  lcd.setTextSize(2);
  lcd.drawString("ONLINE / READY", lcd.width() / 2, 95);

  lcd.setTextColor(TFT_WHITE);
  lcd.setTextSize(1);
  lcd.drawString("Device IP Address (UDP 8888):", lcd.width() / 2, 125);

  lcd.setTextColor(TFT_YELLOW);
  lcd.setTextSize(2);
  lcd.drawString(ipAddr, lcd.width() / 2, 150);

  lcd.setTextColor(lcd.color565(148, 163, 184));
  lcd.setTextSize(1);
  lcd.drawString("Hold screen 3s to reset Wi-Fi", lcd.width() / 2, 215);
}

void drawErrorScreen(const char* errMsg) {
  lcd.fillScreen(lcd.color565(60, 10, 10));
  lcd.setTextColor(TFT_RED);
  lcd.setTextSize(2);
  lcd.setTextDatum(MC_DATUM);
  lcd.drawString("ERROR", lcd.width() / 2, 80);

  lcd.setTextColor(TFT_WHITE);
  lcd.setTextSize(1);
  lcd.drawString(errMsg, lcd.width() / 2, 130);
}
