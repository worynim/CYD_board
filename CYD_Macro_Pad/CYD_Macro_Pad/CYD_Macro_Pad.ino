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
 * 의존 라이브러리: LovyanGFX (JPEGDEC은 불필요)
 * 보드 설정: ESP32 Dev Module · Flash 4MB · Partition "Huge APP (3MB No OTA/1MB SPIFFS)"
 *
 * 와이어 프로토콜 (host_macro_pad/macro_pad_gui.py와 정확히 일치해야 함):
 *   - 설정 패킷 (호스트→디바이스): >IBBBB magic=0x4D434647("MCFG"), page, count, 0, 0
 *                                 + count개 엔트리(>BB + label bytes, 라벨 최대 24바이트)
 *   - 이벤트 패킷 (디바이스→호스트): >IBBBB magic=0x4D504144("MPAD"), page, button_id, 0, 0
 *   - 디스커버리 비콘 (디바이스→서브넷 브로드캐스트): >IBBBB magic=0x4D504245("MPBE"), 0, 0, 0, 0
 *     Wi-Fi 연결 후 3초 주기로 전송 — 호스트 리스너가 소스 IP를 보고 자동으로 설정을 푸시
 *   - 디바이스는 가장 최근 설정 패킷의 소스 IP/포트로 이벤트를 전송
 */

#include <WiFi.h>
#include <AsyncUDP.h>
#include <Preferences.h>
#include <esp_wifi.h>
#include <WiFiUdp.h>
#include <vector>
#include <LovyanGFX.hpp>

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
#define BEACON_INTERVAL_MS  3000         // 비콘 재전송 주기 (호스트가 IP 자동 검색)
#define PAGES            2
#define GRID_COLS        4
#define GRID_ROWS        3
#define BUTTONS_PER_PAGE (GRID_COLS * GRID_ROWS)  // 12
#define LABEL_MAX        24              // 라벨 최대 바이트 (호스트와 동일)

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
#define PREV_X          4
#define PREV_Y          214
#define PREV_W          60
#define PREV_H          22
#define NEXT_X          256
#define NEXT_Y          214
#define NEXT_W          60
#define NEXT_H          22

#define LONG_PRESS_MS   2500             // 상태바 길게 → Wi-Fi 재설정
#define MAX_TAP_MS      800              // 이보다 길게 누른 탭은 무시

// ==========================================
// 4. 전역 상태
// ==========================================
AsyncUDP udp;                  // 설정 수신 (listen 전용)
WiFiUDP  udpSend;              // 이벤트 전송 (AsyncUDP connect()의 단일연결/수신필터 문제 회피)
Preferences prefs;

String stored_ssid = "";
String stored_pass = "";

// 버튼 라벨 저장소 (호스트가 설정 패킷으로 채움)
char labels[PAGES][BUTTONS_PER_PAGE][LABEL_MAX + 1] = {};
volatile bool labelsDirty = false;   // AsyncUDP 콜백에서 세우고 loop()에서 소비
uint8_t currentPage = 0;

// 호스트 주소 (가장 최근 설정 패킷의 소스로 학습)
IPAddress hostIP;
uint16_t hostPort = 0;
bool hostKnown = false;

// [STAT] 계측
static uint32_t statEvents = 0;    // 전송한 이벤트 수
static uint32_t statConfigs = 0;   // 수신한 설정 패킷 수
static uint32_t statBeacons = 0;   // 보낸 디스커버리 비콘 수

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
void drawStatusBar();
void handleTouch();
bool hitButton(uint16_t tx, uint16_t ty, int* col, int* row);

// ==========================================
// 6. Setup 함수
// ==========================================
void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n--- CYD Wireless Macro Pad ---");

  lcd.init();
  lcd.setRotation(3);   // 가로 정방향 (320x240)
  lcd.setBrightness(200);

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
  if (magic != MAGIC_CONFIG) return;

  // 호스트 주소 학습: 가장 최근 설정 패킷의 소스로 이벤트 전송 대상을 갱신
  hostIP = packet.remoteIP();
  hostPort = packet.remotePort();
  hostKnown = true;

  uint8_t page = d[4];
  uint8_t count = d[5];
  if (page >= PAGES) page = PAGES - 1;
  if (count > BUTTONS_PER_PAGE) count = BUTTONS_PER_PAGE;

  bool changed = false;
  // 엔트리는 가변 길이(>BB + label bytes) → 고정 오프셋이 아니라 순차 탐색해야 한다.
  size_t off = 8;
  for (uint8_t i = 0; i < count; i++) {
    if (off + 2 > len) break;
    uint8_t bid = d[off];
    uint8_t llen = d[off + 1];
    off += 2;
    if (bid >= BUTTONS_PER_PAGE) continue;
    if (off + llen > len) llen = len - off;
    if (llen > LABEL_MAX) llen = LABEL_MAX;

    char tmp[LABEL_MAX + 1];
    memcpy(tmp, d + off, llen);
    tmp[llen] = '\0';

    if (strcmp(tmp, labels[page][bid]) != 0) {
      strncpy(labels[page][bid], tmp, LABEL_MAX + 1);
      changed = true;
    }
    off += llen;
  }

  statConfigs++;
  if (changed) {
    labelsDirty = true;   // loop()가 그리드 재렌더
    String hip = hostIP.toString();
    Serial.printf("[CFG] page=%u count=%u host=%s:%u\n", page, count, hip.c_str(), hostPort);
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

  uint16_t fill = pressed ? lcd.color565(37, 99, 235) : lcd.color565(51, 65, 85);
  uint16_t border = pressed ? TFT_WHITE : lcd.color565(100, 116, 139);
  lcd.fillRoundRect(x, y, BTN_W, BTN_H, 6, fill);
  lcd.drawRoundRect(x, y, BTN_W, BTN_H, 6, border);

  lcd.setTextColor(TFT_WHITE);
  lcd.setTextSize(1);
  lcd.setTextDatum(MC_DATUM);
  lcd.drawString(labels[page][idx], x + BTN_W / 2, y + BTN_H / 2);
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
                    currentPage < PAGES - 1 ? lcd.color565(37, 99, 235) : lcd.color565(71, 85, 105));
  lcd.drawString(">", NEXT_X + NEXT_W / 2, NEXT_Y + NEXT_H / 2);

  // 중앙: 페이지 인디케이터 + IP
  String ipStr = (WiFi.status() == WL_CONNECTED) ? WiFi.localIP().toString() : "NO WIFI";
  char buf[40];
  snprintf(buf, sizeof(buf), "PAGE %d/%d  %s", currentPage + 1, PAGES, ipStr.c_str());
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
      // 상태바: 페이지 이동
      if (pressX >= PREV_X && pressX < PREV_X + PREV_W && currentPage > 0) {
        currentPage--;
        drawGrid(currentPage);
      } else if (pressX >= NEXT_X && pressX < NEXT_X + NEXT_W && currentPage < PAGES - 1) {
        currentPage++;
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
void loop() {
  // 호스트 설정 도착 시 그리드 재렌더 (플리커 방지: 변경 시에만)
  if (labelsDirty) {
    labelsDirty = false;
    drawGrid(currentPage);
  }

  handleTouch();

  unsigned long now = millis();

  // 디스커버리 비콘: 호스트가 IP를 자동 검색하도록 3초 주기 브로드캐스트
  static unsigned long lastBeaconTime = 0;
  if (now - lastBeaconTime >= BEACON_INTERVAL_MS) {
    sendBeacon();
    lastBeaconTime = now;
  }

  // [STAT] 초당 1회 계측
  static unsigned long lastStatTime = 0;
  if (now - lastStatTime >= 1000) {
    String hip = hostKnown ? hostIP.toString() : String("-");
    Serial.printf("[STAT] page=%u host=%s:%u evt=%u cfg=%u bcn=%u heap=%lukB wifi=%d\n",
                  currentPage, hip.c_str(), hostPort,
                  statEvents, statConfigs, statBeacons,
                  (unsigned long)(ESP.getFreeHeap() / 1024), WiFi.status());
    statEvents = 0;
    statConfigs = 0;
    statBeacons = 0;
    lastStatTime = now;
  }

  if (WiFi.status() != WL_CONNECTED) {
    delay(100);
  } else {
    yield();
  }
}
