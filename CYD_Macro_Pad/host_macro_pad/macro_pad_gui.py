#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CYD 무선 매크로 패드 호스트 (CYD Macro Pad Host)

CYD(ESP32-2432S028, 2.8" 터치 LCD)의 버튼 그리드를 터치하면 디바이스가 UDP 이벤트를
보내고, 이 호스트가 이를 수신해 액션을 실행한다.
  - shortcut : 키보드 단축키 (예: cmd+shift+4, win+r)   → 격리된 헬퍼(_input_helper.py)
  - text     : 문구/텍스트 입력 (한글 포함, IME 무관)     → 격리된 헬퍼(클립보드 + 붙여넣기)
  - app      : 앱 실행 또는 URL 열기                     → macOS open / 윈도우 os.startfile

실행:
    pip install -r requirements.txt
    python3 macro_pad_gui.py

플랫폼별 키보드 입력 권한:
    macOS: 시스템설정 → 개인정보 보호 및 보안 → 손쉬운 사용 → 터미널(또는 Python) 체크.
           권한이 없으면 키보드/문구 액션은 조용히 무시된다.
    Windows: 별도 권한 불필요 (pynput이 SendInput 사용).

크래시 격리 (중요):
    pynput(네이티브 입력: macOS Quartz / Windows SendInput)은 네이티브 코드라 드물게
    프로세스를 통째로 죽일 수 있다.
    그래서 키보드 입력은 반드시 _input_helper.py 서브프로세스에서 실행한다 —
    헬퍼가 세그폴트로 죽어도 GUI는 살아남고 이벤트 로그에 실패만 남긴다.
    GUI 프로세스 자체에는 pynput이 전혀 import되지 않는다.

스레딩 모델 (streamer_gui.py 관례 계승):
    - 메인(Tkinter) 스레드: 모든 위젯 소유, 설정 편집, Apply.
    - 리스너 daemon 스레드: UDP 소켓 수명 전체를 소유 (bind→수신→finally: close).
      [FIX #1] 메인 스레드는 절대 리스너 소켓을 닫지 않는다.
      [FIX #3] UI 갱신은 _event_queue + root.after 폴링, try/except tk.TclError.
"""

import base64
import importlib.util
import io
import json
import logging
import os
import queue
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# ==========================================
# 크로스파일 계약 (CYD_Macro_Pad/CYD_Macro_Pad.ino와 정확히 일치해야 함)
# ==========================================
UDP_PORT = 8890              # 매크로 패드 전용 포트 (스트리밍 8888과 독립)
MAGIC_CONFIG = 0x4D434647    # "MCFG" 호스트→디바이스 설정 패킷
MAGIC_EVENT = 0x4D504144     # "MPAD" 디바이스→호스트 이벤트 패킷
MAGIC_BEACON = 0x4D504245    # "MPBE" 디바이스→브로드캐스트 디스커버리 비콘
MAGIC_OK = 0x4D504F4B        # "MPOK" 액션 성공 피드백 → 디바이스 (B)
MAGIC_ERR = 0x4D504552       # "MPER" 액션 실패 피드백 → 디바이스 (B)
MAX_PAGES = 8                # 최대 페이지 수 (펌웨어와 일치)
DEFAULT_PAGES = 2            # 최초 페이지 수
GRID_COLS = 4
GRID_ROWS = 3
BUTTONS_PER_PAGE = GRID_COLS * GRID_ROWS   # 12
LABEL_MAX = 24               # 라벨 최대 바이트 (UTF-8)
PAGE_NAME_MAX = 20           # 페이지 이름 최대 바이트 (UTF-8) — 펌웨어와 일치 (A)

# 버튼 색상 팔레트 (인덱스 → 펌웨어 BTN_PALETTE와 정확히 일치해야 함)
# 색상은 index로 전송되므로 표시 이름만 바꿔도 펌웨어/저장 설정과 호환된다.
# 표시명은 언어별(dropdown) — index가 와이어 계약이므로 펌웨어와는 항상 index로만 통신.
COLOR_COUNT = 10
COLOR_NAMES = {
    "ko": ["회색", "빨강", "주황", "노랑", "초록", "청록", "파랑", "보라", "분홍", "흰색"],
    "en": ["Gray", "Red", "Orange", "Yellow", "Green", "Teal", "Blue", "Purple", "Pink", "White"],
}
# 표시명(어느 언어든) → index. ATYPE_FROM_LABEL처럼 언어 전환 중 낡은 라벨도 역조회된다.
COLOR_NAME_TO_IDX = {}
for _lang_names in COLOR_NAMES.values():
    for _ci, _cn in enumerate(_lang_names):
        COLOR_NAME_TO_IDX.setdefault(_cn, _ci)
COLOR_HEX = ["#64748B", "#EF4444", "#F97316", "#EAB308", "#22C55E",
             "#14B8A6", "#3B82F6", "#A855F7", "#EC4899", "#F8FAFC"]

# 양쪽 모두 8바이트 헤더: magic u32 + 필드 4 × u8
CONFIG_HEADER = struct.Struct(">IBBBB")    # magic, page, count, num_pages, rsvd
EVENT_HEADER = struct.Struct(">IBBBB")     # magic, page, button, flags, rsvd
IMAGE_HEADER = struct.Struct(">IBBBB")     # magic, page, button, format, rsvd

# [G] 버튼 이름 이미지: 펌웨어 BTN_W/BTN_H와 일치, JPEG는 단일 UDP 패킷(≤1400B)로 전송
MAGIC_IMAGE = 0x4D494D47      # "MIMG" 호스트→디바이스 버튼 이름 이미지 (G)
BTN_IMG_W = 71                # 펌웨어 BTN_W와 일치
BTN_IMG_H = 61                # 펌웨어 BTN_H와 일치
JPEG_QUALITY = 90             # 기본 품질 — 4:4:4(subsampling=0)와 조합해 크로마 얼룩 제거
JPEG_MIN_QUALITY = 70         # 품질 하한 — 1400B 예산과 무관하게 이 품질을 보장 (MIMG fmt=2 청킹 전송)
JPEG_MAX_BYTES = 1400         # fmt=0 단일 패킷 이미지 페이로드 상한 (헤더 8B + 1400B = 1408 < UDP MTU 1472)
IMG_CHUNK_DATA = 1400         # fmt=2 청크당 데이터 바이트 (8+8+1400 = 1416 < UDP MTU 1472 — 단편화 없음)
IMG_MAX_BYTES = 4096          # 청킹 이미지 총 상한 (펌웨어 IMG_MAX_BYTES와 일치; 71×61 최악 노이즈 q=70 ≈ 2766B)
GRID_BG_HEX = "#0F172A"       # 버튼 주변 그리드 배경색 (이미지 모서리와 동일 → 이음새 제거)
BTN_BORDER_HEX = "#64748B"    # 비활성 버튼 테두리 (슬레이트 — 펌웨어 색상과 일치)
# [PLAN 7] 버튼 모서리 라운드 반경. radius 6은 JPEG 4:2:0 손실 압축 후 코너가 거의
#     채움색으로 번져 디바이스에서 각진 버튼으로 보인다(그리고 텍스트 버튼도 Chamfer처럼
#     보임). 10은 압축 후에도 확실한 라운드가 남는다. 펌웨어 BTN_RADIUS와 일치해야 한다.
BTN_RADIUS = 10

ACTION_TYPES = ["shortcut", "text", "app"]
# 드롭다운 표시 라벨(언어별) ↔ 내부 값(canonical, 와이어 프로토콜 그대로).
# ATYPE_FROM_LABEL은 한/영 라벨을 모두 내부값에 매핑해 어떤 언어로 표시돼도 역조회된다.
ATYPE_LABELS = {
    "ko": {"shortcut": "단축키", "text": "문구", "app": "앱 / URL"},
    "en": {"shortcut": "Shortcut", "text": "Text", "app": "App / URL"},
}
ATYPE_FROM_LABEL = {}
for _lang_map in ATYPE_LABELS.values():
    for _v, _lbl in _lang_map.items():
        ATYPE_FROM_LABEL.setdefault(_lbl, _v)

# [H] 프로토콜 v3: 액션/덤프/ACK (펌웨어와 정확히 일치해야 함)
MAGIC_REQUEST = 0x4D524551    # "MREQ" 설정 덤프 요청 (호스트→디바이스)
ACTION_VAL_MAX = 128          # 액션 값(action_value) 최대 바이트 — 펌웨어 ACTION_VAL_MAX와 일치
ATYPE_TO_IDX = {"shortcut": 0, "text": 1, "app": 2}   # v3 MCFG 엔트리 action_type
ATYPE_FROM_IDX = ["shortcut", "text", "app"]
REQUEST_HEADER = struct.Struct(">IBBBB")     # MREQ: magic + 4×u8
CHUNK_MAX = 1200              # MCFG 청크 상한 — 펌웨어 MCFG_CHUNK_MAX와 일치 (UDP 단일 패킷 안전)

# ------------------------------------------------------------------
# [PLAN] 호스트 UI 한/영 번역. key → {ko, en}. 위젯/로그/도움말/메시지박스는
#     self._t(key) / self._tf(key, *args)로 조회 (누락 시 ko). 사용자 라벨(버튼 이름,
#     페이지 이름)은 데이터라 번역 대상이 아니다. %-포맷은 _tf에서 적용한다.
# ------------------------------------------------------------------
L10N = {
    # 헤더
    "subtitle": {
        "ko": "ESP32-2432S028 터치 버튼 → UDP 이벤트 → 매크로 실행",
        "en": "ESP32-2432S028 touch buttons → UDP events → macros",
    },
    # 언어 메뉴/버튼
    "lang_btn": {"ko": "🌐 한국어", "en": "🌐 English"},
    "log_lang": {"ko": "언어 설정: %s", "en": "Language set to: %s"},
    # 상태바
    "status_starting": {"ko": "● 리스너 시작 중...", "en": "● Listener starting..."},
    "status_running": {"ko": "● 리스너 동작 중 · CYD 자동 검색 대기...",
                       "en": "● Listener running · waiting for CYD..."},
    "status_stopped": {"ko": "● 리스너 중지됨", "en": "● Listener stopped"},
    "status_found": {"ko": "● 리스너 동작 중 · 디바이스 발견: %s:%d",
                     "en": "● Listener running · device found: %s:%d"},
    # 페이지 행
    "label_page_name": {"ko": "페이지 이름:", "en": "Page name:"},
    "btn_add_page": {"ko": "+ 페이지", "en": "+ Page"},
    "btn_del_page": {"ko": "− 페이지", "en": "− Page"},
    # 하단 버튼
    "btn_apply": {"ko": "💾 설정 적용 (Apply)", "en": "💾 Apply Settings"},
    "btn_export": {"ko": "⬇ 내보내기", "en": "⬇ Export"},
    "btn_import": {"ko": "⬆ 가져오기", "en": "⬆ Import"},
    "btn_device_import": {"ko": "🖥 디바이스에서 불러오기", "en": "🖥 Load from Device"},
    # 버튼 카드
    "ph_name": {"ko": "이름", "en": "Name"},
    "ph_action": {"ko": "액션", "en": "Action"},
    "preview_label": {"ko": "라벨/색상", "en": "Label/Color"},
    "hint_shortcut": {"ko": "단축키 (예: cmd+shift+4, win+r)", "en": "Shortcut (e.g. cmd+shift+4, win+r)"},
    "hint_text": {"ko": "문구: 한글 가능", "en": "Text: any characters"},
    "hint_app": {"ko": "영문 앱명(예: Calculator) or URL",
                 "en": "App name (e.g. Calculator) or URL"},
    # 파일 다이얼로그
    "fd_image_files": {"ko": "이미지 파일", "en": "Image files"},
    "fd_all_files": {"ko": "모든 파일", "en": "All files"},
    "fd_json_files": {"ko": "JSON 파일", "en": "JSON files"},
    # 로그
    "log_ready": {"ko": "준비됨. CYD에 설정을 적용하려면 '설정 적용'을 누르세요.",
                  "en": "Ready. Press 'Apply Settings' to send config to the CYD."},
    "log_img_convert_fail": {"ko": "⚠️ 이미지 변환 실패: %s", "en": "⚠️ Image conversion failed: %s"},
    "log_img_upload": {"ko": "🖼 이미지 업로드: page%d · #%d (%s) — 적용하려면 [설정 적용]",
                       "en": "🖼 Image uploaded: page%d · #%d (%s) — press [Apply Settings] to send"},
    "log_img_remove": {"ko": "🗑 이미지 제거: page%d · #%d — 적용하려면 [설정 적용]",
                       "en": "🗑 Image removed: page%d · #%d — press [Apply Settings] to send"},
    "log_page_max": {"ko": "⚠️ 페이지는 최대 %d개까지 추가할 수 있습니다",
                     "en": "⚠️ A maximum of %d pages is allowed"},
    "log_page_add": {"ko": "+ 페이지 %d 추가", "en": "+ Page %d added"},
    "log_page_del": {"ko": "− 페이지 %d 삭제", "en": "− Page %d deleted"},
    "log_page_reorder": {"ko": "↕ 페이지 순서 변경 — 디바이스 동기화 요청",
                         "en": "↕ Page order changed — device sync requested"},
    "log_export": {"ko": "⬇ 설정 내보내기: %s (%d페이지)",
                   "en": "⬇ Config exported: %s (%d pages)"},
    "log_export_fail": {"ko": "⚠️ 내보내기 실패: %s", "en": "⚠️ Export failed: %s"},
    "log_import": {"ko": "⬆ 설정 가져오기: %s (%d페이지) — 적용하려면 [설정 적용]",
                   "en": "⬆ Config imported: %s (%d pages) — press [Apply Settings] to send"},
    "log_device_import_req": {"ko": "🖥 디바이스에서 불러오기 요청 (%s) — 덤프 수신 대기",
                              "en": "🖥 Load-from-device requested (%s) — awaiting dump"},
    "log_device_import_done": {"ko": "🖥 디바이스에서 불러오기 완료: %d페이지, 이미지 %d개 수신 — Apply로 전송하세요",
                               "en": "🖥 Loaded from device: %d pages, %d images — press Apply to send"},
    "log_device_import_noimg": {
        "ko": "⚠️ 이미지가 수신되지 않았습니다 — 디바이스 플래시(/btns)에 저장된 이미지가 "
              "없거나 전송이 유실됐습니다. (직렬 로그 [IMG] flash=1 / [MREQ] dump 확인)",
        "en": "⚠️ No images received — the device flash (/btns) has none stored or the "
              "transfer was lost. (check serial [IMG] flash=1 / [MREQ] dump)"},
    "log_apply_resend": {"ko": "💾 설정 저장 + 재전송 요청 (%d페이지)",
                         "en": "💾 Settings saved + resend requested (%d pages)"},
    "log_apply_sent": {"ko": "💾 설정 저장 + 전송 (%s)",
                       "en": "💾 Settings saved + sent (%s)"},
    "log_apply_wait": {"ko": "💾 설정 저장 — 디바이스 발견 시 자동 전송됩니다",
                       "en": "💾 Settings saved — will auto-send once the device is found"},
    "log_push_fail": {"ko": "⚠️ 설정 전송 실패: %s", "en": "⚠️ Config send failed: %s"},
    "log_push": {"ko": "📤 설정+이미지 푸시 (%s) → %s:%d",
                 "en": "📤 Config+images pushed (%s) → %s:%d"},
    "log_render_fail": {"ko": "⚠️ 이미지 렌더 실패 page%d·#%d: %s",
                        "en": "⚠️ Image render failed page%d·#%d: %s"},
    "log_images_sent": {"ko": "🖼 버튼 이미지 %d개 렌더+전송",
                        "en": "🖼 %d button images rendered + sent"},
    "log_pynput_missing": {"ko": "⚠️ pynput 미설치 — 단축키/문구 액션이 동작하지 않습니다 "
                                 "(pip install -r requirements.txt)",
                           "en": "⚠️ pynput not installed — shortcut/text actions won't work "
                                 "(pip install -r requirements.txt)"},
    "log_listener_start": {"ko": "▶ 리스너 자동 시작: UDP %d (디바이스 자동 검색)",
                           "en": "▶ Listener auto-started: UDP %d (auto-discovery)"},
    "log_listener_stop": {"ko": "■ 리스너 중지", "en": "■ Listener stopped"},
    "log_bind_fail": {"ko": "⚠️ UDP %d 바인드 실패: %s", "en": "⚠️ UDP %d bind failed: %s"},
    "log_dump_fail": {"ko": "디바이스 응답이 불완전합니다 (일부 페이지/버튼 누락). 다시 시도하세요.",
                      "en": "Incomplete device response (some pages/buttons missing). Try again."},
    "log_event_ok": {"ko": "✓ page%d · #%d: %s 실행됨", "en": "✓ page%d · #%d: %s executed"},
    "log_event_err": {"ko": "✗ page%d · #%d: %s", "en": "✗ page%d · #%d: %s"},
    "log_device_found": {"ko": "디바이스 발견 (자동 검색): %s — 설정 비교",
                         "en": "Device found (auto-discovery): %s — comparing settings"},
    # 자동 동기화 보호 (첫 연결 시 디바이스 덮어쓰기 방지)
    "log_autosync_same": {"ko": "디바이스(%s) 설정이 호스트와 동일합니다.",
                          "en": "Device (%s) settings match the host."},
    "log_autosync_loaded": {"ko": "디바이스(%s)에서 설정을 불러왔습니다.",
                            "en": "Loaded settings from device (%s)."},
    "log_autosync_none": {"ko": "디바이스(%s)는 건드리지 않았습니다. 호스트 설정을 유지합니다.",
                          "en": "Device (%s) left untouched; keeping host settings."},
    "log_autosync_fail": {"ko": "디바이스(%s) 설정을 읽지 못해 자동 비교를 건너뜁니다 (다음 비콘에서 재시도).",
                          "en": "Could not read device (%s) settings; auto-sync skipped (retries on next beacon)."},
    # 메시지박스
    "msg_delpage_title": {"ko": "페이지 삭제", "en": "Delete Page"},
    "msg_min1page": {"ko": "최소 1개 페이지는 필요합니다.", "en": "At least 1 page is required."},
    "msg_delpage_confirm": {"ko": "페이지 %d의 설정이 삭제됩니다. 계속할까요?",
                            "en": "Page %d settings will be deleted. Continue?"},
    "msg_import_fail_title": {"ko": "가져오기 실패", "en": "Import Failed"},
    "msg_import_fail": {"ko": "설정 파일을 읽지 못했습니다:\n%s",
                        "en": "Could not read the config file:\n%s"},
    "msg_devimport_title": {"ko": "디바이스에서 불러오기", "en": "Load from Device"},
    "msg_noip": {"ko": "디바이스 IP를 알 수 없습니다.\n비콘으로 자동 검색될 때까지 기다린 후 다시 시도하세요.",
                 "en": "Device IP is unknown.\nWait until auto-discovery finds it, then retry."},
    "msg_listener_down": {"ko": "리스너가 동작하지 않습니다.", "en": "The listener is not running."},
    "msg_dumpfail_title": {"ko": "디바이스에서 불러오기 실패", "en": "Load from Device Failed"},
    "msg_autosync_title": {"ko": "설정 동기화 확인", "en": "Sync Settings"},
    "msg_autosync_ask": {"ko": "디바이스(%s)의 설정이 호스트와 다릅니다.\n"
                                 "디바이스에서 불러오시겠습니까?\n\n"
                                 "[예] 디바이스 설정을 호스트로 불러옴\n"
                                 "[아니오] 아무것도 하지 않음 (디바이스 유지)",
                         "en": "Device (%s) settings differ from the host.\n"
                                 "Load from the device?\n\n"
                                 "[Yes] Load device settings into the host\n"
                                 "[No] Do nothing (keep the device as-is)"},
    # 액션 설명
    "desc_text": {"ko": "문구 \"%s\"", "en": "text \"%s\""},
    "desc_app": {"ko": "app / URL %s", "en": "app / URL %s"},
    "desc_shortcut": {"ko": "단축키 %s", "en": "shortcut %s"},
    # 오류
    "err_timeout": {"ko": "입력 실행 시간 초과 (30s)", "en": "Input timed out (30s)"},
    "err_helper_os": {"ko": "입력 헬퍼 실행 오류: %s", "en": "Input helper error: %s"},
    "err_helper_signal": {"ko": "입력 헬퍼가 시그널 %d로 종료됨 (pynput 크래시?)",
                          "en": "Input helper exited with signal %d (pynput crash?)"},
    "err_input_fail": {"ko": "입력 실행 실패: %s", "en": "Input failed: %s"},
    "err_unknown": {"ko": "알 수 없는 오류", "en": "unknown error"},
    "err_pynput_missing": {"ko": "pynput 미설치 — pip install -r requirements.txt",
                           "en": "pynput not installed — pip install -r requirements.txt"},
    "err_pillow": {"ko": "Pillow 미설치 — 버튼 이미지 기능에 필요합니다 (pip install -r requirements.txt)",
                   "en": "Pillow not installed — needed for button images (pip install -r requirements.txt)"},
    "err_app_timeout": {"ko": "앱 실행 시간 초과 (10s)", "en": "App launch timed out (10s)"},
    "err_app_os": {"ko": "앱 실행 오류: %s", "en": "App launch error: %s"},
    "err_app_fail": {"ko": "앱 실행 실패: %s", "en": "App launch failed: %s"},
    "err_app_open": {"ko": "open이 실패했습니다", "en": "open failed"},
    # 손쉬운 사용(Accessibility) 경고 — 재빌드 때 권한이 초기화되는 PyInstaller 앱의 고질병.
    # 없으면 pynput 키 이벤트가 예외 없이 버려져 단축키/텍스트가 조용히 동작하지 않는다.
    "warn_accessibility": {
        "ko": "⚠ 손쉬운 사용(Accessibility) 권한이 없어 단축키/텍스트 입력이 동작하지 않을 수 "
              "있습니다.\n시스템 설정 → 개인정보 보호 및 보안 → 손쉬운 사용 에서 "
              "'CYD Macro Pad'를 추가한 뒤 앱을 다시 실행하세요.",
        "en": "⚠ Missing Accessibility permission — shortcuts/text input may not work.\n"
              "Add 'CYD Macro Pad' in System Settings → Privacy & Security → "
              "Accessibility, then relaunch the app.",
    },
    # 도움말
    "help_title": {"ko": "프로그램 사용법", "en": "How to Use"},
    "help_close": {"ko": "닫기", "en": "Close"},
    "help_h": {"ko": "⌨️ CYD 무선 매크로 패드 사용법\n",
               "en": "⌨️ CYD Wireless Macro Pad Guide\n"},
    "help_start_sub": {"ko": "\n[ 시작하기 ]\n", "en": "\n[ Getting Started ]\n"},
    "help_start_1": {"ko": "1. CYD 전원 → 같은 Wi-Fi에 자동 연결 → 하단 로그에 "
                           "\"디바이스 발견\"이 뜹니다. IP 입력은 필요 없습니다 (자동 발견, UDP 8890).\n",
                     "en": "1. Power the CYD → it auto-connects to the same Wi-Fi → "
                           "\"device found\" appears in the log below. No IP entry needed (auto-discovery, UDP 8890).\n"},
    "help_start_2": {"ko": "2. 페이지 탭(최대 8개)에서 4×3 버튼 12개를 설정한 뒤 [설정 적용]을 누르세요.\n",
                     "en": "2. Configure the 4×3 grid (12 buttons) on a page tab (up to 8), then press [Apply Settings].\n"},
    "help_start_3": {"ko": "3. 장치에서 버튼을 터치하면 호스트가 등록된 동작을 실행합니다.\n",
                     "en": "3. Touch a button on the device and the host runs the registered action.\n"},
    "help_start_4": {"ko": "4. 페이지 탭을 마우스로 끌어 순서를 바꿀 수 있습니다. 순서 변경은 "
                           "놓는 즉시 자동으로 디바이스에도 반영됩니다.\n",
                     "en": "4. Drag page tabs with the mouse to reorder. The new order is "
                           "synced to the device immediately on drop.\n"},
    "help_act_sub": {"ko": "\n[ 버튼 동작 3종 ]\n", "en": "\n[ 3 Action Types ]\n"},
    "help_act_shortcut": {"ko": "▪ 단축키 (shortcut) — 키 조합 입력. 예: ",
                          "en": "▪ Shortcut — key combination input. e.g. "},
    "help_act_text": {"ko": "▪ 문구 (text) — 입력할 문자열. 예: ",
                      "en": "▪ Text — the string to type. e.g. "},
    "help_act_text_ime": {"ko": " (한글 포함 가능).\n", "en": " (Korean included).\n"},
    "help_act_text_ime_sub": {"ko": "    클립보드 + 붙여넣기(맥 Cmd+V / 윈도우 Ctrl+V) 방식이라 한/영 입력기(IME)와 무관하게 동작합니다.\n",
                              "en": "    Uses clipboard + paste (Cmd+V on macOS / Ctrl+V on Windows), so it works regardless of the IME.\n"},
    "help_act_app": {"ko": "▪ app / URL — \"", "en": "▪ App / URL — \""},
    "help_act_app_ex": {"ko": "    - 앱 예: Safari, Calculator, Notes   (Finder에 보이는 영문 이름)\n",
                        "en": "    - App e.g. Safari, Calculator, Notes   (English name as in Finder)\n"},
    "help_act_url_ex": {"ko": "    - URL 예: https://www.google.com , https://www.youtube.com\n",
                        "en": "    - URL e.g. https://www.google.com , https://www.youtube.com\n"},
    "help_keys_sub": {"ko": "\n[ 단축키에 쓸 수 있는 특수키 ]\n",
                      "en": "\n[ Special keys usable in shortcuts ]\n"},
    "help_keys_mod": {"ko": "  cmd(⌘)/command, win(⊞)/windows, ctrl(⌃)/control, alt(⌥)/option, shift(⇧)\n",
                      "en": "  cmd(⌘)/command, win(⊞)/windows, ctrl(⌃)/control, alt(⌥)/option, shift(⇧)\n"},
    "help_keys_nav": {"ko": "  space, enter/return, tab, esc/escape\n",
                      "en": "  space, enter/return, tab, esc/escape\n"},
    "help_keys_etc": {"ko": "  up/down/left/right, backspace, delete, caps_lock\n",
                      "en": "  up/down/left/right, backspace, delete, caps_lock\n"},
    "help_keys_fn": {"ko": "  home, end, page_up/page_down, fn, insert, print_screen\n",
                     "en": "  home, end, page_up/page_down, fn, insert, print_screen\n"},
    "help_keys_f": {"ko": "  f1 ~ f20 (기능키, 예: f5, cmd+shift+f3)\n",
                    "en": "  f1 ~ f20 (function keys, e.g. f5, cmd+shift+f3)\n"},
    "help_keys_plus": {"ko": "- 조합은 ", "en": "- Combine with "},
    "help_keys_with": {"ko": " 로 연결: ", "en": " to join: "},
    "help_keys_more": {"ko": "  등.\n", "en": "  etc.\n"},
    "help_keys_single": {"ko": "- 영문·숫자 한 글자는 그대로: ",
                         "en": "- A single letter/digit works as-is: "},
    "help_keys_shiftnote": {"ko": "  (대소문자 무시).\n", "en": "  (case-insensitive).\n"},
    "help_special_sub": {"ko": "\n[ 단축키 특수문자 — shift+X 형태 ]\n",
                         "en": "\n[ Shortcut special characters — shift+X form ]\n"},
    "help_special_expl": {"ko": "shortcut 동작에서 ! @ # $ % ^ & * ( ) < > : + 같은 특수문자는 ",
                          "en": "In a shortcut action, special chars like ! @ # $ % ^ & * ( ) < > : + are "},
    "help_special_sep": {"ko": "가 조합 구분자라 직접 못 쓰고, shift를 함께 써야 합니다:\n",
                         "en": "is the join separator, so it can't be typed directly — use shift instead:\n"},
    "help_special_ex_descr": {"ko": " → cmd + ＋(플러스) 키\n",
                              "en": " → cmd + plus key\n"},
    "help_special_textnote2": {"ko": " 동작은 shift+X 없이 특수문자를 그대로 입력합니다 (예: ",
                               "en": " actions type special chars directly without shift+X (e.g. "},
    "help_special_map": {"ko": "  ! = shift+1    @ = shift+2    # = shift+3    $ = shift+4    % = shift+5\n"
                               "  ^ = shift+6    & = shift+7    * = shift+8    ( = shift+9    ) = shift+0\n"
                               "  < = shift+,    > = shift+.    ? = shift+/    : = shift+;    + = shift+=\n",
                         "en": "  ! = shift+1    @ = shift+2    # = shift+3    $ = shift+4    % = shift+5\n"
                               "  ^ = shift+6    & = shift+7    * = shift+8    ( = shift+9    ) = shift+0\n"
                               "  < = shift+,    > = shift+.    ? = shift+/    : = shift+;    + = shift+=\n"},
    "help_special_ex": {"ko": "  예: ", "en": "  e.g. "},
    "help_special_textnote": {"ko": "  참고: ", "en": "  Note: "},
    "help_emoji_sub": {"ko": "\n[ 이모지 ]\n", "en": "\n[ Emoji ]\n"},
    "help_emoji_expl": {"ko": "버튼 이름·액션 값에 이모지(예: ",
                        "en": "Put an emoji in a button name or action value (e.g. "},
    "help_emoji_tail": {"ko": ")를 넣으면 이미지로 렌더링되어 장치에 표시됩니다.\n",
                        "en": ") and it renders as an image on the device.\n"},
    "help_ex_shortcut": {"ko": "cmd+shift+4, cmd+c, win+r\n", "en": "cmd+shift+4, cmd+c, win+r\n"},
    "help_ex_text": {"ko": "안녕하세요", "en": "Hello"},
    "help_ex_emoji": {"ko": "📷 촬영", "en": "📷 Camera"},
    "help_act_url_open": {"ko": "\"가 들어가면 URL로 열고, 아니면 macOS 앱 이름으로 실행합니다.\n",
                          "en": "\" opens it as a URL; otherwise it runs the macOS app name.\n"},
    "help_perm_warn": {"ko": "\n[ 필수 macOS 권한 ]\n", "en": "\n[ Required macOS Permission ]\n"},
    "help_perm_expl": {"ko": "키보드 입력(단축키·문구)은 시스템 설정 → 개인정보 보호 및 보안 → "
                             "손쉬운 사용에서\n터미널/호스트 프로그램에 권한을 줘야 동작합니다. "
                             "없으면 조용히 무시됩니다.\n",
                       "en": "Keyboard input (shortcut/text) needs permission in System Settings → "
                             "Privacy & Security → Accessibility\nfor the terminal/host program. "
                             "Without it, actions are silently ignored.\n"},
    "help_backup_sub": {"ko": "\n[ 백업 / 복원 ]\n", "en": "\n[ Backup / Restore ]\n"},
    "help_backup_export": {"ko": "▪ 내보내기: 현재 설정을 JSON 파일로 저장\n",
                           "en": "▪ Export: save the current settings to a JSON file\n"},
    "help_backup_import": {"ko": "▪ 가져오기: JSON 파일 불러오기\n",
                           "en": "▪ Import: load a JSON file\n"},
    "help_backup_device": {"ko": "▪ 디바이스에서 불러오기: 장치 내부 저장 설정을 읽어 편집 화면에 채움 "
                                 "([설정 적용]으로 동기화).\n",
                           "en": "▪ Load from device: read the device's stored settings into the "
                                 "editor (sync with [Apply Settings]).\n"},
}

# 현재 언어 (모듈 전역). GUI __init__/언어 전환에서 갱신 — 단순 str 참조라 CPython GIL 하
# 리스너 스레드에서 읽어도 안전. 모듈 함수(run_input_helper 등)도 같은 언어로 번역된다.
_CUR_LANG = "ko"


def _t(key: str) -> str:
    """key의 현재 언어 문자열. key/언어 누락 시 ko로 폴백 (키 없으면 key 자체 반환)."""
    entry = L10N.get(key)
    if entry is None:
        return key
    return entry.get(_CUR_LANG) or entry.get("ko", key)


def _tf(key: str, *args) -> str:
    """_t 후 %-포맷 적용 (로그/오류/상태바의 동적 문자열). args 없으면 그대로."""
    text = _t(key)
    return text % args if args else text


def _set_cur_lang(lang: str) -> None:
    """모듈 전역 언어 갱신 — GUI __init__/언어 전환 시 호출 (리스너 스레드 번역 포함)."""
    global _CUR_LANG
    if lang in ("ko", "en"):
        _CUR_LANG = lang


def _helper_executable() -> Path:
    """실행할 입력 헬퍼의 경로를 돌려준다 (패키징 대응).

    PyInstaller .app 번들 안에서는 sys.executable이 앱 바이너리라 .py를 실행할
    수 없고 __file__도 읽기 전용 번들 안을 가리킨다. 따라서 빌드 시 함께 묶은
    컴파일된 헬퍼 바이너리를 직접 실행한다. 개발 모드(스크립트)에서는 소스
    _input_helper.py를 그대로 쓴다.

    번들 내 헬퍼 경로는 플랫폼별로 다르다 (spec 참고):
      macOS  : onefile→onedir 전환 (onefile은 실행마다 _MEI 추출+재검증으로 ~9초,
               onedir은 웜 ~0.2초) → Contents/Frameworks/helper/macro_input_helper/macro_input_helper
      Windows: onefile → _MEIPASS/helper/macro_input_helper.exe
    """
    if getattr(sys, "frozen", False):
        # .app(onedir) 실행 시 sys._MEIPASS는 Contents/Frameworks (번들 데이터 위치)
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        if sys.platform == "win32":
            return base / "helper" / "macro_input_helper.exe"
        return base / "helper" / "macro_input_helper" / "macro_input_helper"
    return Path(__file__).resolve().parent / "_input_helper.py"


def _pynput_installed() -> bool:
    """pynput 존재 여부. import하지 않고 spec만 확인한다 (네이티브 import는 헬퍼에서만).

    패키징(.app) 모드에서는 크래시 격리를 위해 GUI에 pynput을 묶지 않으므로,
    대신 함께 묶은 헬퍼 바이너리(안에 pynput 포함)가 존재하면 True로 간주한다.
    """
    if getattr(sys, "frozen", False):
        return _helper_executable().exists()
    try:
        return importlib.util.find_spec("pynput") is not None
    except Exception:
        return False


def run_input_helper(action_type: str, value: str) -> None:
    """키보드 입력을 격리된 헬퍼 서브프로세스에서 실행한다.

    pynput 네이티브 크래시(세그폴트)가 GUI를 죽이지 못하도록 입력은 반드시
    별도 프로세스에서 수행한다. 헬퍼가 죽어도 GUI는 RuntimeError로 실패를
    받아 이벤트 로그에 표시한다.
    """
    helper = _helper_executable()
    # [패키징] frozen(.app)에서는 묶인 헬퍼 바이너리를 직접 실행하고,
    #           개발 모드에서는 sys.executable로 소스 헬퍼를 실행한다.
    argv = [str(helper), "--input"] if getattr(sys, "frozen", False) else [sys.executable, str(helper), "--input"]
    try:
        # [FIX] 헬퍼 onefile→onedir 전환 후에도 새 머신 최초 1회는 macOS 검증(~9초)이
        # 남아 있어 30초 여유를 준다 (웜 상태는 ~0.2초). subprocess.run은 타임아웃 시
        # 자식을 죽이지 않으므로, 헬퍼가 나중에 완료되어도 액션이 지연 실행되지 않도록
        # timeout은 오직 실패 판정 경계로만 쓴다.
        proc = subprocess.run(
            argv,
            input=json.dumps({"type": action_type, "value": value}).encode("utf-8"),
            capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise RuntimeError(_tf("err_timeout"))
    except OSError as e:
        raise RuntimeError(_tf("err_helper_os", e))
    if proc.returncode != 0:
        if proc.returncode < 0:
            raise RuntimeError(_tf("err_helper_signal", -proc.returncode))
        err = proc.stderr.decode("utf-8", "replace").strip()
        if err.startswith("ERROR: "):
            err = err[7:]
        raise RuntimeError(_tf("err_input_fail", err or _t("err_unknown")))


def _trunc_utf8(text: str, max_bytes: int) -> bytes:
    """max_bytes 이하로 자르되, UTF-8 멀티바이트 문자를 중간에 자르지 않는다."""
    b = text.encode("utf-8")
    if len(b) <= max_bytes:
        return b
    end = 0
    i = 0
    n = len(b)
    while i < n and end < max_bytes:
        c = b[i]
        w = 1 if c < 0x80 else 2 if c < 0xE0 else 3 if c < 0xF0 else 4
        if end + w > max_bytes:
            break
        end += w
        i += w
    return b[:end]


def _shade_hex(color: str, amount: int) -> str:
    """hex 색을 amount만큼 밝게(+)/어둡게(-) 한 hex 반환. 버튼 active/눌림 상태용."""
    c = color.lstrip("#")
    if len(c) != 6:
        return color
    r = max(0, min(255, int(c[0:2], 16) + amount))
    g = max(0, min(255, int(c[2:4], 16) + amount))
    b = max(0, min(255, int(c[4:6], 16) + amount))
    return "#%02X%02X%02X" % (r, g, b)


# ttk.Entry에는 네이티브 placeholder가 없다 → 포커스 기반으로 직접 구현.
# 상태는 entry._ph_text(플레이스홀더 문자열) / entry._ph_active(현재 표시 중인지)에 저장.
_PH_COLOR = "#64748b"        # 플레이스홀더 회색


def _setup_entry_placeholder(entry: ttk.Entry, ph: str) -> None:
    """빈 Entry에 회색 힌트(예: "이름", "액션")를 보여준다.

    포커스 진입 시 힌트 제거, 포커스 이탈 후 비어 있으면 다시 복원.
    값 채움/읽기는 반드시 _set_entry_value()/_entry_text()를 거쳐야 한다 —
    .get()을 직접 부르면 플레이스홀더 문자열이 실제 값으로 잡힌다.
    """
    entry._ph_text = ph
    entry._ph_active = False
    entry._ph_normal = entry.cget("foreground")   # 진짜 텍스트 색 보존

    def _focus_in(_e):
        if getattr(entry, "_ph_active", False):
            entry.delete(0, tk.END)
            entry.config(foreground=entry._ph_normal)
            entry._ph_active = False

    def _focus_out(_e):
        if not entry.get():
            entry.delete(0, tk.END)
            entry.insert(0, ph)
            entry.config(foreground=_PH_COLOR)
            entry._ph_active = True

    entry.bind("<FocusIn>", _focus_in)
    entry.bind("<FocusOut>", _focus_out)
    _set_entry_value(entry, "")                    # 초기: 비어 있으면 힌트 표시


def _set_entry_value(entry: ttk.Entry, value: str) -> None:
    """플레이스홀더를 반영해 Entry 값을 설정한다 (빈 값 → 힌트 표시)."""
    entry.delete(0, tk.END)
    if value:
        entry.insert(0, value)
        entry.config(foreground=entry._ph_normal)
        entry._ph_active = False
    else:
        entry.insert(0, entry._ph_text)
        entry.config(foreground=_PH_COLOR)
        entry._ph_active = True


def _set_placeholder_text(entry, new_ph: str) -> None:
    """placeholder 문자열 교체 (언어 전환용) — 표시 중이면 즉시 반영, 아니면 다음 표시부터."""
    entry._ph_text = new_ph
    if getattr(entry, "_ph_active", False):
        entry.delete(0, tk.END)
        entry.insert(0, new_ph)
        entry.config(foreground=_PH_COLOR)


def _entry_text(entry: ttk.Entry) -> str:
    """플레이스홀더가 표시 중이면 ""(실제 값 없음)을, 아니면 입력값을 반환."""
    if getattr(entry, "_ph_active", False):
        return ""
    return entry.get()


def _build_config_chunk(page_idx: int, items, num_pages: int, page_name: str = "") -> bytes:
    """v3 MCFG 청크 하나를 직렬화. items = [(button_id, btn_dict), ...].

    >IBBBB 헤더(magic, page, count, num_pages, 0)
    + page_name_len u8 + page_name(≤PAGE_NAME_MAX, len==0 = "변경 없음")
    + count개 엔트리(>BBBBB + label bytes + action_value bytes):
      button_id, label_len, color_idx, action_type, action_len [H]
    """
    if not (0 <= page_idx < MAX_PAGES):
        page_idx = max(0, min(page_idx, MAX_PAGES - 1))
    entries = bytearray()
    for bid, btn in items:
        label = _trunc_utf8(btn.get("label") or "", LABEL_MAX)
        color = int(btn.get("color", 0) or 0)
        if not (0 <= color < COLOR_COUNT):
            color = 0
        atype_idx = ATYPE_TO_IDX.get(btn.get("action_type") or "shortcut", 0)
        aval = _trunc_utf8(btn.get("action_value") or "", ACTION_VAL_MAX)
        entries += struct.pack(">BBBBB", bid, len(label), color, atype_idx, len(aval))
        entries += label
        entries += aval
    name_b = _trunc_utf8(page_name or "", PAGE_NAME_MAX)
    return (CONFIG_HEADER.pack(MAGIC_CONFIG, page_idx, len(items), num_pages, 0)
            + struct.pack(">B", len(name_b)) + name_b + bytes(entries))


def build_config_packet(page_idx: int, buttons, num_pages: int, page_name: str = "") -> bytes:
    """한 페이지 v3 설정 패킷을 만든다. buttons는 {'label', 'action_type', 'action_value', 'color', ...}
    딕셔너리 리스트. 단일 패킷에 들어가는 경우용 — 큰 페이지는 chunk_config_packets()를 쓴다."""
    return _build_config_chunk(page_idx, list(enumerate(buttons)), num_pages, page_name)


def chunk_config_packets(page_idx: int, buttons, num_pages: int, page_name: str = "",
                         chunk_max: int = CHUNK_MAX) -> list:
    """한 페이지 MCFG를 ≤chunk_max 바이트 청크들로 분할한 패킷 리스트.

    page_name은 첫 청크에만 실고, 이후 청크는 name_len=0(변경 없음). num_pages는 모든 청크에 반복.
    (최악 엔트리 5+24+128=157B → 12버튼 페이지는 2~3 청크일 수 있음)
    """
    name_b = _trunc_utf8(page_name or "", PAGE_NAME_MAX)
    chunks, cur_items, cur_bytes = [], [], 8 + 1 + len(name_b)
    for bid, b in enumerate(buttons):
        label = _trunc_utf8(b.get("label") or "", LABEL_MAX)
        aval = _trunc_utf8(b.get("action_value") or "", ACTION_VAL_MAX)
        item_bytes = 5 + len(label) + len(aval)
        if cur_items and cur_bytes + item_bytes > chunk_max:
            chunks.append(cur_items)
            cur_items = []
            cur_bytes = 8 + 1   # 이후 청크: 헤더 + name_len=0 (이름 생략)
        cur_items.append((bid, b))
        cur_bytes += item_bytes
    if cur_items:
        chunks.append(cur_items)
    out = []
    for i, items in enumerate(chunks):
        nm = page_name if i == 0 else ""
        out.append(_build_config_chunk(page_idx, items, num_pages, nm))
    return out


def build_ack_packet(num_pages: int) -> bytes:
    """비콘 ACK = MCFG count=0 (설정 없음). 디바이스는 소스에서 호스트 IP/포트만 학습.

    num_pages는 호스트의 실제 페이지 수를 실어야 한다 (디바이스는 count>0일 때만 반영)."""
    return CONFIG_HEADER.pack(MAGIC_CONFIG, 0, 0, num_pages, 0) + struct.pack(">B", 0)


def build_mreq_packet() -> bytes:
    """설정 덤프 요청 (MREQ): 8바이트, magic 외 필드 0."""
    return REQUEST_HEADER.pack(MAGIC_REQUEST, 0, 0, 0, 0)


def _magic_of(data: bytes):
    """패킷의 magic u32 (길이 < 4면 None). 리스너 라우팅용."""
    return struct.unpack(">I", data[:4])[0] if len(data) >= 4 else None


def parse_event_packet(data: bytes):
    """이벤트 패킷 파싱. 유효하면 (page, button, flags), 아니면 None."""
    if len(data) < 8:
        return None
    magic, page, button, flags, rsvd = EVENT_HEADER.unpack(data[:8])
    if magic != MAGIC_EVENT:
        return None
    return (page, button, flags)


# ------------------------------------------------------------------
# [G] 버튼 이름 이미지 렌더링 (Pillow) + MIMG 패킷 구성
#     호스트가 라벨을 71x61 JPEG로 그려 전송 → 펌웨어 JPEGDEC가 표시.
#     F(한글)도 이와 같은 이미지 경로로 해결한다.
# ------------------------------------------------------------------
_PIL_READY = False


def _ensure_pillow():
    """Pillow를 지연 import. GUI/액션 동작(액션 실행, 패킷 검증 일부)은 Pillow 없이도 되도록."""
    global _PIL_READY, Image, ImageDraw, ImageFont
    if _PIL_READY:
        return
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as e:
        raise RuntimeError(_t("err_pillow")) from e
    _PIL_READY = True


_FONT_CACHE = {}

# macOS AppleSDGothicNeo.ttc는 여러 굵기를 담은 TTC — 볼드 페이스 인덱스를 찾아 쓴다
# (이 시스템에서 Bold=6, ExtraBold=14). 이름 스캔이라 macOS 버전에 따라 순서가 달라도 동작.
_APPLE_GOTHIC_TTC = "/System/Library/Fonts/AppleSDGothicNeo.ttc"


def _ttc_bold_index(path: str) -> int:
    """TTC에서 정확히 'Bold' 이름을 가진 페이스 인덱스. 못 찾으면 -1 (regular 폴백 유도).

    getname()의 스타일이 'SemiBold'/'ExtraBold'처럼 Bold를 부분 포함해도 골라내지
    않도록, 스타일 첫 단어가 정확히 'Bold'인 페이스만 선택한다."""
    _ensure_pillow()   # ImageFont가 모듈에 로드돼 있어야 한다 (지연 import 방어)
    for i in range(32):
        try:
            f = ImageFont.truetype(path, 14, index=i)
        except Exception:
            return -1
        style = f.getname()[1]
        if style == "Bold" or style.startswith("Bold "):
            return i
    return -1


def _load_label_font(size: int, bold: bool = False):
    """라벨 렌더용 폰트. macOS/Windows 시스템 폰트를 우선, 없으면 기본 폰트.

    bold=True면 한/영 통합 볼드 폰트를 우선한다 — macOS AppleSDGothicNeo Bold(TTC),
    Windows Malgun/Segoe/Arial Bold. 실제 볼드 페이스가 없으면 regular로 폴백한다.
    결과는 (size, bold)별로 캐시.
    """
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    _ensure_pillow()
    font = None
    if sys.platform == "darwin":
        if bold and os.path.exists(_APPLE_GOTHIC_TTC):
            idx = _ttc_bold_index(_APPLE_GOTHIC_TTC)
            if idx >= 0:
                try:
                    font = ImageFont.truetype(_APPLE_GOTHIC_TTC, size, index=idx)
                except Exception:
                    font = None
        if font is None:
            candidates = ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
                          "/System/Library/Fonts/AppleSDGothicNeo.ttc",
                          "/System/Library/Fonts/Helvetica.ttc")
    else:
        candidates = (("C:/Windows/Fonts/malgunbd.ttf", "C:/Windows/Fonts/segoeuib.ttf",
                       "C:/Windows/Fonts/arialbd.ttf")
                      if bold else
                      ("C:/Windows/Fonts/malgun.ttf", "C:/Windows/Fonts/segoeui.ttf",
                       "C:/Windows/Fonts/arial.ttf"))
    if font is None:
        for p in candidates:
            if os.path.exists(p):
                try:
                    font = ImageFont.truetype(p, size)
                    break
                except Exception:
                    continue
    if font is None:
        try:
            font = ImageFont.load_default(size)   # Pillow 10.1+ 크기 지정 가능
        except TypeError:
            font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


# ------------------------------------------------------------------
# [PLAN 8] 이모지 라벨 렌더: 텍스트/이모지 런 분리 + Apple Color Emoji(32px 스트라이크)
#     이모지는 기본 한글 폰트에 글리프가 없어 박스로 나온다. 라벨을 이모지 런과
#     텍스트 런으로 쪼개, 이모지는 컬러 이모지 폰트로, 텍스트는 기존 폰트로 그린다.
#     Pillow 11은 layout_engine을 받지 않고 RAQM이 없어 ZWJ 결합이 제한적이므로,
#     이모지는 32px 스트라이크로 렌더 후 폰트 크기에 맞게 축소한다 (_emoji_glyph).
# ------------------------------------------------------------------
_EMOJI_FONT_CACHE = {}

# 이모지 문자 + ZWJ(‍) + 변형 셀렉터(️) + 키캡(⃣) 연속을 한 런으로 묶는다.
_EMOJI_CHAR = "[\U0001F000-\U0001FAFF☀-➿⬀-⯿←-⇿⌀-⏿️‍⃣]"
_EMOJI_RUN_RE = re.compile("(" + _EMOJI_CHAR + "+)")


def _load_emoji_font(size: int):
    """Apple Color Emoji 폰트 (macOS). 비트맵 스트라이크 크기가 한정적이라 요청 크기 로드
    실패 시 32px 스트라이크로 대체한다. 로드 불가/비macOS면 None → 텍스트 폰트 폴백."""
    if size in _EMOJI_FONT_CACHE:
        return _EMOJI_FONT_CACHE[size]
    _ensure_pillow()
    font = None
    if sys.platform == "darwin":
        p = "/System/Library/Fonts/Apple Color Emoji.ttc"
        if os.path.exists(p):
            for s in (size, 32):
                try:
                    font = ImageFont.truetype(p, s)
                    _EMOJI_FONT_CACHE[s] = font
                    break
                except Exception:
                    continue
    _EMOJI_FONT_CACHE[size] = font
    return font


def _split_label_runs(label: str):
    """라벨을 (run, is_emoji) 리스트로 분리 — 이모지/ZWJ 연속은 한 런으로."""
    parts = []
    pos = 0
    for m in _EMOJI_RUN_RE.finditer(label):
        if m.start() > pos:
            parts.append((label[pos:m.start()], False))
        parts.append((m.group(0), True))
        pos = m.end()
    if pos < len(label):
        parts.append((label[pos:], False))
    return parts


def _emoji_glyph(run: str, target_size: int):
    """이모지 런을 32px 스트라이크로 렌더해 target_size 높이 RGBA로 축소. 실패 시 None.

    Pillow 11은 layout_engine 인자를 받지 않고(RAQM 없음), 크기 16 등 일부 스트라이크
    로드가 실패하므로 항상 32px 스트라이크로 그린 뒤 폰트 크기에 맞게 축소한다.
    """
    f = _load_emoji_font(32)
    if f is None:
        return None
    pad = 4
    tmp = Image.new("RGBA", (BTN_IMG_W * 3, BTN_IMG_H * 3), (0, 0, 0, 0))
    dt = ImageDraw.Draw(tmp)
    dt.text((pad, pad), run, font=f, embedded_color=True)
    bbox = tmp.getbbox()
    if not bbox:
        return None
    glyph = tmp.crop(bbox)
    gw, gh = glyph.size
    if gw == 0 or gh == 0:
        return None
    scale = target_size / gh
    new_w = max(1, round(gw * scale))
    new_h = target_size
    if new_w > BTN_IMG_W - 2:               # 너무 넓으면 폭 기준으로 다시 축소
        scale = (BTN_IMG_W - 2) / gw
        new_w = round(gw * scale)
        new_h = max(1, round(gh * scale))
    return glyph.resize((new_w, new_h), Image.LANCZOS)


def _compose_button_image_emoji(label: str, color_idx: int):
    """이모지 포함 라벨을 버튼 JPEG로 렌더. 맞는 폰트 크기를 못 찾으면 None 반환 → 기본 경로 폴백.

    텍스트 런은 라벨 폰트로, 이모지 런은 _emoji_glyph(컬러 32px 스트라이크 축소)로
    그린다. 이모지 폭은 실제 축소 글리프 폭을 사용해 줄바꿈/정렬이 어긋나지 않게 한다.
    """
    _ensure_pillow()
    color_hex = COLOR_HEX[color_idx]
    text_hex = "#0f172a" if color_idx == 9 else "#ffffff"

    img = Image.new("RGB", (BTN_IMG_W, BTN_IMG_H), GRID_BG_HEX)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, BTN_IMG_W - 1, BTN_IMG_H - 1], radius=BTN_RADIUS, fill=color_hex)

    max_w, max_h = BTN_IMG_W - 10, BTN_IMG_H - 10
    tokens = [_split_label_runs(tok) for tok in label.split() if tok]
    if not tokens:
        return None

    # [폰트] 순수 이모지 라벨(단일 런)은 버튼을 채우는 큰 이모지로 렌더 (기존 20px → ~49px)
    if len(tokens) == 1 and len(tokens[0]) == 1 and tokens[0][0][1]:
        g = _emoji_glyph(tokens[0][0][0], max_h - 2)
        # 가로로 긴 이모지는 폭 여백(max_w)에도 맞게 다시 축소
        if g is not None and g.width > max_w:
            g = _emoji_glyph(tokens[0][0][0], max(1, round((max_h - 2) * max_w / g.width)))
        if g is not None:
            x = (BTN_IMG_W - g.width) // 2
            y = (BTN_IMG_H - g.height) // 2
            img.paste(g, (int(x), int(y)), g)
            return img

    for fs in range(20, 7, -1):
        text_font = _load_label_font(fs, bold=True)
        space_w = d.textlength(" ", font=text_font)

        # 런별 (is_emoji, width, glyph|None, run) — 이모지 폭은 실제 축소 글리프 폭
        runs = []
        for tok in tokens:
            for run, is_emoji in tok:
                if is_emoji:
                    g = _emoji_glyph(run, fs)
                    w = g.width if g is not None else d.textlength(run, font=text_font)
                    runs.append((True, w, g, run))
                else:
                    runs.append((False, d.textlength(run, font=text_font), None, run))

        # 런 단위 greedy 줄바꿈 (최대 2줄)
        lines = []
        cur = []
        cur_w = 0.0
        too_wide = False
        for r in runs:
            sep = 0.0 if not cur else space_w
            if cur and cur_w + sep + r[1] > max_w:
                lines.append(cur)
                cur = []
                cur_w = 0.0
                sep = 0.0
                if len(lines) >= 2:
                    too_wide = True
                    break
            cur.append(r)
            cur_w += sep + r[1]
        if too_wide:
            continue
        if cur:
            lines.append(cur)
        if not lines:
            continue
        line_h = fs + 4
        if len(lines) * line_h > max_h:
            continue
        total_h = len(lines) * line_h
        y0 = (BTN_IMG_H - total_h) / 2
        for i, line in enumerate(lines):
            line_w = sum(r[1] for r in line)
            x = (BTN_IMG_W - line_w) / 2
            y = y0 + i * line_h
            for is_emoji, _w, g, run in line:
                if is_emoji and g is not None:
                    ey = y + (line_h - g.height) // 2      # 줄 안에서 세로 중앙
                    img.paste(g, (int(x), int(ey)), g)
                    x += g.width
                else:
                    bbox = d.textbbox((0, 0), run, font=text_font)
                    # [폰트] 볼드 페이스 자체가 두꺼우므로 stroke 겹침은 제거 (뭉개짐 방지)
                    d.text((x - bbox[0], y - bbox[1]), run, font=text_font, fill=text_hex)
                    x += d.textlength(run, font=text_font)
        return img
    return None


def _wrap_words(draw, words, font, max_w, max_lines):
    """공백 기준 워드랩. max_lines줄 이내로 못 넣으면 None 반환 (폰트 축소 유도)."""
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip() if cur else w
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            if draw.textlength(w, font=font) > max_w:
                return None
            cur = w
    if cur:
        lines.append(cur)
    return lines if len(lines) <= max_lines else None


def _compose_button_image(label: str, color_idx: int):
    """71x61 버튼 이미지(PIL)를 그린다: 버튼 색 라운드사각 + 중앙 라벨(최대 2줄).

    이미지 모서리는 GRID_BG_HEX — 펌웨어가 그대로 push하면 라운드 코너가 주변
    배경과 이어진다. 테두리는 눌림 상태(펌웨어 drawRoundRect)에 따라 그리므로 여기엔 안 넣는다.
    """
    # [PLAN 8] 이모지 포함 라벨은 텍스트/이모지 런 분리 렌더 (실패 시 아래 기본 경로로 폴백)
    if _EMOJI_RUN_RE.search(label):
        emo = _compose_button_image_emoji(label, color_idx)
        if emo is not None:
            return emo

    _ensure_pillow()
    color_hex = COLOR_HEX[color_idx]
    text_hex = "#0f172a" if color_idx == 9 else "#ffffff"   # 흰색 배경 → 검정 글자

    img = Image.new("RGB", (BTN_IMG_W, BTN_IMG_H), GRID_BG_HEX)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, BTN_IMG_W - 1, BTN_IMG_H - 1], radius=BTN_RADIUS, fill=color_hex)

    max_w, max_h = BTN_IMG_W - 10, BTN_IMG_H - 10
    words = label.split()
    for fs in range(20, 7, -1):
        font = _load_label_font(fs, bold=True)
        lines = _wrap_words(d, words, font, max_w, 2)
        if lines is None:
            continue
        line_h = fs + 3
        if len(lines) * line_h > max_h:
            continue
        total_h = len(lines) * line_h
        y0 = (BTN_IMG_H - total_h) / 2
        for i, ln in enumerate(lines):
            bbox = d.textbbox((0, 0), ln, font=font)
            x = (BTN_IMG_W - (bbox[2] - bbox[0])) / 2 - bbox[0]
            y = y0 + i * line_h - bbox[1]
            d.text((x, y), ln, font=font, fill=text_hex)
        return img
    # 최소 폰트에서도 안 들어가면 잘림
    font = _load_label_font(7, bold=True)
    short = label if len(label) <= 7 else label[:6] + "…"
    bbox = d.textbbox((0, 0), short, font=font)
    d.text(((BTN_IMG_W - (bbox[2] - bbox[0])) / 2 - bbox[0],
            (BTN_IMG_H - (bbox[3] - bbox[1])) / 2 - bbox[1]), short, font=font, fill=text_hex)
    return img


def _jpeg_fit(img) -> bytes:
    """JPEG 인코딩. 1400B 이하로 맞으면 즉시 반환, 초과 시 품질을 낮추되
    JPEG_MIN_QUALITY(70) 하한을 지킨다. 1400B를 넘는 결과는 MIMG fmt=2 멀티 패킷
    전송이 처리하므로 이 함수는 "단일 패킷 크기"가 아니라 "품질 하한"을 보장한다.

    [FIX] subsampling=0(4:4:4)로 크로마 서브샘플링 아티팩트 제거 — 단색 버튼에서
    텍스트/모서리 경계가 얼룩덜룩(chroma smear)하게 보이던 원인.
    """
    q = JPEG_QUALITY
    while q >= JPEG_MIN_QUALITY:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=q, subsampling=0, optimize=True)
        data = buf.getvalue()
        if len(data) <= JPEG_MAX_BYTES:
            return data
        q -= 5
    return data   # 최저 품질에도 초과 → 호출부에서 라벨을 줄여 재구성


def _render_button_image(label: str, color_idx: int) -> bytes:
    """라벨/색상 → JPEG 바이트(품질 하한 JPEG_MIN_QUALITY 보장; 초과는 fmt=2 청킹 전송).
    극단적으로 큰 라벨은 축약 후 재구성."""
    img = _compose_button_image(label, color_idx)
    data = _jpeg_fit(img)
    if len(data) <= JPEG_MAX_BYTES:
        return data
    short = label if len(label) <= 7 else label[:6] + "…"
    return _jpeg_fit(_compose_button_image(short, color_idx))


def _image_to_button_jpeg(img) -> bytes:
    """업로드 이미지를 버튼 크기(71x61)로 중앙 크롭 채움 변환 후 JPEG 인코딩.

    비율은 유지하되 버튼보다 작은 축을 채우도록 확대하고 중앙을 크롭한다.
    (늘어나지도, 빈 여백도 생기지 않는다 — 스트림덱 아이콘 방식)
    [PLAN 7] 모서리는 그리드 배경 위 rounded_rectangle(BTN_RADIUS) 알파 마스크로 합성해
    펌웨어 텍스트 버튼(fillRoundRect BTN_RADIUS)과 같은 둥근 모서리로 표시한다.
    """
    _ensure_pillow()
    ratio = max(BTN_IMG_W / img.width, BTN_IMG_H / img.height)
    w, h = max(1, round(img.width * ratio)), max(1, round(img.height * ratio))
    img = img.resize((w, h), Image.LANCZOS)
    left = (w - BTN_IMG_W) // 2
    top = (h - BTN_IMG_H) // 2
    img = img.crop((left, top, left + BTN_IMG_W, top + BTN_IMG_H))
    # 라운드 코너 마스크: rounded_rectangle 알파로 잘라 그리드 배경 위에 올린다.
    canvas = Image.new("RGB", (BTN_IMG_W, BTN_IMG_H), GRID_BG_HEX)
    mask = Image.new("L", (BTN_IMG_W, BTN_IMG_H), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, BTN_IMG_W - 1, BTN_IMG_H - 1], radius=BTN_RADIUS, fill=255)
    canvas.paste(img, (0, 0), mask)
    return _jpeg_fit(canvas)


def _image_file_to_b64(path) -> str:
    """이미지 파일 → 버튼 크기 JPEG → base64 문자열 (config에 저장, 내보내기로 이동)."""
    _ensure_pillow()
    with Image.open(path) as im:
        return base64.b64encode(_image_to_button_jpeg(im.convert("RGB"))).decode("ascii")


def build_image_packet(page: int, button_id: int, jpeg_bytes: bytes, fmt: int = 0) -> bytes:
    """버튼 이미지 패킷 (MIMG): 8B 헤더 + 페이로드. 단일 UDP 패킷으로 전송.

    fmt: 0 = JPEG 바이트(버튼 71x61), 1 = 이미지 제거(clear, 페이로드 없음 —
    디바이스는 해당 버튼을 펌웨어 텍스트/색 사각형으로 폴백).
    """
    return IMAGE_HEADER.pack(MAGIC_IMAGE, page, button_id, fmt, 0) + jpeg_bytes


def build_image_packets(page: int, button_id: int, jpeg_bytes: bytes) -> list:
    """버튼 이미지 전송 패킷 목록.

    ≤JPEG_MAX_BYTES(1400B)면 기존 MIMG fmt=0 단일 패킷 그대로(하위호환).
    초과면 MIMG fmt=2 청킹으로 나눈다 — 8B 헤더 + 8B 서브헤더 `>IHH`
    (total_len u32, total_chunks u16, chunk_idx u16) + 청크 데이터(≤IMG_CHUNK_DATA).
    데이터그램 총량 8+8+1400 = 1416 < UDP MTU 1472 → 단편화 없이 항상 도착.
    """
    if len(jpeg_bytes) <= JPEG_MAX_BYTES:
        return [build_image_packet(page, button_id, jpeg_bytes, 0)]
    total = len(jpeg_bytes)
    chunks = (total + IMG_CHUNK_DATA - 1) // IMG_CHUNK_DATA
    return [
        IMAGE_HEADER.pack(MAGIC_IMAGE, page, button_id, 2, 0)
        + struct.pack(">IHH", total, chunks, i)
        + jpeg_bytes[i * IMG_CHUNK_DATA:(i + 1) * IMG_CHUNK_DATA]
        for i in range(chunks)
    ]


def parse_beacon_packet(data: bytes) -> bool:
    """디스커버리 비콘("MPBE") 여부. True면 리스너가 소스 IP를 자동 학습한다."""
    if len(data) < 8:
        return False
    return EVENT_HEADER.unpack(data[:8])[0] == MAGIC_BEACON


def _is_valid_ip(ip: str) -> bool:
    """IPv4 주소 형식인지 검사 (빈 문자열/잘못된 형식이면 False)."""
    try:
        socket.inet_aton(ip)
        return True
    except OSError:
        return False


# ------------------------------------------------------------------
# [H] 디바이스에서 불러오기: MREQ 덤프 수집 (순수 함수 — 오프라인 테스트 가능)
#     dump dict: {"pages": {page: {"name", "buttons": {bid: {...}}}},
#                 "images": {(page,bid): base64}, "num_pages": int|None}
# ------------------------------------------------------------------
def apply_mcfg_to_dump(dump: dict, data: bytes) -> bool:
    """MREQ 응답의 MCFG 덤프 패킷을 dump에 병합. 부분 유효도 진행.

    페이지/버튼을 bid별로 축적하므로 패킷 순서/중복에 무관하다."""
    if len(data) < 9:
        return False
    magic, page, count, num_pages, rsvd = CONFIG_HEADER.unpack(data[:8])
    if magic != MAGIC_CONFIG:
        return False
    name_len = data[8]
    if 9 + name_len > len(data):
        return False
    name = data[9:9 + name_len].decode("utf-8", "replace")
    off = 9 + name_len
    dump["num_pages"] = num_pages
    pg = dump["pages"].setdefault(page, {"name": "", "buttons": {}})
    if name:
        pg["name"] = name
    for _ in range(count):
        if off + 5 > len(data):
            break
        bid, llen, col, atype, alen = struct.unpack_from(">BBBBB", data, off)
        off += 5
        if off + llen + alen > len(data):
            break
        label = data[off:off + llen].decode("utf-8", "replace")
        off += llen
        aval = data[off:off + alen].decode("utf-8", "replace")
        off += alen
        pg["buttons"][bid] = {
            "label": label,
            "color": col,
            "action_type": ATYPE_FROM_IDX[atype] if 0 <= atype < len(ATYPE_FROM_IDX) else "shortcut",
            "action_value": aval,
        }
    return True


def apply_mimg_to_dump(dump: dict, data: bytes) -> bool:
    """MREQ 응답의 MIMG 덤프 패킷을 base64로 저장. fmt=0 단일 또는 fmt=2 청킹 지원.

    fmt=2 청킹은 (page,bid)별 누적 버퍼를 dump["img_chunks"]에 두고, 모든 청크가
    모이면 base64로 dump["images"]에 저장한다. 순서 무관·중복 무시.
    """
    if len(data) < 8:
        return False
    magic, page, bid, fmt, rsvd = IMAGE_HEADER.unpack(data[:8])
    if magic != MAGIC_IMAGE:
        return False
    if fmt == 0:
        dump["images"][(page, bid)] = base64.b64encode(data[8:]).decode("ascii")
        return True
    if fmt == 2:
        if len(data) < 16:
            return False
        total, chunks, idx = struct.unpack(">IHH", data[8:16])
        chunk = data[16:]
        if not (0 < total <= IMG_MAX_BYTES and 0 < chunks <= 16 and idx < chunks):
            return False
        off = idx * IMG_CHUNK_DATA
        if off + len(chunk) > total:
            return False
        acc = dump.setdefault("img_chunks", {}).setdefault((page, bid), None)
        if acc is None or acc["total"] != total or acc["chunks"] != chunks:
            acc = {"total": total, "chunks": chunks,
                   "buf": bytearray(total), "got": [False] * chunks}
            dump["img_chunks"][(page, bid)] = acc
        if acc["got"][idx]:
            return False                     # 중복 청크 → 무시
        acc["buf"][off:off + len(chunk)] = chunk
        acc["got"][idx] = True
        if all(acc["got"]):
            dump["images"][(page, bid)] = base64.b64encode(bytes(acc["buf"])).decode("ascii")
            del dump["img_chunks"][(page, bid)]
        return True
    return False


def build_config_from_dump(dump: dict):
    """수집 완료된 dump → config dict(version 3). 모든 페이지의 12버튼이 채워졌을 때만
    성공하고, 그렇지 않으면 None (일부 페이지 누락 = 불완전 덤프)."""
    np = dump.get("num_pages")
    if not np or np < 1:
        return None
    pages = []
    for p in range(np):
        pg = dump["pages"].get(p)
        if not pg or len(pg.get("buttons", {})) < BUTTONS_PER_PAGE:
            return None
        btns = []
        for bid in range(BUTTONS_PER_PAGE):
            b = pg["buttons"].get(bid)
            if not b:
                return None
            btns.append({"label": b["label"], "action_type": b["action_type"],
                         "action_value": b["action_value"], "color": b["color"],
                         "image": dump["images"].get((p, bid))})
        pages.append({"name": pg["name"] or "Page %d" % (p + 1), "buttons": btns})
    return {"version": 3, "port": UDP_PORT, "device_ip": dump.get("device_ip", ""), "pages": pages}


def _settings_projection(config: dict) -> tuple:
    """디바이스와 호스트 설정 비교용 투영: 페이지 수/이름 + 버튼 라벨·색·액션.

    이미지(base64/바이트)는 표시용 파생물이라 비교에서 제외 — 한글/이모지 라벨은 label로
    재현되므로 설정(라벨·색·액션)이 같으면 이미지도 자연히 같아진다. 빈 호스트가 디바이스
    내용을 지우는 원래 버그를 설정 비교만으로 정확히 잡아낸다.
    """
    pages = config.get("pages") or []
    proj = []
    for p in pages:
        name = (p.get("name") or "").strip()
        btns = []
        for b in (p.get("buttons") or [])[:BUTTONS_PER_PAGE]:
            btns.append(((b.get("label") or "").strip(),
                         int(b.get("color") or 0),
                         b.get("action_type") or "shortcut",
                         (b.get("action_value") or "").strip()))
        proj.append((name, tuple(btns)))
    return (len(pages), tuple(proj))


class MacroPadGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("CYD Macro Pad Host")
        # 창 크기: [PLAN 2] 버튼 설정(4x3 카드)이 스크롤 없이 다 보이도록 기본 크기를 키운다.
        # 화면이 작을 때만 높이를 줄이고(스크롤 폴백), 리사이즈로 조절 가능하게 한다.
        w, h = 600, 940
        sh = self.root.winfo_screenheight()
        if h > sh - 40:                        # 메뉴바/독 여유만 빼고 최대 높이 사용
            h = max(480, sh - 40)
        self.root.geometry("%dx%d" % (w, h))
        self.root.resizable(True, True)
        self.root.minsize(520, 560)

        if getattr(sys, "frozen", False):
            # [패키징] .app 번들 안은 읽기 전용이므로 설정 파일은 사용자 Application Support에 저장
            app_support = Path.home() / "Library" / "Application Support" / "CYD Macro Pad"
            app_support.mkdir(parents=True, exist_ok=True)
            self.config_path = app_support / "macro_config.json"
        else:
            self.config_path = Path(__file__).resolve().parent / "macro_config.json"
        self.config = self._load_config()          # dict (런타임 스냅샷: 메인/리스너가 lock으로 읽음)

        # [PLAN] 언어 설정 (호스트 표시 전용 — config에만 저장, 와이어 프로토콜 무관)
        self.lang = self.config.get("lang", "ko") or "ko"
        if self.lang not in ("ko", "en"):
            self.lang = "ko"
        _set_cur_lang(self.lang)                   # 모듈 전역 → 모듈 함수(run_input_helper 등)도 동일 언어

        # 스레드 동기화
        self._config_lock = threading.Lock()
        self._resend_queue: "queue.Queue[str]" = queue.Queue()  # Apply→리스너 재전송 요청
        self._event_queue: "queue.Queue[tuple]" = queue.Queue()  # 리스너→UI 로그
        self._listener_running = False
        self._listener_thread: threading.Thread | None = None

        self._button_widgets: list = []            # [page][button] → 위젯 dict
        self._page_tabs: list = []                 # [page] → Notebook 탭 Frame
        self._img_cache: dict = {}                 # [G] (page,bid,label,color) → JPEG 바이트 (재렌더 방지)
        self._img_pillow_warned = False            # [G] Pillow 미설치 경고 1회만
        self._drag_tab: str | None = None          # 탭 드래그 재정렬 중인 탭 ID (None=미드래그)
        self._tab_dragged = False                  # 드래그 중 실제 탭 이동이 있었는지 (클릭↔드래그 구분)
        self._help_win: "tk.Toplevel | None" = None  # [?] 도움말 창 단일 인스턴스

        # [PLAN] 언어 전환 리프레시 상태
        self._i18n_widgets: list = []             # (widget, key) — 정적 위젯, _refresh_lang에서 text 갱신
        self._listen_status: tuple | None = None  # (key, fg, args) — 상태바 마지막 표시값 (재렌더용)

        # [H] 동기화 재설계 상태
        self._pushed_ip: str | None = None         # 이번 세션에서 전체 푸시한 디바이스 IP (1회 푸시 판정)
        self._dump = None                           # 리스너 스레드 전용 MREQ 덤프 수집 상태 (None=미수집)
        self._dump_queue: "queue.Queue[tuple]" = queue.Queue()   # 메인→리스너: ("dump_start", ip)
        # [DBG] 리스너 진단 로그 경로 (Application Support — 창 모드 앱도 쓸 수 있는 곳)
        self._debug_log_path = Path.home() / "Library" / "Application Support" / "CYD Macro Pad" / "listener_debug.log"
        try:
            self._debug_log_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        self.setup_ui_style()
        self.create_widgets()
        self._populate_from_config()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._start_listener()                     # 리스너 자동 시작 (고정 포트 8890)
        self._warmup_helper()                      # [FIX] 헬퍼 최초 콜드 시작(~9초)을 백그라운드로
        self._check_accessibility()                # [FIX] 손쉬운 사용 권한 없으면 경고
        self._poll_event_queue()

    # ------------------------------------------------------------------
    # 설정 로드/저장
    # ------------------------------------------------------------------
    def _load_config(self) -> dict:
        if self.config_path.exists():
            try:
                raw = json.loads(self.config_path.read_text(encoding="utf-8"))
                return self._normalize_config(raw)
            except Exception as e:
                logging.warning("[CFG] 설정 파싱 실패, 기본값 사용: %s", e)
        return self._default_config()

    @staticmethod
    def _default_config() -> dict:
        empty = [{"label": "", "action_type": "shortcut", "action_value": "", "color": 0,
                  "image": None}
                 for _ in range(BUTTONS_PER_PAGE)]
        return {
            "version": 3,
            "port": UDP_PORT,
            "device_ip": "",
            "lang": "ko",
            "pages": [{"name": "Page %d" % (i + 1), "buttons": list(empty)}
                      for i in range(DEFAULT_PAGES)],
        }

    def _normalize_config(self, config: dict) -> dict:
        """구버전(1) 설정을 v2로 보정. 누락된 필드를 기본값으로 채운다."""
        pages = config.get("pages")
        if not isinstance(pages, list) or not pages:
            pages = self._default_config()["pages"]
        norm = []
        for i, pg in enumerate(pages):
            if not isinstance(pg, dict):
                pg = {}
            btns = pg.get("buttons")
            if not isinstance(btns, list):
                btns = []
            buttons = []
            for bid in range(BUTTONS_PER_PAGE):
                b = btns[bid] if bid < len(btns) and isinstance(btns[bid], dict) else {}
                color = int(b.get("color", 0) or 0)
                if not (0 <= color < COLOR_COUNT):
                    color = 0
                image = b.get("image")
                if not isinstance(image, str) or not image:
                    image = None
                buttons.append({
                    "label": (b.get("label") or "")[:LABEL_MAX],
                    "action_type": b.get("action_type", "shortcut") or "shortcut",
                    "action_value": b.get("action_value") or "",
                    "color": color,
                    "image": image,
                })
            norm.append({"name": (pg.get("name") or "Page %d" % (i + 1)), "buttons": buttons})
        lang = config.get("lang", "ko")
        return {
            "version": 3,
            "port": int(config.get("port", UDP_PORT) or UDP_PORT),
            "device_ip": str(config.get("device_ip", "") or ""),
            "lang": lang if lang in ("ko", "en") else "ko",
            "pages": norm,
        }

    def _save_config(self, config: dict) -> None:
        self.config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # [PLAN] 언어 설정: 조회 헬퍼 + 언어 메뉴/전환/리프레시
    # ------------------------------------------------------------------
    def _atype_labels(self) -> dict:
        """현재 언어의 액션 타입 라벨 맵 {내부값: 표시라벨}."""
        return ATYPE_LABELS.get(self.lang, ATYPE_LABELS["ko"])

    def _atype_label(self, atype: str) -> str:
        return self._atype_labels().get(atype, atype)

    def _color_names(self) -> list:
        """현재 언어의 색상 표시명 리스트 (index 계약은 그대로 — 와이어/펌웨어와 무관)."""
        return COLOR_NAMES.get(self.lang, COLOR_NAMES["ko"])

    def _color_name(self, idx: int) -> str:
        names = self._color_names()
        return names[idx] if 0 <= idx < len(names) else names[0]

    def _color_index(self, name: str):
        """표시명 → index (어느 언어든). 모르는 이름이면 None."""
        return COLOR_NAME_TO_IDX.get(name)

    def _show_lang_menu(self) -> None:
        """언어 선택 팝업 — ? 버튼 왼쪽의 🌐 버튼. 선택 즉시 전환 + config 저장."""
        menu = tk.Menu(self.root, tearoff=0)
        for code, label in (("ko", "한국어"), ("en", "English")):
            menu.add_radiobutton(label=label, value=code,
                                 command=lambda c=code: self._set_lang(c))
        try:
            menu.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())
        finally:
            menu.grab_release()

    def _set_lang(self, lang: str) -> None:
        if lang not in ("ko", "en") or lang == self.lang:
            return
        self.lang = lang
        _set_cur_lang(lang)
        with self._config_lock:
            self.config["lang"] = lang
        self._save_config(self.config)
        # 도움말/메뉴는 재오픈 시 새 언어로 — 열려 있던 창은 닫는다
        if self._help_win is not None:
            try:
                if self._help_win.winfo_exists():
                    self._help_win.destroy()
            except tk.TclError:
                pass
            self._help_win = None
        self.lang_btn.config(text=_t("lang_btn"))
        self._refresh_lang()
        self._log(_tf("log_lang", "한국어" if lang == "ko" else "English"))

    def _refresh_lang(self) -> None:
        """언어 전환 시 표시 문자열 일괄 갱신 (카드 상태: color index/action type 보존)."""
        # 1) 정적 위젯
        for widget, key in self._i18n_widgets:
            try:
                widget.config(text=_t(key))
            except tk.TclError:
                pass
        # 2) 상태바
        if self._listen_status:
            key, fg, args = self._listen_status
            self.listen_status.config(text=_tf(key, *args), fg=fg)
        # 3) 카드별 동적 위젯 — 현재 선택값(내부 index/타입)을 새 언어 라벨로 재표시
        atype_labels = self._atype_labels()
        color_names = self._color_names()
        for page_widgets in self._button_widgets:
            for w in page_widgets:
                w["action"].config(values=list(atype_labels.values()))   # 드롭다운 목록도 새 언어로
                w["color"].config(values=color_names)
                atype = ATYPE_FROM_LABEL.get(w["action"].get(), "shortcut")
                w["action"].set(atype_labels.get(atype, atype))
                cidx = self._color_index(w["color"].get())   # 어느 언어 라벨이든 index 복원
                if cidx is not None:
                    w["color"].set(color_names[cidx])
                self._update_hint(w["hint"], w["action"])
                for entry, key in ((w["label"], "ph_name"), (w["value"], "ph_action")):
                    _set_placeholder_text(entry, _t(key))

    def _set_listen_status(self, key: str, fg: str, *args) -> None:
        """상태바 갱신 + 재렌더용 (key, fg, args) 저장. 언어 전환 시 _refresh_lang이 복원."""
        self._listen_status = (key, fg, args)
        try:
            self.listen_status.config(text=_tf(key, *args), fg=fg)
        except tk.TclError:
            pass    # 창 생성 전/파괴 후 호출되면 무시

    # ------------------------------------------------------------------
    # UI 스타일
    # ------------------------------------------------------------------
    def setup_ui_style(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")

        self.bg_color = "#1e293b"
        self.card_bg = "#0f172a"
        self.text_color = "#f8fafc"
        self.sub_text = "#94a3b8"

        self.root.configure(bg=self.bg_color)
        style.configure("TLabel", background=self.card_bg, foreground=self.text_color,
                        font=("Pretendard", 10))
        style.configure("Header.TLabel", background=self.bg_color, foreground=self.text_color,
                        font=("Pretendard", 14, "bold"))
        style.configure("TCheckbutton", background=self.card_bg, foreground=self.text_color,
                        font=("Pretendard", 10))
        style.configure("TFrame", background=self.card_bg)

    def _button(self, parent, text, command, bg, fg="#ffffff",
                font=("Pretendard", 10), padx=10, pady=6, **kw):
        """색상 버튼 생성. tk.Button이 아니라 clam 테마 ttk.Button을 쓴다.

        [PLAN 3] macOS는 tk.Button을 네이티브(Aqua)로 그려서 창이 활성/포커스되면 커스텀 bg가
        무시되고 밝은 흰 배경 + 흰 글자가 되어 구분이 안 된다. clam 테마 커스텀 스타일은
        창 상태와 무관하게 background/foreground를 유지하므로 모든 상태에서 읽힌다.
        """
        # [FIX] ttk는 레이아웃을 스타일명의 마지막 점 뒤(베이스)에서 찾는다. 즉 "커스텀.베이스"
        #     형태여야 clam의 TButton 레이아웃을 쓰면서 색만 덮어쓴다. "커스텀"만 쓰면
        #     "Layout ... not found", "베이스.커스텀"(예: TButton.X)도 실패한다.
        tag = "Cbtn_%s_%s_%s.TButton" % (bg.lstrip("#").upper(), font[1], padx)
        style = ttk.Style()
        style.configure(tag, background=bg, foreground=fg, bordercolor=bg,
                        lightcolor=bg, darkcolor=bg, focuscolor=bg,
                        relief="flat", padding=(padx, pady), font=font)
        style.map(tag,
                  background=[("active", _shade_hex(bg, 24)),
                              ("pressed", _shade_hex(bg, -16))],
                  foreground=[("active", fg), ("pressed", fg)])
        return ttk.Button(parent, text=text, command=command, style=tag, **kw)

    # ------------------------------------------------------------------
    # 위젯 구성
    # ------------------------------------------------------------------
    def create_widgets(self) -> None:
        # 헤더 (제목 좌측 + 사용법 '?' 버튼 우측 상단)
        header_frame = tk.Frame(self.root, bg=self.bg_color, pady=8)
        header_frame.pack(fill=tk.X, padx=16)
        title_row = tk.Frame(header_frame, bg=self.bg_color)
        title_row.pack(fill=tk.X)
        ttk.Label(title_row, text="⌨️ CYD Macro Pad Host",
                  style="Header.TLabel").pack(side=tk.LEFT)
        # [FIX] '?' 버튼도 내보내기/가져오기와 같은 모양(ttk clam, #334155)으로 통일 —
        #       macOS Aqua 렌더링에서 bg/fg가 무시돼 '?'가 안 보일 수 있던 문제도 해결
        self.help_btn = self._button(title_row, "?", self._show_help, "#334155",
                                     font=("Pretendard", 12), padx=8, pady=6, width=2)
        self.help_btn.pack(side=tk.RIGHT, padx=(8, 0))
        # [PLAN] 언어 선택 버튼 — ? 다음에 pack해 ? 왼쪽에 위치. 선택 즉시 전체 UI 문자열 전환.
        self.lang_btn = self._button(title_row, _t("lang_btn"), self._show_lang_menu, "#334155",
                                     font=("Pretendard", 10), padx=8, pady=6)
        self.lang_btn.pack(side=tk.RIGHT, padx=(8, 0))
        self._subtitle_label = tk.Label(header_frame, text=_t("subtitle"),
                                        bg=self.bg_color, fg=self.sub_text,
                                        font=("Pretendard", 9))
        self._subtitle_label.pack(anchor="w")
        self._i18n_widgets.append((self._subtitle_label, "subtitle"))

        # 리스너 상태 카드 (IP/포트/시작 버튼 없음 — 리스너는 자동 시작)
        status_card = tk.Frame(self.root, bg=self.card_bg, bd=1, relief=tk.SOLID, padx=12, pady=8)
        status_card.pack(fill=tk.X, padx=16, pady=(0, 8))
        self.listen_status = tk.Label(status_card, text=_t("status_starting"), bg=self.card_bg,
                                      fg=self.sub_text, font=("Pretendard", 9), anchor="w")
        self.listen_status.pack(fill=tk.X)
        self._set_listen_status("status_starting", self.sub_text)   # 언어 전환 재렌더용 상태 기록

        # 페이지 관리 행 (이름 편집 + 추가/삭제)
        page_row = tk.Frame(self.root, bg=self.card_bg, bd=1, relief=tk.SOLID, padx=12, pady=8)
        page_row.pack(fill=tk.X, padx=16, pady=(0, 8))
        self._page_name_label = ttk.Label(page_row, text=_t("label_page_name"))
        self._page_name_label.pack(side=tk.LEFT)
        self._i18n_widgets.append((self._page_name_label, "label_page_name"))
        self.page_name_entry = self._text_entry(page_row, 18)
        self.page_name_entry.pack(side=tk.LEFT, padx=6)
        self.page_name_entry.bind("<KeyRelease>", lambda e: self._page_name_edited())
        # [FIX] +/− 페이지 버튼을 내보내기/가져오기와 동일한 모양(배경/폰트/패딩)으로 통일.
        #       사이 간격도 내보내기↔가져오기와 같은 8px를 둔다.
        self.del_page_btn = self._button(page_row, _t("btn_del_page"), self._del_page, "#334155",
                                         font=("Pretendard", 12), padx=8, pady=6)
        self.del_page_btn.pack(side=tk.RIGHT, padx=(0, 6))
        self.add_page_btn = self._button(page_row, _t("btn_add_page"), self._add_page, "#334155",
                                         font=("Pretendard", 12), padx=8, pady=6)
        self.add_page_btn.pack(side=tk.RIGHT, padx=(0, 8))
        self._i18n_widgets += [(self.del_page_btn, "btn_del_page"),
                               (self.add_page_btn, "btn_add_page")]

        # 페이지 노트북 (최대 MAX_PAGES × 4×3 버튼) — pack은 하단 바를 먼저 고정한 뒤 진행
        self.notebook = ttk.Notebook(self.root)
        self.notebook.bind("<<NotebookTabChanged>>", self._page_changed)
        self.notebook.bind("<ButtonPress-1>", self._tab_press)     # 탭 드래그 재정렬
        self.notebook.bind("<B1-Motion>", self._tab_drag)
        self.notebook.bind("<ButtonRelease-1>", self._tab_release)
        self._button_widgets = []
        self._page_tabs = []

        # 하단 카드: 적용 / 내보내기 / 가져오기 + 이벤트 로그 — side=BOTTOM으로 먼저 pack해서
        # 창이 짧아져도 하단 버튼이 항상 보이게 고정 (노트북이 남은 공간을 흡수)
        bot_card = tk.Frame(self.root, bg=self.card_bg, bd=1, relief=tk.SOLID, padx=12, pady=10)
        bot_card.pack(side=tk.BOTTOM, fill=tk.X, padx=16, pady=(0, 8))
        bot_row = tk.Frame(bot_card, bg=self.card_bg)
        bot_row.pack(fill=tk.X)
        self.apply_btn = self._button(bot_row, _t("btn_apply"), self._apply, "#0ea5e9",
                                      font=("Pretendard", 12, "bold"), padx=12, pady=6)
        self.apply_btn.pack(side=tk.LEFT)
        self.export_btn = self._button(bot_row, _t("btn_export"), self._export_config, "#334155",
                                       font=("Pretendard", 12), padx=8, pady=6)
        self.export_btn.pack(side=tk.LEFT, padx=(8, 0))
        self.import_btn = self._button(bot_row, _t("btn_import"), self._import_config, "#334155",
                                       font=("Pretendard", 12), padx=8, pady=6)
        self.import_btn.pack(side=tk.LEFT, padx=(8, 0))
        self.device_import_btn = self._button(bot_row, _t("btn_device_import"),
                                              self._import_from_device, "#334155",
                                              font=("Pretendard", 12), padx=8, pady=6)
        self.device_import_btn.pack(side=tk.LEFT, padx=(8, 0))
        self._i18n_widgets += [(self.apply_btn, "btn_apply"), (self.export_btn, "btn_export"),
                               (self.import_btn, "btn_import"),
                               (self.device_import_btn, "btn_device_import")]
        log_frame = tk.Frame(bot_card, bg=self.card_bg)
        log_frame.pack(fill=tk.BOTH, pady=(8, 0))
        self.log_text = tk.Text(log_frame, height=6, bg="#020617", fg=self.text_color,
                                insertbackground=self.text_color, state=tk.DISABLED,
                                font=("Pretendard", 12), relief=tk.FLAT, padx=6, pady=4)
        self.log_text.pack(fill=tk.BOTH)
        self.log_text.tag_configure("error", foreground="#ef4444")
        self._log(_t("log_ready"))

        # 노트북 pack (하단 바 위 남은 공간 전체) 후 페이지 탭 구성
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 8))
        pages = self.config.get("pages") or []
        for page in range(len(pages)):
            self._make_page_tab(page, pages[page].get("name") or "Page %d" % (page + 1))

    def _show_help(self) -> None:
        """[?] 버튼 — 프로그램 사용법 안내 대화상자를 연다.

        [FIX] '?'를 누를 때마다 새 창이 생기지 않도록 단일 창을 재사용한다.
        이미 열려 있으면 새로 만들지 않고 위로 올려 포커스를 준다.
        """
        win = getattr(self, "_help_win", None)
        if win is not None:
            try:
                if win.winfo_exists():
                    win.lift()
                    win.focus_force()
                    return
            except tk.TclError:
                pass    # 창이 이미 파괴됨 → 아래에서 새로 생성
        win = tk.Toplevel(self.root)
        self._help_win = win
        win.title(_t("help_title"))
        win.configure(bg=self.bg_color)
        win.geometry("640x600")
        win.transient(self.root)
        win.resizable(True, True)

        txt = tk.Text(win, bg=self.card_bg, fg=self.text_color,
                      insertbackground=self.text_color, wrap=tk.WORD,
                      font=("Pretendard", 14), relief=tk.FLAT,
                      padx=18, pady=14, spacing1=4, spacing3=4)
        sb = ttk.Scrollbar(win, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        txt.pack(fill=tk.BOTH, expand=True, padx=12, pady=(12, 4))

        txt.tag_configure("h",   foreground=self.text_color, font=("Pretendard", 17, "bold"))
        txt.tag_configure("sub", foreground=self.sub_text, font=("Pretendard", 14, "bold"))
        txt.tag_configure("code", foreground="#7dd3fc", font=("Menlo", 13))
        txt.tag_configure("warn", foreground="#fbbf24", font=("Pretendard", 14, "bold"))

        # (태그, 본문) — 실제 동작 정의(_exec_shortcut/_exec_text/_exec_app)와 일치시킬 것
        # [PLAN] 전부 _t() 기반 — 언어 전환 시 재오픈하면 새 언어로 표시된다.
        guide = [
            ("h",   _t("help_h")),
            ("sub", _t("help_start_sub")),
            ("",    _t("help_start_1")),
            ("",    _t("help_start_2")),
            ("",    _t("help_start_3")),
            ("",    _t("help_start_4")),
            ("sub", _t("help_act_sub")),
            ("",    _t("help_act_shortcut")), ("code", _t("help_ex_shortcut")),
            ("",    _t("help_act_text")), ("code", _t("help_ex_text")),
            ("",    _t("help_act_text_ime")),
            ("sub", _t("help_act_text_ime_sub")),
            ("",    _t("help_act_app")),
            ("code", "://"),
            ("",    _t("help_act_url_open")),
            ("code", _t("help_act_app_ex")),
            ("code", _t("help_act_url_ex")),
            ("sub", _t("help_keys_sub")),
            ("code", _t("help_keys_mod")),
            ("code", _t("help_keys_nav")),
            ("code", _t("help_keys_etc")),
            ("code", _t("help_keys_fn")),
            ("code", _t("help_keys_f")),
            ("",    _t("help_keys_plus")), ("code", "+"), ("", _t("help_keys_with")),
            ("code", "cmd+option+v"), ("", _t("help_keys_more")),
            ("",    _t("help_keys_single")), ("code", "cmd+a, cmd+1"),
            ("",    _t("help_keys_shiftnote")),
            ("sub", _t("help_special_sub")),
            ("",    _t("help_special_expl")),
            ("code", "+"), ("", _t("help_special_sep")),
            ("code", _t("help_special_map")),
            ("",    _t("help_special_ex")), ("code", "cmd+shift+="),
            ("",    _t("help_special_ex_descr")),
            ("",    _t("help_special_textnote")), ("code", "text"),
            ("",    _t("help_special_textnote2")), ("code", "Hello! <3"), ("", ").\n"),
            ("sub", _t("help_emoji_sub")),
            ("",    _t("help_emoji_expl")),
            ("code", _t("help_ex_emoji")),
            ("",    _t("help_emoji_tail")),
            ("warn", _t("help_perm_warn")),
            ("",    _t("help_perm_expl")),
            ("sub", _t("help_backup_sub")),
            ("",    _t("help_backup_export")),
            ("",    _t("help_backup_import")),
            ("",    _t("help_backup_device")),
        ]
        for tag, text in guide:
            txt.insert(tk.END, text, tag)
        txt.config(state=tk.DISABLED)

        close = tk.Button(win, text=_t("help_close"), command=win.destroy, width=8,
                          bg="#334155", fg=self.text_color, activebackground="#475569",
                          activeforeground=self.text_color, relief=tk.FLAT, bd=0,
                          font=("Pretendard", 14))
        close.pack(pady=10)
        win.bind("<Escape>", lambda e: win.destroy())

    def _text_entry(self, parent: tk.Widget, width: int = 12) -> tk.Entry:
        """버튼 설정용 텍스트 박스 (classic tk.Entry).

        [FIX] ttk.Entry는 macOS Tk 8.6.15에서 insertbackground 위젯 옵션을 지원하지 않아
        입력 캐럿(타이핑 커서)이 안 보인다. classic tk.Entry는 insertbackground로 캐럿 색을
        직접 지정하고 항상 캐럿을 그린다. 다크 카드(#0f172a)에 맞춰 스타일링.
        """
        return tk.Entry(parent, width=width, bg="#0f172a", fg=self.text_color,
                        insertbackground=self.text_color, relief=tk.FLAT,
                        highlightthickness=1, highlightbackground="#334155",
                        highlightcolor="#0ea5e9",
                        selectbackground="#0ea5e9", selectforeground="#0f172a",
                        font=("Pretendard", 12))

    def _make_button_card(self, parent: tk.Widget, page: int, bid: int) -> dict:
        """버튼 한 개의 편집 카드 (라벨/동작/값/색 + [G] 이미지 업로드 + 힌트)."""
        f = tk.Frame(parent, bg="#1e293b", bd=1, relief=tk.SOLID, padx=5, pady=3)
        tk.Label(f, text="#%d" % bid, bg="#1e293b", fg=self.sub_text,
                 font=("Pretendard", 10)).pack(anchor="w")
        lbl = self._text_entry(f, 12)
        lbl.pack(fill=tk.X, pady=(2, 2))
        _setup_entry_placeholder(lbl, _t("ph_name"))    # 빈 칸일 때 힌트 (어느 칸인지 표시)
        act = ttk.Combobox(f, values=list(self._atype_labels().values()),
                           width=10, state="readonly")
        act.pack(fill=tk.X, pady=(0, 2))
        val = self._text_entry(f, 12)
        val.pack(fill=tk.X, pady=(0, 2))
        _setup_entry_placeholder(val, _t("ph_action"))  # 액션 값(단축키/문구/앱·URL) 힌트
        # 색상 선택 (구분용): 스와치 + 팔레트 Combobox
        color_row = tk.Frame(f, bg="#1e293b")
        color_row.pack(fill=tk.X, pady=(0, 2))
        swatch = tk.Label(color_row, text="  ", bg=COLOR_HEX[0], width=2)
        swatch.pack(side=tk.LEFT)
        col = ttk.Combobox(color_row, values=self._color_names(), width=9, state="readonly")
        col.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))
        col.bind("<<ComboboxSelected>>",
                 lambda e, s=swatch, c=col: self._color_selected(s, c))
        # [G] 이미지 업로드: 업로드 버튼 + 썸네일(미리보기) + 제거 버튼
        img_row = tk.Frame(f, bg="#1e293b")
        img_row.pack(fill=tk.X, pady=(0, 2))
        # [FIX] 이모지 글리프가 ttk 버튼 폭을 크게(약 84px) 만들어 행이 넘쳐 '✕'가 잘렸다.
        #       expand 제거 + 명시적 width(문자 단위)로 고정해 세 요소가 모두 보이게 한다.
        img_btn = self._button(img_row, "🖼", lambda: self._pick_image(page, bid), "#334155",
                               font=("Pretendard", 10), padx=2, pady=1, width=3)
        img_btn.pack(side=tk.LEFT)
        img_preview = tk.Label(img_row, text=_t("preview_label"), bg="#0f172a", fg=self.sub_text,
                               font=("Pretendard", 9), padx=4, pady=2, width=7)
        img_preview.pack(side=tk.LEFT, padx=4)
        img_clear = self._button(img_row, "✕", lambda: self._clear_image(page, bid), "#334155",
                                 fg="#f8fafc", font=("Pretendard", 10), padx=2, pady=1, width=3)
        img_clear.pack(side=tk.LEFT)
        hint = tk.Label(f, text="", bg="#1e293b", fg=self.sub_text,
                        font=("Pretendard", 9), anchor="w")
        hint.pack(fill=tk.X)
        act.bind("<<ComboboxSelected>>",
                 lambda e, h=hint, a=act: self._update_hint(h, a))
        return {"frame": f, "label": lbl, "action": act, "value": val,
                "color": col, "swatch": swatch, "img_btn": img_btn,
                "img_preview": img_preview, "img_clear": img_clear,
                "img_photo": None, "hint": hint}

    def _color_selected(self, swatch: tk.Label, col: ttk.Combobox) -> None:
        idx = self._color_index(col.get())
        if idx is not None:
            swatch.config(bg=COLOR_HEX[idx])

    # ------------------------------------------------------------------
    # [G] 버튼 이미지 업로드/제거/미리보기
    # ------------------------------------------------------------------
    def _pick_image(self, page: int, bid: int) -> None:
        """버튼에 이미지 파일 업로드 → 버튼 크기(71x61) JPEG base64로 저장.

        [H] 즉시 푸시하지 않는다 — 디바이스 반영은 [설정 적용]을 눌렀을 때만.
        """
        path = filedialog.askopenfilename(
            filetypes=[(_tf("fd_image_files"), "*.png *.jpg *.jpeg *.bmp *.gif *.webp"),
                       (_tf("fd_all_files"), "*.*")])
        if not path:
            return
        try:
            b64 = _image_file_to_b64(path)
        except Exception as e:
            self._log(_tf("log_img_convert_fail", e), error=True)
            return
        with self._config_lock:
            pages = self.config["pages"]
            if page < len(pages):
                btns = pages[page]["buttons"]
                if bid < len(btns):
                    btns[bid]["image"] = b64
        self._update_card_image_preview(page, bid)
        self._log(_tf("log_img_upload", page + 1, bid, os.path.basename(path)))

    def _clear_image(self, page: int, bid: int) -> None:
        """버튼 이미지 제거 → 디바이스는 라벨 텍스트/색 사각형으로 폴백 (MIMG fmt=1).

        [H] 즉시 푸시하지 않는다 — 디바이스 반영은 [설정 적용]을 눌렀을 때만.
        """
        with self._config_lock:
            pages = self.config["pages"]
            if page < len(pages):
                btns = pages[page]["buttons"]
                if bid < len(btns):
                    btns[bid]["image"] = None
        self._update_card_image_preview(page, bid)
        self._log(_tf("log_img_remove", page + 1, bid))

    def _update_card_image_preview(self, page: int, bid: int) -> None:
        """카드 썸네일을 config의 base64 이미지로 갱신 (없으면 '라벨/색상' 표시)."""
        if not (0 <= page < len(self._button_widgets)) or not (0 <= bid < BUTTONS_PER_PAGE):
            return
        w = self._button_widgets[page][bid]
        b64 = None
        with self._config_lock:
            pages = self.config["pages"]
            if page < len(pages):
                btns = pages[page]["buttons"]
                if bid < len(btns):
                    b64 = btns[bid].get("image")
        w["img_photo"] = None                    # 이전 참조 해제
        if isinstance(b64, str) and b64:
            try:
                _ensure_pillow()
                img = Image.open(io.BytesIO(base64.b64decode(b64)))
                img.thumbnail((40, 34))          # 카드에 맞는 썸네일 크기 (비율 유지)
                buf = io.BytesIO()
                img.convert("RGB").save(buf, "PNG")
                photo = tk.PhotoImage(data=buf.getvalue())
                w["img_photo"] = photo           # GC 방지: 참조를 카드에 유지해야 함
                w["img_preview"].config(image=photo, text="", width=0, height=0)
                return
            except Exception:
                w["img_photo"] = None
        w["img_preview"].config(image="", text=_t("preview_label"))

    def _make_page_tab(self, idx: int, name: str) -> None:
        """페이지 탭 하나를 만들어 _button_widgets/_page_tabs에 추가한다."""
        tab = tk.Frame(self.notebook, bg=self.card_bg, padx=8, pady=8)
        self.notebook.add(tab, text=name or "Page %d" % (idx + 1))

        # 창이 짧아도 카드가 잘리지 않도록 세로 스크롤 캔버스로 감싼다
        canvas = tk.Canvas(tab, bg=self.card_bg, highlightthickness=0)
        scroll = ttk.Scrollbar(tab, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        grid = tk.Frame(canvas, bg=self.card_bg)
        grid_id = canvas.create_window((0, 0), window=grid, anchor="nw")
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        def _fit_canvas(e):                      # 캔버스 폭에 카드 그리드 폭을 맞춤 + 스크롤 영역 갱신
            canvas.itemconfigure(grid_id, width=e.width)
            canvas.configure(scrollregion=canvas.bbox("all"))
        canvas.bind("<Configure>", _fit_canvas)
        grid.bind("<Configure>",
                  lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        page_widgets = []
        for bid in range(BUTTONS_PER_PAGE):
            r, c = divmod(bid, GRID_COLS)
            card = self._make_button_card(grid, idx, bid)
            card["frame"].grid(row=r, column=c, padx=4, pady=2, sticky="nsew")
            page_widgets.append(card)
        for c in range(GRID_COLS):
            grid.columnconfigure(c, weight=1, uniform="col")
        for r in range(GRID_ROWS):
            grid.rowconfigure(r, weight=1, uniform="row")
        self._button_widgets.append(page_widgets)
        self._page_tabs.append(tab)

    def _update_hint(self, hint: tk.Label, act: ttk.Combobox) -> None:
        hints = {"shortcut": _t("hint_shortcut"),
                 "text": _t("hint_text"),
                 "app": _t("hint_app")}
        label = act.get()
        hint.config(text=hints.get(ATYPE_FROM_LABEL.get(label, label), ""))

    # ------------------------------------------------------------------
    # 설정 ↔ 위젯
    # ------------------------------------------------------------------
    def _populate_from_config(self) -> None:
        pages = self.config.get("pages") or []
        for page in range(len(self._button_widgets)):
            for bid in range(BUTTONS_PER_PAGE):
                w = self._button_widgets[page][bid]
                btn = {}
                if page < len(pages):
                    btns = pages[page].get("buttons", [])
                    if bid < len(btns):
                        btn = btns[bid]
                _set_entry_value(w["label"], btn.get("label") or "")
                w["action"].set(self._atype_label(btn.get("action_type") or "shortcut"))
                _set_entry_value(w["value"], btn.get("action_value") or "")
                color = int(btn.get("color", 0) or 0)
                if not (0 <= color < COLOR_COUNT):
                    color = 0
                w["color"].set(self._color_name(color))
                w["swatch"].config(bg=COLOR_HEX[color])
                self._update_hint(w["hint"], w["action"])
        # [G] 각 카드 이미지 미리보기 갱신 (업로드된 base64 이미지 표시)
        for page in range(len(self._button_widgets)):
            for bid in range(BUTTONS_PER_PAGE):
                self._update_card_image_preview(page, bid)
        if self._page_tabs:
            self._load_page_name(self._current_page_idx())

    # ------------------------------------------------------------------
    # 페이지 관리 (이름 편집 / 추가 / 삭제)
    # ------------------------------------------------------------------
    def _current_page_idx(self) -> int:
        try:
            return self.notebook.index(self.notebook.select())
        except tk.TclError:
            return 0

    def _page_name_edited(self) -> None:
        idx = self._current_page_idx()
        name = self.page_name_entry.get().strip()[:20]
        with self._config_lock:
            pages = self.config["pages"]
            if 0 <= idx < len(pages):
                pages[idx]["name"] = name
        if 0 <= idx < len(self._page_tabs):
            self.notebook.tab(self._page_tabs[idx], text=name or "Page %d" % (idx + 1))

    def _page_changed(self, event=None) -> None:
        self._load_page_name(self._current_page_idx())

    def _load_page_name(self, idx: int) -> None:
        with self._config_lock:
            pages = self.config["pages"]
            name = pages[idx].get("name", "") if 0 <= idx < len(pages) else ""
        self.page_name_entry.delete(0, tk.END)
        self.page_name_entry.insert(0, name)

    def _add_page(self) -> None:
        with self._config_lock:
            if len(self.config["pages"]) >= MAX_PAGES:
                self._log(_tf("log_page_max", MAX_PAGES), error=True)
                return
        idx = len(self._page_tabs)
        self._make_page_tab(idx, "Page %d" % (idx + 1))
        with self._config_lock:
            self.config["pages"].append({
                "name": "Page %d" % (idx + 1),
                "buttons": [{"label": "", "action_type": "shortcut", "action_value": "", "color": 0}
                            for _ in range(BUTTONS_PER_PAGE)],
            })
        self.notebook.select(self._page_tabs[idx])
        self._load_page_name(idx)
        self._log(_tf("log_page_add", idx + 1))

    def _del_page(self) -> None:
        with self._config_lock:
            if len(self.config["pages"]) <= 1:
                messagebox.showwarning(_t("msg_delpage_title"), _t("msg_min1page"))
                return
        idx = self._current_page_idx()
        if not (0 <= idx < len(self._button_widgets)):
            return
        has_content = any(
            _entry_text(w["label"]).strip() or _entry_text(w["value"]).strip()
            for w in self._button_widgets[idx])
        if has_content and not messagebox.askyesno(
                _t("msg_delpage_title"), _tf("msg_delpage_confirm", idx + 1)):
            return
        self.notebook.forget(idx)
        tab = self._page_tabs.pop(idx)
        tab.destroy()
        self._button_widgets.pop(idx)
        with self._config_lock:
            self.config["pages"].pop(idx)
        self._reindex_tab_labels()
        self._page_changed()
        self._log(_tf("log_page_del", idx + 1))

    def _reindex_tab_labels(self) -> None:
        with self._config_lock:
            pages = self.config["pages"]
        for i, tab in enumerate(self._page_tabs):
            name = pages[i].get("name") if i < len(pages) else "Page %d" % (i + 1)
            self.notebook.tab(tab, text=name or "Page %d" % (i + 1))

    # ------------------------------------------------------------------
    # 페이지 탭 드래그 재정렬
    # ------------------------------------------------------------------
    def _tab_press(self, event) -> None:
        """탭 스트립 위에서 눌렀을 때만 드래그를 시작한다.

        [FIX] macOS Tk 8.6.15에서 ttk.Notebook.bbox()는 항상 (0,0,0,0)을 반환해 탭
        영역 판별에 못 쓴다. 대신 clam 테마는 index("@x,y")가 탭 스트립 위에서만 유효
        인덱스를 돌려주고 콘텐츠/빈 영역에서는 TclError를 던진다 — 이걸로 구분한다.
        "break"를 반환하지 않는 것은 탭 선택(클래스 바인딩)이 함께 동작하도록 하기 위함.
        """
        self._drag_tab = None
        self._tab_dragged = False
        try:
            idx = self.notebook.index("@%d,%d" % (event.x, event.y))
        except tk.TclError:
            return
        self._drag_tab = self.notebook.tabs()[idx]

    def _tab_drag(self, event) -> str:
        """드래그 중 마우스가 위치한 탭 자리로 현재 탭을 옮긴다 (실시간 미리보기)."""
        if not self._drag_tab:
            return "break"
        try:
            idx = self.notebook.index("@%d,%d" % (event.x, event.y))
        except tk.TclError:
            return "break"     # 스트립 밖(콘텐츠)으로 나가면 이동 중단
        if idx != self.notebook.index(self._drag_tab):
            self.notebook.insert(idx, self._drag_tab)
            self._tab_dragged = True   # 실제 이동이 있었을 때만 릴리스에서 동기화
        return "break"

    def _tab_release(self, event) -> str:
        """드래그 종료 — 실제 이동이 있었을 때만 순서를 config/위젯/디바이스에 반영한다.

        [FIX] 단순 클릭(드래그 없음)은 동기화하지 않는다 — 매 클릭마다 재푸시되면
        탭 선택만으로 디바이스가 리셋/재렌더된다.
        """
        if not self._drag_tab:
            return "break"
        was_dragged = self._tab_dragged
        self._drag_tab = None
        self._tab_dragged = False
        if was_dragged:
            self._sync_pages_from_tabs()
        return "break"

    def _sync_pages_from_tabs(self) -> None:
        """Notebook 탭의 시각적 순서를 config/_page_tabs/_button_widgets에 반영하고 재푸시.

        드래그 후 호출. 위젯 편집을 먼저 스냅샷(_collect_config)으로 받은 뒤 새 순서로
        재배치해, Apply를 누르지 않았더라도 최신 편집 상태가 순서와 함께 디바이스에 반영된다.
        """
        frames = [self.notebook.nametowidget(t) for t in self.notebook.tabs()]
        with self._config_lock:
            if len(frames) != len(self.config["pages"]) or len(frames) != len(self._page_tabs):
                return
        # 각 탭 프레임 → 이전 페이지 인덱스 (드래그 전 순서 기준)
        try:
            order = [self._page_tabs.index(f) for f in frames]
        except ValueError:
            return
        fresh = self._collect_config()["pages"]     # 위젯 현재 상태 스냅샷 (드래그 전 순서)
        with self._config_lock:
            self.config["pages"] = [fresh[i] for i in order]
        self._button_widgets = [self._button_widgets[i] for i in order]
        self._page_tabs = frames
        self._reindex_tab_labels()
        self._save_config(self.config)
        self._log(_t("log_page_reorder"))
        ip = self.config.get("device_ip", "") or ""
        if self._listener_running:
            self._resend_queue.put("resend")
        elif _is_valid_ip(ip):
            self._send_config_from_temp_socket(ip, UDP_PORT)

    def _collect_config(self) -> dict:
        with self._config_lock:
            cur_pages = self.config["pages"]
        pages = []
        for page in range(len(self._button_widgets)):
            buttons = []
            for bid in range(BUTTONS_PER_PAGE):
                w = self._button_widgets[page][bid]
                cname = w["color"].get()
                color = self._color_index(cname)
                if color is None:                     # 과거 언어 라벨이 남아 있으면 0으로 폴백
                    color = 0
                # 업로드 이미지는 위젯이 아니라 config 스냅샷(cur_pages)에서 가져온다 (base64 보존)
                image = None
                if page < len(cur_pages):
                    cbtns = cur_pages[page].get("buttons", [])
                    if bid < len(cbtns):
                        img = cbtns[bid].get("image")
                        image = img if isinstance(img, str) and img else None
                buttons.append({
                    "label": _entry_text(w["label"]).strip()[:LABEL_MAX],
                    "action_type": ATYPE_FROM_LABEL.get(w["action"].get(), "shortcut"),
                    "action_value": _entry_text(w["value"]),
                    "color": color,
                    "image": image,
                })
            name = cur_pages[page].get("name") if page < len(cur_pages) else "Page %d" % (page + 1)
            pages.append({"name": name, "buttons": buttons})
        return {"version": 3, "port": UDP_PORT,
                "device_ip": self.config.get("device_ip", "") or "",
                "lang": self.lang,
                "pages": pages}

    # ------------------------------------------------------------------
    # 설정 내보내기/가져오기 (D)
    # ------------------------------------------------------------------
    def _export_config(self) -> None:
        config = self._collect_config()
        path = filedialog.asksaveasfilename(
            defaultextension=".json", initialfile="macro_config.json",
            filetypes=[(_tf("fd_json_files"), "*.json")])
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(config, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
            self._log(_tf("log_export", path, len(config["pages"])))
        except OSError as e:
            self._log(_tf("log_export_fail", e), error=True)

    def _import_config(self) -> None:
        path = filedialog.askopenfilename(
            filetypes=[(_tf("fd_json_files"), "*.json"), (_tf("fd_all_files"), "*.*")])
        if not path:
            return
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as e:
            messagebox.showerror(_t("msg_import_fail_title"),
                                 _tf("msg_import_fail", e))
            return
        new_config = self._normalize_config(raw)
        new_config["lang"] = self.lang          # 언어는 호스트 표시 설정 — 가져온 파일이 덮지 않게 보존
        with self._config_lock:
            self.config = new_config
        self._rebuild_all_tabs()
        self._save_config(new_config)
        self._log(_tf("log_import", path, len(new_config["pages"])))

    def _import_from_device(self) -> None:
        """[H] "디바이스에서 불러오기": MREQ 전송 + 덤프 수집 시작 (리스너가 큐를 드레인).

        액션은 디바이스에 저장만 되어 있고 실행은 항상 호스트가 하므로, 덤프로
        복원된 프로필을 그대로 Apply하면 무해한 자기 동기화가 된다.
        """
        with self._config_lock:
            ip = self.config.get("device_ip", "") or ""
        if not _is_valid_ip(ip):
            messagebox.showerror(_t("msg_devimport_title"), _t("msg_noip"))
            return
        if not self._listener_running:
            messagebox.showerror(_t("msg_devimport_title"), _t("msg_listener_down"))
            return
        self._dump_queue.put(("dump_start", ip, UDP_PORT, False))   # auto=False: 수동 불러오기
        self._log(_tf("log_device_import_req", ip))

    def _apply_dump_config(self, config: dict, n_img_recv: int = 0) -> None:
        """[H] 덤프 완료: GUI를 디바이스 설정으로 채운다 (검토 후 수동 Apply)."""
        new_config = self._normalize_config(config)
        new_config["lang"] = self.lang          # 언어는 호스트 표시 설정 — 덤프가 덮지 않게 보존
        n_pages = len(new_config["pages"])
        n_images = sum(1 for pg in new_config["pages"] for b in pg.get("buttons", [])
                       if b.get("image"))
        with self._config_lock:
            self.config = new_config
        self._rebuild_all_tabs()
        self._save_config(new_config)
        self._log(_tf("log_device_import_done", n_pages, n_img_recv))
        if n_images == 0:
            self._log(_t("log_device_import_noimg"), error=True)

    def _rebuild_all_tabs(self) -> None:
        """가져오기 후 페이지 탭을 전부 다시 만든다 (페이지 수/이름이 바뀔 수 있음)."""
        for tab in self._page_tabs:
            self.notebook.forget(tab)
            tab.destroy()
        self._page_tabs = []
        self._button_widgets = []
        pages = self.config.get("pages") or []
        for page in range(len(pages)):
            self._make_page_tab(page, pages[page].get("name") or "Page %d" % (page + 1))
        self._populate_from_config()
        if self._page_tabs:
            self.notebook.select(self._page_tabs[0])

    def _ask_yesno(self, title: str, msg: str) -> bool:
        """예/아니오 질문. macOS에서 메시지박스가 메인 창 뒤로 숨는 문제 방지.

        기본 messagebox는 parent가 없으면 앱 활성화 상태에 따라 메인 창 뒤에 숨어
        안 보일 수 있다 (보고된 "멈춤" 증상의 원인 — 코드는 정상 실행 중이었음).
        루트를 앞으로 올리고 parent를 명시해 대화창이 항상 앞에 보이게 한다.
        """
        try:
            self.root.lift()
            self.root.focus_force()
            self.root.update_idletasks()
        except tk.TclError:
            pass
        return messagebox.askyesno(parent=self.root, title=title, message=msg)

    def _handle_auto_sync(self, config: dict, n_img: int, ip: str, port: int) -> None:
        """[동기화 보호] 첫 연결 시 디바이스 설정과 호스트 설정을 비교해 방향을 묻는다.

        무조건 푸시하면 빈(신규) 호스트가 디바이스 설정을 지울 수 있어, MREQ 덤프로
        디바이스 설정을 읽어 호스트와 다르면 사용자에게 묻는다 (메인 스레드).
          - [예]     → 디바이스 설정을 호스트로 불러옴 (디바이스는 이미 그 내용이라 재푸시 안 함)
          - [아니오] → 아무것도 안 함 — 디바이스 보호 (다음 실행 시 다시 질문)
        동일하면 푸시/질문 없이 조용히 통과.
        """
        dev_proj = _settings_projection(config)
        host_proj = _settings_projection(self.config)
        self._debug_log("[autosync] ip=%s equal=%s dev_pages=%d host_pages=%d"
                        % (ip, dev_proj == host_proj, len(dev_proj[1]), len(host_proj[1])))
        if dev_proj == host_proj:
            self._log(_tf("log_autosync_same", ip))
            return
        ask = self._ask_yesno(_t("msg_autosync_title"), _tf("msg_autosync_ask", ip))
        if ask:
            self._apply_dump_config(config, n_img)
            self._log(_tf("log_autosync_loaded", ip))
        else:
            self._log(_tf("log_autosync_none", ip))

    # ------------------------------------------------------------------
    # 설정 적용 (Apply)
    # ------------------------------------------------------------------
    def _apply(self) -> None:
        config = self._collect_config()
        self._save_config(config)
        with self._config_lock:
            self.config = config
        ip = self.config.get("device_ip", "") or ""
        if self._listener_running:
            self._resend_queue.put("resend")
            self._log(_tf("log_apply_resend", len(config["pages"])))
        elif _is_valid_ip(ip):
            self._send_config_from_temp_socket(ip, UDP_PORT)
            self._log(_tf("log_apply_sent", ip))
        else:
            self._log(_t("log_apply_wait"))

    def _send_config_from_temp_socket(self, ip: str, port: int) -> None:
        """리스너 미실행 시 임시 소켓으로 설정 전송.

        소스 포트를 port에 바인드해 디바이스가 호스트 수신 포트를 정확히 학습하게 한다.
        (리스너가 없으므로 포트 충돌 없음)
        """
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("0.0.0.0", port))
            except OSError:
                pass  # 바인드 실패 시 IP는 학습되지만 포트는 불확실 (드문 경우)
            try:
                self._send_all_pages(sock, ip, port)
                self._send_all_images(sock, ip, port)   # [G] 임시 소켓 경로에도 이미지 포함
            finally:
                sock.close()
        except OSError as e:
            self._log(_tf("log_push_fail", e), error=True)

    def _send_all_pages(self, sock: socket.socket, ip: str, port: int) -> None:
        with self._config_lock:
            pages = self.config.get("pages") or []
        num_pages = len(pages)
        for page in range(num_pages):
            btns = pages[page].get("buttons", []) if page < len(pages) else []
            # [H] v3: 패딩에도 action 키 포함 (기존 누락 수정) — 디바이스가 config.bin에 온전히 저장하도록
            while len(btns) < BUTTONS_PER_PAGE:
                btns = btns + [{"label": "", "action_type": "shortcut", "action_value": "",
                                "color": 0, "image": None}]
            name = pages[page].get("name", "") if page < len(pages) else ""
            # [H] 큰 페이지는 MCFG 여러 청크로 분할 (MTU 1472 방지, bid별 갱신으로 합쳐짐)
            for pkt in chunk_config_packets(page, btns, num_pages, name):
                self._safe_sendto(sock, pkt, (ip, port))

    def _push_config(self, sock: socket.socket, ip: str, port: int, retries: int = 1,
                     reason: str = "푸시") -> None:
        """전체 설정+이미지 푸시. retries만큼 250ms 간격 재전송 (UDP 유실 대비).

        [H] 3초 재푸시 안전망이 제거됐으므로 Apply/시작/첫 비콘 푸시는 2회 전송으로 보정한다.
        리스너 스레드에서 실행되므로 로그는 _event_queue로 라우팅 (Tkinter 직접 접근 금지).
        reason은 트리거 진단용 — "적용 안 눌렀는데 표시"는 호스트 시작/첫 비콘 푸시로 확인된다.
        """
        self._event_queue.put(("log", _tf("log_push", reason, ip, port), False))
        for attempt in range(retries + 1):
            if attempt:
                time.sleep(0.25)
            self._send_all_pages(sock, ip, port)
            self._send_all_images(sock, ip, port)

    def _send_all_images(self, sock: socket.socket, ip: str, port: int) -> None:
        """버튼 이미지(MIMG) 전송 (G): 업로드 이미지 / 한글 라벨 / ASCII·빈 라벨(clear).

        설정 푸시(MCFG) 직후 같은 소스로 호출된다. 버튼별 의도 3분기:
          1. 업로드 이미지(base64) → 그대로 MIMG 전송
          2. 한글(비-ASCII) 라벨 → 텍스트를 71x61 JPEG로 렌더해 MIMG 전송
          3. ASCII/빈 라벨 → MIMG(fmt=1, clear) 전송 → 디바이스가 펌웨어 텍스트/색 사각형 폴백
        이미지는 ≤JPEG_MAX_BYTES(1400B)면 fmt=0 단일, 초과면 fmt=2 청킹
        (build_image_packets)으로 분할 전송 — 품질 하한 JPEG_MIN_QUALITY(70) 보장.
        렌더 결과는 (page,bid,...) 키로 캐시해 재인코딩을 피한다. 캐시는 리스너 스레드에서만
        접근하므로 단일 스레드 안전. 로그는 Tkinter 직접 접근 금지 → _event_queue로 라우팅.
        """
        with self._config_lock:
            pages = self.config.get("pages") or []
        keys = set()
        rendered = 0        # 새로 렌더한 라벨 이미지 수 (캐시 미스 — 설정이 실제로 바뀜)
        for page in range(len(pages)):
            btns = pages[page].get("buttons", []) if page < len(pages) else []
            for bid in range(BUTTONS_PER_PAGE):
                btn = btns[bid] if bid < len(btns) else {}
                label = (btn.get("label") or "").strip()
                b64 = btn.get("image") or None
                color = int(btn.get("color", 0) or 0)

                # (1) 업로드 이미지 → 저장된 JPEG 그대로 전송
                if b64:
                    key = (page, bid, "img", b64)
                    jpeg = self._img_cache.get(key)
                    if jpeg is None:
                        try:
                            # [FIX] 과거 버전/가져오기로 들어온 >JPEG_MAX_BYTES(1400B) JPEG:
                            #      품질을 낮춰 클램프하는 대신 MIMG fmt=2 청킹
                            #      (build_image_packets)으로 전송한다. 단일 패킷 초과는
                            #      IP 단편화 → ESP32 Wi-Fi 수신 실패로 버튼이 안 뜬다.
                            jpeg = base64.b64decode(b64)
                        except Exception:
                            continue           # 잘못된 base64 → 이 버튼은 보내지 않음
                        self._img_cache[key] = jpeg
                    keys.add(key)
                    for pkt in build_image_packets(page, bid, jpeg):
                        self._safe_sendto(sock, pkt, (ip, port))
                    continue

                # (2) 한글(비-ASCII) 라벨 → 텍스트를 71x61 JPEG로 렌더해 전송
                if label and not label.isascii():
                    if not (0 <= color < COLOR_COUNT):
                        color = 0
                    key = (page, bid, label, color)
                    keys.add(key)
                    jpeg = self._img_cache.get(key)
                    if jpeg is None:
                        try:
                            jpeg = _render_button_image(label, color)
                        except RuntimeError as e:
                            # Pillow 미설치 같은 시스템적 실패는 1회만 경고 (3초 비콘마다 스팸 방지).
                            # 판정은 번역과 무관하게 __cause__(ImportError)로 구분한다.
                            if isinstance(e.__cause__, ImportError) and not self._img_pillow_warned:
                                self._img_pillow_warned = True
                                self._event_queue.put(("log", str(e), True))
                            else:
                                self._event_queue.put(
                                    ("log", _tf("log_render_fail", page + 1, bid, e), True))
                            continue
                        except Exception as e:
                            self._event_queue.put(
                                ("log", _tf("log_render_fail", page + 1, bid, e), True))
                            continue
                        self._img_cache[key] = jpeg
                        rendered += 1
                    for pkt in build_image_packets(page, bid, jpeg):
                        self._safe_sendto(sock, pkt, (ip, port))
                    continue

                # (3) ASCII/빈 라벨 → clear(fmt=1): 디바이스의 구 이미지를 제거하고 텍스트로 폴백.
                #     매 푸시마다 보내 호스트/디바이스 재시작 후에도 의도가 수렴된다.
                #     (한글→ASCII 전환 시 남는 스테일 이미지 제거 — 펌웨어는 이미지 없으면 no-op)
                keys.add((page, bid, "clear"))
                self._safe_sendto(sock, build_image_packet(page, bid, b"", 1), (ip, port))
        # 캐시 정리: 더 이상 존재하지 않는 키(버튼/라벨/색/이미지 변경) 제거
        for k in list(self._img_cache):
            if k not in keys:
                del self._img_cache[k]
        # 3초 비콘 재푸시(캐시 히트)는 조용히 — 새로 렌더된 게 있을 때만 로그
        if rendered:
            self._event_queue.put(("log", _tf("log_images_sent", rendered), False))

    # ------------------------------------------------------------------
    # 리스너 스레드 (UDP 수신 + 액션 실행)
    # ------------------------------------------------------------------
    def _debug_log(self, msg: str) -> None:
        """[DBG] 리스너 스레드 진단 로그 (파일). 창 모드 앱은 stderr이 /dev/null이라
        파일로 남긴다. 문제 진단/회귀 확인용으로 유지."""
        try:
            with open(self._debug_log_path, "a", encoding="utf-8") as f:
                f.write("[dbg] %s\n" % msg)
        except Exception:
            pass

    def _safe_sendto(self, sock: socket.socket, data: bytes, addr) -> bool:
        """UDP 전송 — 리스너 스레드가 sendto 에러로 죽지 않게 보호한다.

        macOS UDP는 도달 불가(ARP 실패/EHOSTUNREACH)나 ICMP 오류가 소켓에 큐잉되어
        다음 sendto/recvfrom을 OSError로 터뜨릴 수 있다. 무조건 푸시처럼 멀티캐스트
        전송 중 실패하면 리스너가 통째로 죽어 "설정 비교에서 멈춤"이 된다. 여기서는
        전송 실패를 무시하고 수신 루프를 계속 유지한다 (비콘→ACK 루프가 자가치유).
        성공 시 True, 실패 시 False.
        """
        try:
            sock.sendto(data, addr)
            return True
        except OSError as e:
            # [FIX] 포맷 인자 수 버그: 이 줄이 TypeError로 크래시하면 리스너 스레드가
            # 죽어 디바이스 이벤트를 못 받는다 (sendto ICMP/라우팅 오류 시 방아쇠).
            self._debug_log("sendto %d bytes → %s: %s" % (len(data), addr, e))
            return False

    def _start_listener(self) -> None:
        """리스너 자동 시작: 고정 포트 UDP_PORT(8890)로 바인드해 실시간 수신."""
        if self._listener_running:
            return
        port = UDP_PORT
        with self._config_lock:
            self.config["port"] = port
        if not _pynput_installed():
            self._log(_t("log_pynput_missing"), error=True)
        self._listener_running = True
        self._listener_thread = threading.Thread(target=self._listener_worker, args=(port,),
                                                 daemon=True)
        self._listener_thread.start()
        self._set_listen_status("status_running", "#22c55e")
        self._log(_tf("log_listener_start", port))

    def _warmup_helper(self) -> None:
        """[FIX] 헬퍼 최초 콜드 시작 비용을 앱 시작 시점으로 옮긴다 (frozen 전용).

        onedir 헬퍼도 새 머신 최초 1회는 macOS 검증(~9초)이 필요하다. 이를 첫
        액션에서 지불하지 않도록, 시작 직후 백그라운드 데몬 스레드로 헬퍼를 한 번
        실행해 pynput/PyObjC import + macOS 검증을 미리 끝내 둔다. 실패는 조용히
        무시한다 (best-effort — 프리워밍이 실패해도 실제 액션 시 그대로 시도된다).
        """
        if not getattr(sys, "frozen", False):
            return   # 개발 모드: 헬퍼가 이미 빠르므로 불필요
        def _run():
            try:
                run_input_helper("shortcut", "")   # Controller 생성 + import만, 키 입력 없음
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _check_accessibility(self) -> None:
        """시작 시 macOS 손쉬운 사용(Accessibility) 권한 확인 (best-effort, 비동기).

        권한이 없으면 pynput 키 이벤트(CGEventPost)가 예외 없이 조용히 버려져
        단축키/텍스트가 아무 오류 없이 '동작하지 않는' 것처럼 보인다. 파이썬 실행은
        터미널 권한으로 동작하지만, .app은 고유 권한이 필요하고 PyInstaller 재빌드
        때마다 초기화될 수 있어 시작 시 확인해 경고로 띄워준다. 헬퍼 --trust 모드
        (exit 0=권한 있음, 3=없음)를 사용하고, 실패는 조용히 무시한다.

        [DIAG] 'self' = GUI 메인 프로세스(.app) 자신의 권한, 'helper' = 헬퍼
        서브프로세스의 권한. self=True & helper=False 면 헬퍼가 .app 권한을
        상속하지 못하는 구조 문제(H2)이고, 둘 다 False면 .app 자체가 미등록(H1,
        등록 경로/서명 불일치)이다. PyObjC(HIServices)를 GUI에 내장하지 않기 위해
        ctypes로 프레임워크를 직접 로드한다 (read-only 확인 — 크래시 격리 유지).
        """
        def _ax_self_trusted():
            """메인 프로세스 자신의 AXIsProcessTrusted (ctypes, PyObjC 불필요)."""
            if sys.platform != "darwin":
                return None
            try:
                import ctypes
                _h = ctypes.cdll.LoadLibrary(
                    "/System/Library/Frameworks/ApplicationServices.framework/"
                    "Frameworks/HIServices.framework/HIServices")
                _fn = _h.AXIsProcessTrusted
                _fn.restype = ctypes.c_bool
                _fn.argtypes = []
                return bool(_fn())
            except Exception:
                return None

        def _run():
            try:
                self_trusted = _ax_self_trusted()
                helper = _helper_executable()
                if not helper.exists():
                    self._debug_log("[accessibility] self=%s helper=missing" % self_trusted)
                    return
                argv = ([str(helper), "--trust"]
                        if getattr(sys, "frozen", False)
                        else [sys.executable, str(helper), "--trust"])
                proc = subprocess.run(argv, capture_output=True, timeout=30)
                helper_trusted = (proc.returncode == 0)
                self._debug_log("[accessibility] self=%s helper=%s rc=%s" %
                                (self_trusted, helper_trusted, proc.returncode))
                if not (self_trusted or helper_trusted):
                    self._event_queue.put(("log", _tf("warn_accessibility"), True))
            except Exception as e:
                self._debug_log("[accessibility] check failed: %s" % e)
        threading.Thread(target=_run, daemon=True).start()

    def _stop_listener(self) -> None:
        self._listener_running = False
        if self._listener_thread is not None:
            self._listener_thread.join(timeout=3.0)
            self._listener_thread = None
        self._set_listen_status("status_stopped", self.sub_text)
        self._log(_t("log_listener_stop"))

    def _listener_worker(self, port: int) -> None:
        """UDP 수신 리스너: 소켓 수명 전체를 이 스레드가 소유한다.

        bind → 시작 시 설정 1회 → 수신 루프(+하트비트/재전송 요청 처리) → finally: close.
        """
        # [DBG] 리스너 스레드 생명주기/예외를 파일로 기록 (윈도우 모드 앱은 stderr이 /dev/null)
        self._debug_log("[listener] start t=%s port=%d" % (time.time(), port))

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError as e:
            self._listener_running = False
            self._event_queue.put(("log", _tf("log_bind_fail", port, e), True))
            self._debug_log("[listener] BIND FAIL %s" % e)
            try:
                sock.close()
            except OSError:
                pass
            return
        sock.settimeout(2.0)   # 주기적 기상: 재전송/종료 폴링
        self._dump = None       # [H] 새 수신 사이클 시작 — 이전 덤프 상태 초기화

        with self._config_lock:
            ip = self.config.get("device_ip", "") or ""
        if _is_valid_ip(ip):
            # [H] 호스트 시작 시 무조건 푸시하지 않고, MREQ로 디바이스 설정을 읽어 호스트와
            #     비교 후 방향을 묻는다 (동기화 보호). IP를 아는 건 이전 연결 기록이므로
            #     _pushed_ip를 미리 세워 비콘이 이중 트리거하지 않게 한다.
            self._pushed_ip = ip
            self._dump_queue.put(("dump_start", ip, port, True))

        try:
            while self._listener_running:
                try:
                    data, addr = sock.recvfrom(4096)
                    magic = _magic_of(data)
                    if magic == MAGIC_BEACON:
                        self._handle_beacon(sock, addr, port)   # [H] ACK 회신 + 새 IP 첫 비콘만 전체 푸시
                    elif magic == MAGIC_CONFIG:
                        self._handle_config_dump_packet(sock, data, addr)   # [H] MREQ 덤프 응답
                    elif magic == MAGIC_IMAGE:
                        self._handle_image_dump_packet(sock, data, addr)    # [H] MREQ 이미지 덤프
                    elif magic == MAGIC_EVENT:
                        self._handle_event_packet(sock, data, addr)
                    else:
                        pass   # MPOK/MPER/기타 → 무시
                except socket.timeout:
                    pass
                except OSError:
                    break

                # Apply 재전송 요청 (메인 스레드에서 큐로 전달) — [H] 2회 재전송 (UDP 유실 보정)
                try:
                    self._resend_queue.get_nowait()
                    with self._config_lock:
                        ip = self.config.get("device_ip", "") or ""
                    if _is_valid_ip(ip):
                        self._pushed_ip = ip
                        self._push_config(sock, ip, port, retries=1, reason="Apply")
                except queue.Empty:
                    pass

                # [H] "디바이스에서 불러오기" 요청 → MREQ 전송 + 수집 시작 (리스너 스레드 전용 상태)
                try:
                    msg = self._dump_queue.get_nowait()
                    if msg[0] == "dump_start":
                        dip = msg[1]
                        dport = msg[2] if len(msg) > 2 else UDP_PORT
                        auto = msg[3] if len(msg) > 3 else False
                        # [FIX] MREQ 발송 실패(일시적 ARP/ICMP "No route to host" 등) 시 리스너가
                        #     죽지 않도록 _safe_sendto로 보호. 전송에 성공했을 때만 덤프를 수집하고,
                        #     실패하면 _pushed_ip를 풀어 다음 비콘이 자동 비교를 재시도한다
                        #     (안 풀면 "설정 비교에서 멈춤"이 되어 디바이스와 비교가 아예 안 된다).
                        sent = self._safe_sendto(sock, build_mreq_packet(), (dip, UDP_PORT))
                        if sent:
                            self._dump = {"pages": {}, "images": {}, "num_pages": None,
                                          "device_ip": dip, "device_port": dport, "auto": auto,
                                          "start": time.time(), "last_pkt": time.time()}
                            # [H] 경쟁 방지: 불러오기 직후 첫 비콘 푸시를 억제한다.
                            #     _pushed_ip가 None(아직 미발견)인 채로 불러오기를 하면, 덤프 완료로 호스트
                            #     config가 디바이스 데이터로 채워진 뒤 들어오는 첫 비콘이 "새 IP"로 오인돼
                            #     방금 불러온 설정을 디바이스로 재푸시("Apply 안 눌렀는데 적용")한다.
                            self._pushed_ip = dip
                            sock.settimeout(0.5)   # 수집 중 더 자주 기상해 quiet 판정
                        else:
                            self._pushed_ip = None   # [FIX] 다음 비콘이 자동 비교 재시도
                except queue.Empty:
                    pass

                # [H] 덤프 완료/타임아웃 판정
                self._maybe_finalize_dump(sock)
        except BaseException:
            # [DBG] 리스너 스레드 비정상 종료 → 예외를 파일로 남긴다
            try:
                with open(self._debug_log_path, "a", encoding="utf-8") as f:
                    f.write("[listener] CRASH t=%s\n" % time.time())
                    traceback.print_exc(file=f)
                    f.write("---\n")
            except Exception:
                pass
            raise
        finally:
            # [FIX #1] 소켓은 리스너 스레드에서만 닫는다. 메인 스레드는 절대 닫지 않음.
            self._debug_log("[listener] exit t=%s" % time.time())
            sock.close()
            self._log("리스너 스레드 종료", debug=True)

    def _handle_config_dump_packet(self, sock: socket.socket, data: bytes, addr) -> None:
        """[H] MREQ 응답의 MCFG 덤프 패킷 수집 (수집 중일 때만 — 자기 푸시는 되돌아오지 않음)."""
        if self._dump is not None and apply_mcfg_to_dump(self._dump, data):
            self._dump["last_pkt"] = time.time()

    def _handle_image_dump_packet(self, sock: socket.socket, data: bytes, addr) -> None:
        """[H] MREQ 응답의 MIMG 이미지 덤프 패킷 수집 (수집 중일 때만)."""
        if self._dump is not None and apply_mimg_to_dump(self._dump, data):
            self._dump["last_pkt"] = time.time()

    def _maybe_finalize_dump(self, sock: socket.socket) -> None:
        """[H] 덤프 수집 완료 판정: 모든 페이지 12버튼 완성 + 0.3s quiet, 또는 5s 데드라인."""
        d = self._dump
        if d is None:
            return
        now = time.time()
        complete = False
        if d["num_pages"] is not None:
            complete = (now - d["last_pkt"]) >= 0.3 and all(
                len(d["pages"].get(p, {}).get("buttons", {})) >= BUTTONS_PER_PAGE
                for p in range(d["num_pages"]))
        if complete or (now - d["start"]) >= 5.0:
            auto = d.get("auto", False)
            ip = d.get("device_ip", "")
            self._dump = None
            sock.settimeout(2.0)   # 정상 타임아웃 복원
            # [H] 진단: MREQ 덤프로 받은 이미지 패킷 수 — 0이면 디바이스 플래시에 이미지가
            #     없거나 유실된 것. build_config_from_dump 결과와 함께 로그로 남긴다.
            n_img = len(d.get("images") or {})
            config = build_config_from_dump(d)
            self._debug_log("[dump] auto=%s ip=%s num_pages=%s pages=%s imgs=%d complete=%s"
                            % (auto, ip, d.get("num_pages"),
                               sorted(d.get("pages", {}).keys()), n_img, complete))
            if config is None:
                if auto:
                    # [동기화 보호] 자동 비교 실패(오프라인 등)는 조용히 건너뛰고 _pushed_ip를
                    #     풀어 다음 비콘이 재시도하게 한다 (디바이스가 오프라인이면 비콘도 안 옴).
                    self._pushed_ip = None
                    self._event_queue.put(("auto_sync_fail", ip))
                else:
                    self._event_queue.put(("dump_fail", _t("log_dump_fail")))
            else:
                if auto:
                    self._event_queue.put(("auto_sync_ready",
                                           (config, n_img, ip, d.get("device_port", UDP_PORT))))
                else:
                    self._event_queue.put(("dump_ready", (config, n_img)))

    def _handle_event_packet(self, sock: socket.socket, data: bytes, addr) -> None:
        parsed = parse_event_packet(data)
        if parsed is None:
            return
        page, button, flags = parsed
        with self._config_lock:
            pages = self.config.get("pages") or []
        if not (0 <= page < len(pages)):
            return
        btns = pages[page].get("buttons", [])
        if not (0 <= button < len(btns)):
            return
        btn = btns[button]
        try:
            self._exec_action(btn)
            desc = self._action_desc(btn)
            self._event_queue.put((page, button,
                                   _tf("log_event_ok", page + 1, button, desc), False))
            self._send_feedback(sock, addr, page, button, True)    # [B] MPOK
        except Exception as e:
            self._event_queue.put((page, button,
                                   _tf("log_event_err", page + 1, button, e), True))
            self._send_feedback(sock, addr, page, button, False)   # [B] MPER

    def _send_feedback(self, sock: socket.socket, addr, page: int, button: int, ok: bool) -> None:
        """액션 실행 결과(성공=MPOK/실패=MPER)를 디바이스로 회신 — 버튼 플래시 피드백(B).

        디바이스는 항상 UDP_PORT(8890)에서 listen 중이므로, 이벤트 소스 IP + 고정 포트로 보낸다.
        (이벤트 패킷의 소스 포트는 WiFiUDP가 임의 할당하므로 회신 대상이 아니다.)
        """
        magic = MAGIC_OK if ok else MAGIC_ERR
        pkt = EVENT_HEADER.pack(magic, page, button, 0, 0)
        self._safe_sendto(sock, pkt, (addr[0], UDP_PORT))

    def _handle_beacon(self, sock: socket.socket, addr, port: int) -> None:
        """디스커버리 비콘("MPBE") 수신 — H 동기화 재설계.

        3초마다 전체 재푸시하던 것을 제거하고:
          (a) 호스트 IP·포트 학습용 **ACK**(MCFG count=0, ~12B) 유니캐스트 회신
              → 디바이스가 호스트 IP 변경을 재실행 없이 자가치유.
          (b) 새 디바이스 IP의 첫 비콘: **무조건 푸시하지 않고** MREQ로 디바이스 설정을
              읽어 호스트와 비교 후 방향을 묻는다 (동기화 보호 — 빈 호스트가 디바이스를
              지우는 것 방지). 비교·질문은 메인 스레드, 1회는 _pushed_ip 가드 (세션당 1회).
        """
        device_ip = addr[0]
        is_new = False
        with self._config_lock:
            cur = self.config.get("device_ip", "") or ""
            if device_ip != cur:
                is_new = True
                self.config["device_ip"] = device_ip
                self.config["port"] = port
        if is_new:
            self._event_queue.put(("ip", device_ip,
                                   _tf("log_device_found", device_ip)))
        # (a) ACK: 디바이스가 소스 IP/포트만 학습 (count=0 → 상태 변화 없음)
        with self._config_lock:
            num_pages = len(self.config.get("pages") or [])
        self._safe_sendto(sock, build_ack_packet(num_pages), (device_ip, UDP_PORT))
        # (b) 새 디바이스 IP의 첫 비콘: 자동 비교 덤프 시작 (재비콘 재트리거는 _pushed_ip로 방지)
        if device_ip != self._pushed_ip:
            self._pushed_ip = device_ip
            self._dump_queue.put(("dump_start", device_ip, port, True))

    def _apply_detected_ip(self, ip: str, msg: str) -> None:
        """자동 검색된 디바이스를 상태바에 반영 (메인 스레드)."""
        self._log(msg)
        port = int(self.config.get("port", UDP_PORT) or UDP_PORT)
        self._set_listen_status("status_found", "#22c55e", ip, port)

    def _action_desc(self, btn: dict) -> str:
        atype = btn.get("action_type", "shortcut")
        aval = btn.get("action_value", "")
        if atype == "text":
            return _tf("desc_text", aval[:20])
        if atype == "app":
            return _tf("desc_app", aval)
        return _tf("desc_shortcut", aval)

    # ------------------------------------------------------------------
    # 액션 실행기
    # ------------------------------------------------------------------
    def _exec_action(self, btn: dict) -> None:
        atype = btn.get("action_type", "shortcut")
        aval = btn.get("action_value", "")
        if atype == "shortcut":
            self._exec_shortcut(aval)
        elif atype == "text":
            self._exec_text(aval)
        elif atype == "app":
            self._exec_app(aval)

    def _exec_shortcut(self, s: str) -> None:
        if not s.strip():
            return
        if not _pynput_installed():
            raise RuntimeError(_t("err_pynput_missing"))
        run_input_helper("shortcut", s)   # 격리된 서브프로세스에서 실행

    def _exec_text(self, text: str) -> None:
        if not text:
            return
        if not _pynput_installed():
            raise RuntimeError(_t("err_pynput_missing"))
        # 클립보드 + 붙여넣기(맥 Cmd+V / 윈도우 Ctrl+V): IME 상태와 무관하게 한글 포함 텍스트 입력
        run_input_helper("text", text)    # 격리된 서브프로세스에서 실행

    def _exec_app(self, value: str) -> None:
        value = value.strip()
        if not value:
            return
        if sys.platform == "win32":
            # Windows: os.startfile — URL이면 기본 브라우저, 파일이면 연결된 프로그램으로 연다.
            # "calc"처럼 확장자가 없는 실행 이름은 .exe를 붙여 PATH에서 재탐색한다.
            try:
                os.startfile(value)
            except OSError:
                if "://" not in value and "." not in os.path.basename(value):
                    try:
                        os.startfile(value + ".exe")
                    except OSError as e:
                        raise RuntimeError(_tf("err_app_os", e))
                else:
                    raise RuntimeError(_tf("err_app_os", e))
            return
        if "://" in value:
            cmd = ["open", value]                    # URL
        else:
            cmd = ["open", "-a", value]              # 앱 번들 (Finder 영문 이름)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            raise RuntimeError(_t("err_app_timeout"))
        except OSError as e:
            raise RuntimeError(_tf("err_app_os", e))
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(_tf("err_app_fail", err or _t("err_app_open")))

    # ------------------------------------------------------------------
    # UI 로그 + 이벤트 폴링
    # ------------------------------------------------------------------
    def _log(self, msg: str, error: bool = False, debug: bool = False) -> None:
        if debug:
            logging.debug(msg)
            return
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, "[%s] %s\n" % (time.strftime("%H:%M:%S"), msg),
                             "error" if error else None)
        # 로그 길이 제한 (최근 ~200줄)
        if int(self.log_text.index("end-1c").split(".")[0]) > 200:
            self.log_text.delete("1.0", "50.0")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _poll_event_queue(self) -> None:
        # [FIX #3] after 콜백이 창 닫힘 뒤에도 실행되면 예외 → 무시
        try:
            while True:
                item = self._event_queue.get_nowait()
                if item[0] == "ip":
                    _, ip, msg = item
                    self._apply_detected_ip(ip, msg)
                elif item[0] == "log":
                    _, msg, is_error = item
                    self._log(msg, error=is_error)
                elif item[0] == "dump_ready":   # [H] 디바이스 덤프 수신 완료 (config, 수신 이미지 수)
                    _, (config, n_img) = item   # [FIX] 생산자는 ("dump_ready", (config, n_img)) 중첩 튜플
                    self._apply_dump_config(config, n_img)
                elif item[0] == "auto_sync_ready":  # [동기화 보호] 첫 연결 자동 비교 완료
                    _, (config, n_img, ip, port) = item
                    self._handle_auto_sync(config, n_img, ip, port)
                elif item[0] == "auto_sync_fail":   # [동기화 보호] 자동 비교 실패 (로그만)
                    _, ip = item
                    self._log(_tf("log_autosync_fail", ip), error=True)
                elif item[0] == "dump_fail":    # [H] 덤프 수집 실패/타임아웃
                    _, msg = item
                    self._log(msg, error=True)
                    messagebox.showerror(_t("msg_dumpfail_title"), msg)
                else:
                    page, button, msg, is_error = item
                    self._log(msg, error=is_error)
        except queue.Empty:
            pass
        except tk.TclError:
            return
        self.root.after(200, self._poll_event_queue)

    # ------------------------------------------------------------------
    # 종료
    # ------------------------------------------------------------------
    def on_close(self) -> None:
        self._listener_running = False
        if self._listener_thread is not None and self._listener_thread.is_alive():
            self._listener_thread.join(timeout=3.0)
        self.root.destroy()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    root = tk.Tk()
    app = MacroPadGUI(root)
    root.mainloop()


if __name__ == "__main__":
    if "--test-packets" in sys.argv:
        # 오프라인 패킷 구성/파싱 자가검증 (GUI 없이 실행 가능)
        assert struct.calcsize(">IBBBB") == 8, "헤더는 8바이트여야 함"
        btns = [{"label": "Copy", "action_type": "shortcut", "action_value": "cmd+c", "color": 0},
                {"label": "안녕", "action_type": "text", "action_value": "x", "color": 7},
                {"label": "", "action_type": "app", "action_value": "", "color": 2}] + \
               [{"label": "", "action_type": "shortcut", "action_value": "", "color": 0}] * 9
        pkt = build_config_packet(0, btns, 3)   # page_name 기본 "" → name_len 0
        assert pkt[:8] == struct.pack(">IBBBB", MAGIC_CONFIG, 0, 12, 3, 0)  # num_pages=3
        assert pkt[8] == 0                       # [A/H] page_name_len == 0 (이름 없음)
        # v3 엔트리 >BBBBB(bid, label_len, color_idx, action_type, action_len) + label + action_value
        # len = 헤더8 + name_len1 + Σ(5+label+aval): btn0 14B, btn1 12B, btn2 5B, 빈 9개 45B = 85
        assert len(pkt) == 85, "v3 패킷 길이 %d ≠ 85" % len(pkt)
        magic, page, count, num_pages, r2 = CONFIG_HEADER.unpack(pkt[:8])
        assert magic == MAGIC_CONFIG and page == 0 and count == 12 and num_pages == 3
        # layout: [8B헤더][name_len 1B][bid0 llen0 col0 atype0 alen5]["Copy" 4B]["cmd+c" 5B]
        #          [bid1 llen1 col1 atype1 alen1]["안녕" 6B]["x" 1B][bid2 llen0 col2 atype2 alen0]...
        assert pkt[9:14] == bytes([0, 4, 0, 0, 5])     # bid0 "Copy" shortcut "cmd+c" color 0
        assert pkt[14:18] == b"Copy"
        assert pkt[18:23] == b"cmd+c"
        assert pkt[23:28] == bytes([1, 6, 7, 1, 1])    # bid1 "안녕"(6B) text "x" color 7
        assert pkt[28:34] == "안녕".encode("utf-8")
        assert pkt[34:35] == b"x"
        assert pkt[35:40] == bytes([2, 0, 2, 2, 0])    # bid2 빈 라벨 app 빈 값 color 2
        # [A] page_name이 있으면 name_len+name 뒤에 엔트리가 온다
        pkt2 = build_config_packet(1, btns[:2], 3, "Page 2")
        assert pkt2[8] == 6 and pkt2[9:15] == b"Page 2"
        assert pkt2[15:20] == bytes([0, 4, 0, 0, 5])   # page_name 뒤 v3 엔트리 시작
        # [H] ACK = MCFG count=0 (9B) — 소스 IP/포트 학습용
        ack = build_ack_packet(3)
        assert len(ack) == 9 and ack == CONFIG_HEADER.pack(MAGIC_CONFIG, 0, 0, 3, 0) + b"\x00"
        # [H] MREQ = 8B 요청 패킷
        mreq = build_mreq_packet()
        assert len(mreq) == 8 and mreq == REQUEST_HEADER.pack(MAGIC_REQUEST, 0, 0, 0, 0)
        assert _magic_of(mreq) == MAGIC_REQUEST and _magic_of(b"\x00" * 2) is None
        # 라벨 24바이트 초과 절단 검증 (멀티바이트 비분할)
        long_label = _trunc_utf8("가" * 30, LABEL_MAX)
        assert len(long_label) <= LABEL_MAX and long_label.decode("utf-8").endswith("가")
        # 이벤트 파싱
        evt = struct.pack(">IBBBB", MAGIC_EVENT, 1, 5, 0, 0)
        assert parse_event_packet(evt) == (1, 5, 0)
        # [B] 피드백 패킷 (MPOK/MPER) 구성 검증
        ok_pkt = EVENT_HEADER.pack(MAGIC_OK, 1, 5, 0, 0)
        err_pkt = EVENT_HEADER.pack(MAGIC_ERR, 1, 5, 0, 0)
        assert ok_pkt[:4] == b"MPOK" and ok_pkt[4] == 1 and ok_pkt[5] == 5
        assert err_pkt[:4] == b"MPER" and err_pkt[4] == 1 and err_pkt[5] == 5
        assert parse_event_packet(evt[:7]) is None          # 짧은 패킷
        bad = struct.pack(">IBBBB", 0xDEADBEEF, 0, 0, 0, 0)  # 잘못된 magic
        assert parse_event_packet(bad) is None
        # [G] 이미지 패킷(MIMG) 구성 검증
        img_pkt = build_image_packet(0, 3, b"\xff\xd8\xff\xe0" + b"\x00" * 20)
        assert len(img_pkt) == 8 + 24                     # 헤더8 + JPEG 24B
        assert img_pkt[:8] == IMAGE_HEADER.pack(MAGIC_IMAGE, 0, 3, 0, 0)
        assert img_pkt[:4] == b"MIMG"
        m2, p2, b2, f2, _ = IMAGE_HEADER.unpack(img_pkt[:8])
        assert (m2, p2, b2, f2) == (MAGIC_IMAGE, 0, 3, 0)
        assert img_pkt[8:] == b"\xff\xd8\xff\xe0" + b"\x00" * 20   # JPEG 페이로드 그대로
        # [G] clear 패킷(fmt=1): 페이로드 없음 — 디바이스가 펌웨어 텍스트/색 사각형으로 폴백
        clear_pkt = build_image_packet(1, 7, b"", 1)
        assert len(clear_pkt) == 8                                  # 헤더만
        assert clear_pkt[:8] == IMAGE_HEADER.pack(MAGIC_IMAGE, 1, 7, 1, 0)
        cm, cp, cb, cf, _ = IMAGE_HEADER.unpack(clear_pkt[:8])
        assert (cm, cp, cb, cf) == (MAGIC_IMAGE, 1, 7, 1)           # page=1 bid=7 fmt=1
        # [G] 실제 렌더 크기가 총 상한(≤IMG_MAX_BYTES)에 들어가는지 검증 (Pillow 필요).
        #     MIN_QUALITY=70 보장 → 일부 이미지는 1400B를 넘을 수 있고 fmt=2 청킹으로 전송된다.
        img_ok = True
        try:
            _ensure_pillow()
        except RuntimeError:
            img_ok = False
            print("SKIP: Pillow 미설치 — 이미지 렌더 크기 검증 생략")
        if img_ok:
            for lbl, ci in [("Copy", 0), ("안녕하세요", 7), ("스크린샷 및 기록", 9),
                            ("멀티라인 긴 버튼 이름 테스트 문구", 4), ("Open Safari", 6)]:
                jpg = _render_button_image(lbl, ci)
                assert jpg[:2] == b"\xff\xd8", "JPEG 마커 확인: %r" % lbl
                assert len(jpg) <= IMG_MAX_BYTES, \
                    "JPEG %dB > %dB (fmt=2 청킹 상한 초과): %r" % (len(jpg), IMG_MAX_BYTES, lbl)
                assert all(len(p) <= 8 + 8 + IMG_CHUNK_DATA for p in build_image_packets(0, 0, jpg)), \
                    "모든 전송 패킷 ≤1416B (단편화 없음): %r" % lbl
            # [PLAN 8] 이모지 라벨도 텍스트/이모지 런 렌더 → 총 상한 이내
            for lbl, ci in [("🎉", 6), ("안녕 🎉", 6), ("Copy 🐱 👨‍👩‍👧", 6)]:
                jpg = _render_button_image(lbl, ci)
                assert jpg[:2] == b"\xff\xd8", "이모지 JPEG 마커 확인: %r" % lbl
                assert len(jpg) <= IMG_MAX_BYTES, \
                    "이모지 JPEG %dB > %dB (fmt=2 청킹 상한 초과): %r" % (len(jpg), IMG_MAX_BYTES, lbl)
            # [G] 업로드 이미지 경로: 임의 크기 이미지 → 중앙 크롭 채움 71x61 JPEG → base64 왕복
            src = Image.new("RGB", (200, 100), (200, 30, 30))
            up_jpg = _image_to_button_jpeg(src)
            assert up_jpg[:2] == b"\xff\xd8", "업로드 이미지 JPEG 마커 확인"
            assert len(up_jpg) <= IMG_MAX_BYTES, \
                "업로드 JPEG %dB > %dB (fmt=2 청킹 상한 초과)" % (len(up_jpg), IMG_MAX_BYTES)
            up_b64 = base64.b64encode(up_jpg).decode("ascii")
            assert base64.b64decode(up_b64) == up_jpg               # config 저장/전송 왕복
            reopened = Image.open(io.BytesIO(up_jpg))
            assert reopened.size == (BTN_IMG_W, BTN_IMG_H), \
                "중앙 크롭 채움 결과 크기 %r ≠ 71x61" % (reopened.size,)
            # [PLAN 7] 업로드 이미지도 라운드 코너: 네 귀퉁이 픽셀은 그리드 배경색이어야 한다
            # (JPEG 손실 압축 때문에 정확 등가가 아니라 허용 오차로 비교. 코너 픽셀은
            #  DCT 블록 경계에서 배경↔채움 급경계 링잉으로 최대 ~30 벗어날 수 있음 —
            #  실제로는 어두운 배경색으로 보이고 디바이스 roundButtonCorners가 정확히 덮음.
            #  반면 라운드 미적용(채움색 그대로)이면 팔레트 최저 97 이상 벗어나므로 ±45로 구분된다)
            bg = tuple(int(GRID_BG_HEX.lstrip("#")[i:i+2], 16) for i in (0, 2, 4))
            reopen_rgb = reopened.convert("RGB")
            for cx, cy in ((0, 0), (BTN_IMG_W - 1, 0), (0, BTN_IMG_H - 1),
                           (BTN_IMG_W - 1, BTN_IMG_H - 1)):
                px = reopen_rgb.getpixel((cx, cy))
                assert all(abs(a - b) <= 45 for a, b in zip(px, bg)), \
                    "모서리 (%d,%d) %r이 그리드 배경 %s에서 너무 벗어남 (라운드 코너 미적용)" \
                    % (cx, cy, px, GRID_BG_HEX)
            # 중앙은 버튼 채움색(원본 빨강 200,30,30)이어야 한다 — 라운드 마스크가 전체를 가리지 않았는지
            center = reopen_rgb.getpixel((BTN_IMG_W // 2, BTN_IMG_H // 2))
            assert all(abs(a - b) <= 24 for a, b in zip(center, (200, 30, 30))), \
                "중앙 %r이 원본 채움색과 다름 — 라운드 마스크가 이미지를 가림" % (center,)
        # [H] 청크 분할: 최악 엔트리(24B 라벨 + 128B 액션) 12개 = 1893B > CHUNK_MAX → 2청크
        fat_btns = [{"label": "L" * LABEL_MAX, "action_type": "shortcut",
                     "action_value": "v" * ACTION_VAL_MAX, "color": 3}] * 12
        chunks = chunk_config_packets(0, fat_btns, 8, "Fat")
        assert len(chunks) >= 2, "최악 페이지가 청크 분할되어야 함 (chunks=%d)" % len(chunks)
        assert all(len(c) <= CHUNK_MAX for c in chunks), "모든 청크 ≤ CHUNK_MAX"
        for i, c in enumerate(chunks):
            hdr = CONFIG_HEADER.unpack(c[:8])
            assert hdr[3] == 8                       # num_pages는 모든 청크에 반복
            assert c[8] == (len(b"Fat") if i == 0 else 0)   # page_name은 첫 청크에만
        bids = []
        for c in chunks:
            off, nl = 9, c[8]
            off += nl
            m, page, cnt, np_, _ = CONFIG_HEADER.unpack(c[:8])
            for _ in range(cnt):
                bids.append(c[off]); off += 5 + c[off + 1] + c[off + 4]
        assert sorted(bids) == list(range(12)), "청크들에서 bid 0..11 전부 등장"
        # [H] 덤프 왕복: chunk_config_packets → apply_mcfg_to_dump → build_config_from_dump
        dump = {"pages": {}, "images": {}, "num_pages": None, "device_ip": "10.0.0.5",
                "start": 0, "last_pkt": 0}
        for c in chunk_config_packets(0, btns, 3, "P0") + chunk_config_packets(1, btns, 3, "P1") \
                 + chunk_config_packets(2, btns, 3, ""):
            assert apply_mcfg_to_dump(dump, c)
        dump_img = build_image_packet(0, 3, b"\xff\xd8\xff\xe0" + b"\x00" * 20)
        assert apply_mimg_to_dump(dump, dump_img)          # fmt=0 이미지 덤프 병합
        cfg = build_config_from_dump(dump)
        assert cfg is not None and len(cfg["pages"]) == 3, "3페이지 덤프 완성"
        assert cfg["pages"][0]["name"] == "P0"
        assert cfg["pages"][0]["buttons"][0]["label"] == "Copy"
        assert cfg["pages"][0]["buttons"][0]["action_value"] == "cmd+c"
        assert cfg["pages"][0]["buttons"][1]["action_type"] == "text"
        assert cfg["pages"][0]["buttons"][3]["image"] == \
            base64.b64encode(dump_img[8:]).decode("ascii")  # 이미지가 config에 임베드
        assert cfg["pages"][2]["name"] == "Page 3"          # 이름 없음 → 기본명
        # [G] MIMG fmt=2 청킹 왕복: >1400B 이미지 → build_image_packets 분할 → apply_mimg_to_dump 재조립
        big_jpeg = b"\xff\xd8\xff\xe0" + bytes((i * 7) & 0xFF for i in range(JPEG_MAX_BYTES + 100))
        pkts = build_image_packets(0, 5, big_jpeg)
        assert len(pkts) == 2, "1500B → 2청크 (chunks=%d)" % len(pkts)
        for p in pkts:
            m, pg, bd, f, _ = IMAGE_HEADER.unpack(p[:8])
            assert (m, pg, bd, f) == (MAGIC_IMAGE, 0, 5, 2), "fmt=2 헤더 검증"
            assert len(p) <= 8 + 8 + IMG_CHUNK_DATA, "fmt=2 데이터그램 ≤1416B (단편화 없음)"
        dump3 = {"pages": {}, "images": {}, "num_pages": None, "device_ip": "", "start": 0, "last_pkt": 0}
        for p in pkts:
            assert apply_mimg_to_dump(dump3, p), "fmt=2 덤프 청크 적용"
        assert (0, 5) in dump3["images"], "전체 청크 도착 → images에 완성 저장"
        assert base64.b64decode(dump3["images"][(0, 5)]) == big_jpeg, "청킹 왕복 바이트 일치"
        #  비순서 도착 + 중복 청크 무시
        dump4 = {"pages": {}, "images": {}, "num_pages": None, "device_ip": "", "start": 0, "last_pkt": 0}
        for p in [pkts[1], pkts[0], pkts[1]]:               # 역순 + 중복
            assert apply_mimg_to_dump(dump4, p)
        assert base64.b64decode(dump4["images"][(0, 5)]) == big_jpeg, "비순서/중복 재조립 일치"
        #  ≤1400B는 fmt=0 단일 유지 (하위호환)
        small = b"\xff\xd8" + b"\x00" * 20
        assert build_image_packets(0, 6, small) == [build_image_packet(0, 6, small, 0)], \
            "≤JPEG_MAX_BYTES는 fmt=0 단일 유지"
        # [H] 불완전 덤프(12버튼 미달) → None
        dump2 = {"pages": {0: {"name": "", "buttons": {}}}, "images": {}, "num_pages": 1,
                 "device_ip": "", "start": 0, "last_pkt": 0}
        for c in chunk_config_packets(0, btns[:3], 1, ""):
            assert apply_mcfg_to_dump(dump2, c)
        assert build_config_from_dump(dump2) is None, "3/12버튼 덤프는 불완전"
        print("OK: 패킷 구성/파싱 검증 통과 (헤더 8B, magic MCFG/MPAD/MIMG/MREQ, v3 엔트리 >BBBBB+액션, num_pages, ACK count=0, 청크 분할, 라벨 절단, 이벤트 파싱, 이미지 크기 ≤%dB, clear fmt=1, fmt=2 청킹 왕복, 업로드 변환, 덤프 왕복)" % IMG_MAX_BYTES)
        sys.exit(0)
    if "--test-action" in sys.argv:
        # 오프라인 액션 검증: 격리 헬퍼로 키보드 입력이 실제 동작하는지 확인 (디바이스 불필요)
        try:
            idx = sys.argv.index("--test-action")
            atype, value = sys.argv[idx + 1], sys.argv[idx + 2]
        except IndexError:
            print("사용법: macro_pad_gui.py --test-action shortcut|text <값>")
            sys.exit(2)
        if atype not in ("shortcut", "text"):
            print("지원하는 액션: shortcut | text")
            sys.exit(2)
        try:
            run_input_helper(atype, value)
            print('OK: %s 액션 실행 성공 ("%s")' % (atype, value))
        except RuntimeError as e:
            print("FAIL: %s" % e)
            sys.exit(1)
        sys.exit(0)
    main()

