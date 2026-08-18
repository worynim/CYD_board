/**
 * @file CYD_Wireless_Monitor.ino
 * @brief CYD(ESP32-2432S028) 무선 모니터 (LGFX_AUTODETECT 기반 안정화 원복 버전)
 * 
 * 1. LGFX_AUTODETECT를 통해 사용자의 CYD 보드 패널(ILI9341/ST7789)을 자동 인식하여 색상/방향 무결성 보장
 * 2. 주변 Wi-Fi AP 터치 목록 선택 및 비밀번호 가상 키보드 입력
 * 3. 원격 가로/세로 화면 회전 제어 및 FPS 오버레이 제어
 * 4. AsyncUDP 초저지연 프레임 수신
 */

#include <WiFi.h>
#include <AsyncUDP.h>
#include <Preferences.h>
#include <esp_wifi.h>
#define LGFX_AUTODETECT
#include <LovyanGFX.hpp>

// ==========================================
// 1. 객체 및 설정 정의
// ==========================================
static LGFX lcd;
AsyncUDP udp;
Preferences prefs;

const uint16_t UDP_PORT = 8888;

#define MAX_JPEG_SIZE 35000
#define PACKET_PAYLOAD_SIZE 1400

static uint8_t rxBuffer[MAX_JPEG_SIZE];
static uint8_t renderBuffer[MAX_JPEG_SIZE];
static size_t renderBufferSize = 0;
volatile bool isRendering = false;
volatile bool hasNewFrame = false;

static uint16_t expectedChunks = 0;
static uint16_t receivedChunks = 0;
static uint32_t latestFrameId = 0;
static uint8_t currentRotation = 3; // 기본 180도 가로
static bool showFpsOverlay = false;

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

void runTouchWifiSetup() {
  lcd.setRotation(3);
  
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
void onUdpPacketReceived(AsyncUDPPacket packet) {
  size_t len = packet.length();
  if (len < 8) return;

  const uint8_t* data = packet.data();

  uint32_t frameId = ((uint32_t)data[0] << 24) | ((uint32_t)data[1] << 16) | ((uint32_t)data[2] << 8) | (uint32_t)data[3];
  
  // 제어 명령 패킷
  if (frameId == 0xFFFFFFFF) {
    uint8_t reqRotation = data[4];
    if (reqRotation <= 7) {
      if (reqRotation != currentRotation) {
        currentRotation = reqRotation;
        lcd.setRotation(currentRotation);
      }
    }
    showFpsOverlay = (data[5] != 0);
    return;
  }

  uint16_t totalChunks = ((uint16_t)data[4] << 8) | (uint16_t)data[5];
  uint16_t chunkIdx = ((uint16_t)data[6] << 8) | (uint16_t)data[7];
  size_t payloadLen = len - 8;

  isReceiving = true;

  if (frameId < latestFrameId && (latestFrameId - frameId < 100000)) {
    return;
  }

  if (frameId != latestFrameId) {
    latestFrameId = frameId;
    expectedChunks = totalChunks;
    receivedChunks = 0;
  }

  size_t offset = (size_t)chunkIdx * PACKET_PAYLOAD_SIZE;
  if (offset + payloadLen <= MAX_JPEG_SIZE) {
    memcpy(rxBuffer + offset, data + 8, payloadLen);
    receivedChunks++;

    if (receivedChunks == expectedChunks && expectedChunks > 0) {
      if (!isRendering) {
        size_t totalBytes = ((expectedChunks - 1) * PACKET_PAYLOAD_SIZE) + payloadLen;
        if (totalBytes >= 4 && rxBuffer[0] == 0xFF && rxBuffer[1] == 0xD8) {
          memcpy(renderBuffer, rxBuffer, totalBytes);
          renderBufferSize = totalBytes;
          hasNewFrame = true;
        }
      }
    }
  }
}

// ==========================================
// 6. Loop 함수
// ==========================================
void loop() {
  if (hasNewFrame) {
    isRendering = true;
    hasNewFrame = false;

    int w = lcd.width();
    int h = lcd.height();

    lcd.drawJpg(renderBuffer, renderBufferSize, 0, 0, w, h);

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

  if (isReceiving) {
    unsigned long now = millis();
    if (now - lastFpsTime >= 1000) {
      currentFps = (frameCount * 1000.0f) / (now - lastFpsTime);
      frameCount = 0;
      lastFpsTime = now;
    }
  }

  static unsigned long touchStart = 0;
  uint16_t tx, ty;
  if (lcd.getTouch(&tx, &ty)) {
    if (touchStart == 0) touchStart = millis();
    if (millis() - touchStart > 3000) {
      touchStart = 0;
      runTouchWifiSetup();
      ESP.restart();
    }
  } else {
    touchStart = 0;
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
