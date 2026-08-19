#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CYD 무선 매크로 패드 호스트 (Wireless Macro Pad Host)

CYD(ESP32-2432S028, 2.8" 터치 LCD)의 버튼 그리드를 터치하면 디바이스가 UDP 이벤트를
보내고, 이 호스트가 이를 수신해 액션을 실행한다.
  - shortcut : 키보드 단축키 (예: cmd+shift+4)          → 격리된 헬퍼(_input_helper.py)
  - text     : 문구/텍스트 입력 (한글 포함, IME 무관)     → 격리된 헬퍼(pbcopy + Cmd+V)
  - app      : 앱 실행 또는 URL 열기                     → open -a / open

실행:
    pip install -r requirements.txt
    python3 macro_pad_gui.py

macOS 접근성 권한 (키보드 입력에 필수):
    시스템설정 → 개인정보 보호 및 보안 → 손쉬운 사용 → 터미널(또는 Python) 체크.
    권한이 없으면 키보드/문구 액션은 조용히 무시된다.

크래시 격리 (중요):
    pynput(Quartz CGEventPost)은 네이티브 코드라 드물게 프로세스를 통째로 죽일 수 있다.
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
import socket
import struct
import subprocess
import sys
import threading
import time
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
COLOR_NAMES = ["slate", "red", "orange", "yellow", "green",
               "teal", "blue", "purple", "pink", "white"]
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
JPEG_QUALITY = 85             # 기본 품질 (작은 텍스트 아티팩트 번짐 보정 — PLAN q85~90)
JPEG_MIN_QUALITY = 45         # 1400B 초과 시 낮출 최저 품질
JPEG_MAX_BYTES = 1400         # 이미지 페이로드 상한 (헤더 8B + 1400B = 1408 < UDP MTU 1472)
GRID_BG_HEX = "#0F172A"       # 버튼 주변 그리드 배경색 (이미지 모서리와 동일 → 이음새 제거)
BTN_BORDER_HEX = "#64748B"    # 비활성 버튼 테두리 (슬레이트 — 펌웨어 색상과 일치)

ACTION_TYPES = ["shortcut", "text", "app"]

# [H] 프로토콜 v3: 액션/덤프/ACK (펌웨어와 정확히 일치해야 함)
MAGIC_REQUEST = 0x4D524551    # "MREQ" 설정 덤프 요청 (호스트→디바이스)
ACTION_VAL_MAX = 128          # 액션 값(action_value) 최대 바이트 — 펌웨어 ACTION_VAL_MAX와 일치
ATYPE_TO_IDX = {"shortcut": 0, "text": 1, "app": 2}   # v3 MCFG 엔트리 action_type
ATYPE_FROM_IDX = ["shortcut", "text", "app"]
REQUEST_HEADER = struct.Struct(">IBBBB")     # MREQ: magic + 4×u8
CHUNK_MAX = 1200              # MCFG 청크 상한 — 펌웨어 MCFG_CHUNK_MAX와 일치 (UDP 단일 패킷 안전)


def _pynput_installed() -> bool:
    """pynput 존재 여부. import하지 않고 spec만 확인한다 (네이티브 import는 헬퍼에서만)."""
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
    helper = Path(__file__).resolve().parent / "_input_helper.py"
    try:
        proc = subprocess.run(
            [sys.executable, str(helper), "--input"],
            input=json.dumps({"type": action_type, "value": value}).encode("utf-8"),
            capture_output=True, timeout=10)
    except subprocess.TimeoutExpired:
        raise RuntimeError("입력 실행 시간 초과 (10s)")
    except OSError as e:
        raise RuntimeError("입력 헬퍼 실행 오류: %s" % e)
    if proc.returncode != 0:
        if proc.returncode < 0:
            raise RuntimeError("입력 헬퍼가 시그널 %d로 종료됨 (pynput 크래시?)" % (-proc.returncode))
        err = proc.stderr.decode("utf-8", "replace").strip()
        if err.startswith("ERROR: "):
            err = err[7:]
        raise RuntimeError("입력 실행 실패: %s" % (err or "알 수 없는 오류"))


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
        if not (0 <= color < len(COLOR_NAMES)):
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
        raise RuntimeError(
            "Pillow 미설치 — 버튼 이미지 기능에 필요합니다 (pip install -r requirements.txt)") from e
    _PIL_READY = True


_FONT_CACHE = {}


def _load_label_font(size: int):
    """라벨 렌더용 폰트. macOS/Windows 시스템 폰트를 우선, 없으면 기본 폰트.
    결과는 크기별로 캐시 (한글 포함 — F에서 같은 경로 사용)."""
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    _ensure_pillow()
    font = None
    if sys.platform == "darwin":
        candidates = ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
                      "/System/Library/Fonts/AppleSDGothicNeo.ttc",
                      "/System/Library/Fonts/Helvetica.ttc")
    else:
        candidates = ("C:/Windows/Fonts/malgun.ttf",
                      "C:/Windows/Fonts/segoeui.ttf",
                      "C:/Windows/Fonts/arial.ttf")
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
    _FONT_CACHE[size] = font
    return font


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
    _ensure_pillow()
    color_hex = COLOR_HEX[color_idx]
    text_hex = "#0f172a" if color_idx == 9 else "#ffffff"   # 흰색 배경 → 검정 글자

    img = Image.new("RGB", (BTN_IMG_W, BTN_IMG_H), GRID_BG_HEX)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, BTN_IMG_W - 1, BTN_IMG_H - 1], radius=6, fill=color_hex)

    max_w, max_h = BTN_IMG_W - 10, BTN_IMG_H - 10
    words = label.split()
    for fs in range(15, 7, -1):
        font = _load_label_font(fs)
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
    font = _load_label_font(7)
    short = label if len(label) <= 7 else label[:6] + "…"
    bbox = d.textbbox((0, 0), short, font=font)
    d.text(((BTN_IMG_W - (bbox[2] - bbox[0])) / 2 - bbox[0],
            (BTN_IMG_H - (bbox[3] - bbox[1])) / 2 - bbox[1]), short, font=font, fill=text_hex)
    return img


def _jpeg_fit(img) -> bytes:
    """JPEG 인코딩. JPEG_MAX_BYTES 초과 시 품질을 낮춰 맞춘다 (단일 패킷 보장)."""
    q = JPEG_QUALITY
    while q >= JPEG_MIN_QUALITY:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=q, optimize=True)
        data = buf.getvalue()
        if len(data) <= JPEG_MAX_BYTES:
            return data
        q -= 10
    return data   # 최저 품질에도 초과 → 호출부에서 라벨을 줄여 재구성


def _render_button_image(label: str, color_idx: int) -> bytes:
    """라벨/색상 → JPEG 바이트(≤JPEG_MAX_BYTES). 극단적으로 큰 라벨은 축약 후 재구성."""
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
    """
    _ensure_pillow()
    ratio = max(BTN_IMG_W / img.width, BTN_IMG_H / img.height)
    w, h = max(1, round(img.width * ratio)), max(1, round(img.height * ratio))
    img = img.resize((w, h), Image.LANCZOS)
    left = (w - BTN_IMG_W) // 2
    top = (h - BTN_IMG_H) // 2
    img = img.crop((left, top, left + BTN_IMG_W, top + BTN_IMG_H))
    return _jpeg_fit(img)


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
    """MREQ 응답의 MIMG 덤프 패킷(fmt=0, 이미지 버튼만)을 base64로 저장."""
    if len(data) < 8:
        return False
    magic, page, bid, fmt, rsvd = IMAGE_HEADER.unpack(data[:8])
    if magic != MAGIC_IMAGE or fmt != 0:
        return False
    dump["images"][(page, bid)] = base64.b64encode(data[8:]).decode("ascii")
    return True


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


class MacroPadGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("CYD Wireless Macro Pad Host")
        # 창 크기: 화면 높이보다 크면 하단 적용/내보내기/가져오기 버튼이 잘린다.
        # 화면에 맞게 높이를 제한하고, 리사이즈를 허용해 사용자가 조절할 수 있게 한다.
        w, h = 520, 820
        sh = self.root.winfo_screenheight()
        if h > sh - 120:                       # 메뉴바/독 여유를 뺀 가용 높이
            h = max(480, sh - 120)
        self.root.geometry("%dx%d" % (w, h))
        self.root.resizable(True, True)
        self.root.minsize(480, 420)

        self.config_path = Path(__file__).resolve().parent / "macro_config.json"
        self.config = self._load_config()          # dict (런타임 스냅샷: 메인/리스너가 lock으로 읽음)

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

        # [H] 동기화 재설계 상태
        self._pushed_ip: str | None = None         # 이번 세션에서 전체 푸시한 디바이스 IP (1회 푸시 판정)
        self._dump = None                           # 리스너 스레드 전용 MREQ 덤프 수집 상태 (None=미수집)
        self._dump_queue: "queue.Queue[tuple]" = queue.Queue()   # 메인→리스너: ("dump_start", ip)

        self.setup_ui_style()
        self.create_widgets()
        self._populate_from_config()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._start_listener()                     # 리스너 자동 시작 (고정 포트 8890)
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
                if not (0 <= color < len(COLOR_NAMES)):
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
        return {
            "version": 3,
            "port": int(config.get("port", UDP_PORT) or UDP_PORT),
            "device_ip": str(config.get("device_ip", "") or ""),
            "pages": norm,
        }

    def _save_config(self, config: dict) -> None:
        self.config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

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

    # ------------------------------------------------------------------
    # 위젯 구성
    # ------------------------------------------------------------------
    def create_widgets(self) -> None:
        # 헤더
        header_frame = tk.Frame(self.root, bg=self.bg_color, pady=8)
        header_frame.pack(fill=tk.X, padx=16)
        ttk.Label(header_frame, text="⌨️ CYD Wireless Macro Pad Host",
                  style="Header.TLabel").pack(anchor="w")
        tk.Label(header_frame, text="ESP32-2432S028 터치 버튼 → UDP 이벤트 → 매크로 실행",
                 bg=self.bg_color, fg=self.sub_text, font=("Pretendard", 9)).pack(anchor="w")

        # 리스너 상태 카드 (IP/포트/시작 버튼 없음 — 리스너는 자동 시작)
        status_card = tk.Frame(self.root, bg=self.card_bg, bd=1, relief=tk.SOLID, padx=12, pady=8)
        status_card.pack(fill=tk.X, padx=16, pady=(0, 8))
        self.listen_status = tk.Label(status_card, text="● 리스너 시작 중...", bg=self.card_bg,
                                      fg=self.sub_text, font=("Pretendard", 9), anchor="w")
        self.listen_status.pack(fill=tk.X)

        # 페이지 관리 행 (이름 편집 + 추가/삭제)
        page_row = tk.Frame(self.root, bg=self.card_bg, bd=1, relief=tk.SOLID, padx=12, pady=8)
        page_row.pack(fill=tk.X, padx=16, pady=(0, 8))
        ttk.Label(page_row, text="페이지 이름:").pack(side=tk.LEFT)
        self.page_name_entry = ttk.Entry(page_row, width=18)
        self.page_name_entry.pack(side=tk.LEFT, padx=6)
        self.page_name_entry.bind("<KeyRelease>", lambda e: self._page_name_edited())
        self.del_page_btn = tk.Button(page_row, text="− 페이지", command=self._del_page,
                                      bg="#ef4444", fg="white", relief=tk.FLAT, padx=10, pady=4,
                                      cursor="pointinghand", font=("Pretendard", 9, "bold"))
        self.del_page_btn.pack(side=tk.RIGHT, padx=(0, 6))
        self.add_page_btn = tk.Button(page_row, text="+ 페이지", command=self._add_page,
                                      bg="#10b981", fg="white", relief=tk.FLAT, padx=10, pady=4,
                                      cursor="pointinghand", font=("Pretendard", 9, "bold"))
        self.add_page_btn.pack(side=tk.RIGHT)

        # 페이지 노트북 (최대 MAX_PAGES × 4×3 버튼) — pack은 하단 바를 먼저 고정한 뒤 진행
        self.notebook = ttk.Notebook(self.root)
        self.notebook.bind("<<NotebookTabChanged>>", self._page_changed)
        self._button_widgets = []
        self._page_tabs = []

        # 하단 카드: 적용 / 내보내기 / 가져오기 + 이벤트 로그 — side=BOTTOM으로 먼저 pack해서
        # 창이 짧아져도 하단 버튼이 항상 보이게 고정 (노트북이 남은 공간을 흡수)
        bot_card = tk.Frame(self.root, bg=self.card_bg, bd=1, relief=tk.SOLID, padx=12, pady=10)
        bot_card.pack(side=tk.BOTTOM, fill=tk.X, padx=16, pady=(0, 8))
        bot_row = tk.Frame(bot_card, bg=self.card_bg)
        bot_row.pack(fill=tk.X)
        self.apply_btn = tk.Button(bot_row, text="💾 설정 적용 (Apply)", command=self._apply,
                                   bg="#0ea5e9", fg="white", relief=tk.FLAT, padx=14, pady=6,
                                   cursor="pointinghand", font=("Pretendard", 10, "bold"))
        self.apply_btn.pack(side=tk.LEFT)
        self.export_btn = tk.Button(bot_row, text="⬇ 내보내기", command=self._export_config,
                                    bg="#334155", fg="white", relief=tk.FLAT, padx=10, pady=6,
                                    cursor="pointinghand", font=("Pretendard", 10))
        self.export_btn.pack(side=tk.LEFT, padx=(8, 0))
        self.import_btn = tk.Button(bot_row, text="⬆ 가져오기", command=self._import_config,
                                    bg="#334155", fg="white", relief=tk.FLAT, padx=10, pady=6,
                                    cursor="pointinghand", font=("Pretendard", 10))
        self.import_btn.pack(side=tk.LEFT, padx=(8, 0))
        self.device_import_btn = tk.Button(bot_row, text="🖥 디바이스에서 불러오기",
                                           command=self._import_from_device,
                                           bg="#334155", fg="white", relief=tk.FLAT, padx=10, pady=6,
                                           cursor="pointinghand", font=("Pretendard", 10))
        self.device_import_btn.pack(side=tk.LEFT, padx=(8, 0))
        log_frame = tk.Frame(bot_card, bg=self.card_bg)
        log_frame.pack(fill=tk.BOTH, pady=(8, 0))
        self.log_text = tk.Text(log_frame, height=6, bg="#020617", fg=self.text_color,
                                insertbackground=self.text_color, state=tk.DISABLED,
                                font=("Pretendard", 9), relief=tk.FLAT, padx=6, pady=4)
        self.log_text.pack(fill=tk.BOTH)
        self.log_text.tag_configure("error", foreground="#ef4444")
        self._log("준비됨. CYD에 설정을 적용하려면 '설정 적용'을 누르세요.")

        # 노트북 pack (하단 바 위 남은 공간 전체) 후 페이지 탭 구성
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 8))
        pages = self.config.get("pages") or []
        for page in range(len(pages)):
            self._make_page_tab(page, pages[page].get("name") or "Page %d" % (page + 1))

    def _make_button_card(self, parent: tk.Widget, page: int, bid: int) -> dict:
        """버튼 한 개의 편집 카드 (라벨/동작/값/색 + [G] 이미지 업로드 + 힌트)."""
        f = tk.Frame(parent, bg="#1e293b", bd=1, relief=tk.SOLID, padx=6, pady=5)
        tk.Label(f, text="#%d" % bid, bg="#1e293b", fg=self.sub_text,
                 font=("Pretendard", 8)).pack(anchor="w")
        lbl = ttk.Entry(f, width=12)
        lbl.pack(fill=tk.X, pady=(2, 2))
        act = ttk.Combobox(f, values=ACTION_TYPES, width=10, state="readonly")
        act.pack(fill=tk.X, pady=(0, 2))
        val = ttk.Entry(f, width=12)
        val.pack(fill=tk.X, pady=(0, 2))
        # 색상 선택 (구분용): 스와치 + 팔레트 Combobox
        color_row = tk.Frame(f, bg="#1e293b")
        color_row.pack(fill=tk.X, pady=(0, 2))
        swatch = tk.Label(color_row, text="  ", bg=COLOR_HEX[0], width=2)
        swatch.pack(side=tk.LEFT)
        col = ttk.Combobox(color_row, values=COLOR_NAMES, width=9, state="readonly")
        col.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))
        col.bind("<<ComboboxSelected>>",
                 lambda e, s=swatch, c=col: self._color_selected(s, c))
        # [G] 이미지 업로드: 업로드 버튼 + 썸네일(미리보기) + 제거 버튼
        img_row = tk.Frame(f, bg="#1e293b")
        img_row.pack(fill=tk.X, pady=(0, 2))
        img_btn = tk.Button(img_row, text="🖼", command=lambda: self._pick_image(page, bid),
                            bg="#334155", fg="white", relief=tk.FLAT, padx=5, pady=1,
                            cursor="pointinghand", font=("Pretendard", 9))
        img_btn.pack(side=tk.LEFT)
        img_preview = tk.Label(img_row, text="라벨/색상", bg="#0f172a", fg=self.sub_text,
                               font=("Pretendard", 7), padx=4, pady=2, width=9)
        img_preview.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        img_clear = tk.Button(img_row, text="✕", command=lambda: self._clear_image(page, bid),
                              bg="#334155", fg="#f8fafc", relief=tk.FLAT, padx=5, pady=1,
                              cursor="pointinghand", font=("Pretendard", 9))
        img_clear.pack(side=tk.LEFT)
        hint = tk.Label(f, text="", bg="#1e293b", fg=self.sub_text,
                        font=("Pretendard", 7), anchor="w")
        hint.pack(fill=tk.X)
        act.bind("<<ComboboxSelected>>",
                 lambda e, h=hint, a=act: self._update_hint(h, a))
        return {"frame": f, "label": lbl, "action": act, "value": val,
                "color": col, "swatch": swatch, "img_btn": img_btn,
                "img_preview": img_preview, "img_clear": img_clear,
                "img_photo": None, "hint": hint}

    @staticmethod
    def _color_selected(swatch: tk.Label, col: ttk.Combobox) -> None:
        name = col.get()
        if name in COLOR_NAMES:
            swatch.config(bg=COLOR_HEX[COLOR_NAMES.index(name)])

    # ------------------------------------------------------------------
    # [G] 버튼 이미지 업로드/제거/미리보기
    # ------------------------------------------------------------------
    def _pick_image(self, page: int, bid: int) -> None:
        """버튼에 이미지 파일 업로드 → 버튼 크기(71x61) JPEG base64로 저장.

        [H] 즉시 푸시하지 않는다 — 디바이스 반영은 [설정 적용]을 눌렀을 때만.
        """
        path = filedialog.askopenfilename(
            filetypes=[("이미지 파일", "*.png *.jpg *.jpeg *.bmp *.gif *.webp"),
                       ("모든 파일", "*.*")])
        if not path:
            return
        try:
            b64 = _image_file_to_b64(path)
        except Exception as e:
            self._log("⚠️ 이미지 변환 실패: %s" % e, error=True)
            return
        with self._config_lock:
            pages = self.config["pages"]
            if page < len(pages):
                btns = pages[page]["buttons"]
                if bid < len(btns):
                    btns[bid]["image"] = b64
        self._update_card_image_preview(page, bid)
        self._log("🖼 이미지 업로드: page%d · #%d (%s) — 적용하려면 [설정 적용]"
                  % (page + 1, bid, os.path.basename(path)))

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
        self._log("🗑 이미지 제거: page%d · #%d — 적용하려면 [설정 적용]" % (page + 1, bid))

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
        w["img_preview"].config(image="", text="라벨/색상")

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
            card["frame"].grid(row=r, column=c, padx=5, pady=5, sticky="nsew")
            page_widgets.append(card)
        for c in range(GRID_COLS):
            grid.columnconfigure(c, weight=1, uniform="col")
        for r in range(GRID_ROWS):
            grid.rowconfigure(r, weight=1, uniform="row")
        self._button_widgets.append(page_widgets)
        self._page_tabs.append(tab)

    @staticmethod
    def _update_hint(hint: tk.Label, act: ttk.Combobox) -> None:
        hints = {"shortcut": "단축키: cmd+shift+4",
                 "text": "문구: 한글 가능",
                 "app": "영문 앱명(예: Calculator) or URL"}
        hint.config(text=hints.get(act.get(), ""))

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
                w["label"].delete(0, tk.END)
                w["label"].insert(0, btn.get("label") or "")
                w["action"].set(btn.get("action_type", "shortcut"))
                w["value"].delete(0, tk.END)
                w["value"].insert(0, btn.get("action_value") or "")
                color = int(btn.get("color", 0) or 0)
                if not (0 <= color < len(COLOR_NAMES)):
                    color = 0
                w["color"].set(COLOR_NAMES[color])
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
                self._log("⚠️ 페이지는 최대 %d개까지 추가할 수 있습니다" % MAX_PAGES, error=True)
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
        self._log("+ 페이지 %d 추가" % (idx + 1))

    def _del_page(self) -> None:
        with self._config_lock:
            if len(self.config["pages"]) <= 1:
                messagebox.showwarning("페이지 삭제", "최소 1개 페이지는 필요합니다.")
                return
        idx = self._current_page_idx()
        if not (0 <= idx < len(self._button_widgets)):
            return
        has_content = any(
            w["label"].get().strip() or w["value"].get().strip() for w in self._button_widgets[idx])
        if has_content and not messagebox.askyesno(
                "페이지 삭제", "페이지 %d의 설정이 삭제됩니다. 계속할까요?" % (idx + 1)):
            return
        self.notebook.forget(idx)
        tab = self._page_tabs.pop(idx)
        tab.destroy()
        self._button_widgets.pop(idx)
        with self._config_lock:
            self.config["pages"].pop(idx)
        self._reindex_tab_labels()
        self._page_changed()
        self._log("− 페이지 %d 삭제" % (idx + 1))

    def _reindex_tab_labels(self) -> None:
        with self._config_lock:
            pages = self.config["pages"]
        for i, tab in enumerate(self._page_tabs):
            name = pages[i].get("name") if i < len(pages) else "Page %d" % (i + 1)
            self.notebook.tab(tab, text=name or "Page %d" % (i + 1))

    def _collect_config(self) -> dict:
        with self._config_lock:
            cur_pages = self.config["pages"]
        pages = []
        for page in range(len(self._button_widgets)):
            buttons = []
            for bid in range(BUTTONS_PER_PAGE):
                w = self._button_widgets[page][bid]
                cname = w["color"].get()
                color = COLOR_NAMES.index(cname) if cname in COLOR_NAMES else 0
                # 업로드 이미지는 위젯이 아니라 config 스냅샷(cur_pages)에서 가져온다 (base64 보존)
                image = None
                if page < len(cur_pages):
                    cbtns = cur_pages[page].get("buttons", [])
                    if bid < len(cbtns):
                        img = cbtns[bid].get("image")
                        image = img if isinstance(img, str) and img else None
                buttons.append({
                    "label": w["label"].get().strip()[:LABEL_MAX],
                    "action_type": w["action"].get() or "shortcut",
                    "action_value": w["value"].get(),
                    "color": color,
                    "image": image,
                })
            name = cur_pages[page].get("name") if page < len(cur_pages) else "Page %d" % (page + 1)
            pages.append({"name": name, "buttons": buttons})
        return {"version": 3, "port": UDP_PORT,
                "device_ip": self.config.get("device_ip", "") or "",
                "pages": pages}

    # ------------------------------------------------------------------
    # 설정 내보내기/가져오기 (D)
    # ------------------------------------------------------------------
    def _export_config(self) -> None:
        config = self._collect_config()
        path = filedialog.asksaveasfilename(
            defaultextension=".json", initialfile="macro_config.json",
            filetypes=[("JSON 파일", "*.json")])
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(config, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
            self._log("⬇ 설정 내보내기: %s (%d페이지)" % (path, len(config["pages"])))
        except OSError as e:
            self._log("⚠️ 내보내기 실패: %s" % e, error=True)

    def _import_config(self) -> None:
        path = filedialog.askopenfilename(
            filetypes=[("JSON 파일", "*.json"), ("모든 파일", "*.*")])
        if not path:
            return
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as e:
            messagebox.showerror("가져오기 실패", "설정 파일을 읽지 못했습니다:\n%s" % e)
            return
        new_config = self._normalize_config(raw)
        with self._config_lock:
            self.config = new_config
        self._rebuild_all_tabs()
        self._save_config(new_config)
        self._log("⬆ 설정 가져오기: %s (%d페이지) — 적용하려면 [설정 적용]"
                  % (path, len(new_config["pages"])))

    def _import_from_device(self) -> None:
        """[H] "디바이스에서 불러오기": MREQ 전송 + 덤프 수집 시작 (리스너가 큐를 드레인).

        액션은 디바이스에 저장만 되어 있고 실행은 항상 호스트가 하므로, 덤프로
        복원된 프로필을 그대로 Apply하면 무해한 자기 동기화가 된다.
        """
        with self._config_lock:
            ip = self.config.get("device_ip", "") or ""
        if not _is_valid_ip(ip):
            messagebox.showerror("디바이스에서 불러오기",
                                 "디바이스 IP를 알 수 없습니다.\n비콘으로 자동 검색될 때까지 기다린 후 다시 시도하세요.")
            return
        if not self._listener_running:
            messagebox.showerror("디바이스에서 불러오기", "리스너가 동작하지 않습니다.")
            return
        self._dump_queue.put(("dump_start", ip))
        self._log("🖥 디바이스에서 불러오기 요청 (%s) — 덤프 수신 대기" % ip)

    def _apply_dump_config(self, config: dict, n_img_recv: int = 0) -> None:
        """[H] 덤프 완료: GUI를 디바이스 설정으로 채운다 (검토 후 수동 Apply)."""
        new_config = self._normalize_config(config)
        n_pages = len(new_config["pages"])
        n_images = sum(1 for pg in new_config["pages"] for b in pg.get("buttons", [])
                       if b.get("image"))
        with self._config_lock:
            self.config = new_config
        self._rebuild_all_tabs()
        self._save_config(new_config)
        self._log("🖥 디바이스에서 불러오기 완료: %d페이지, 이미지 %d개 수신 — Apply로 전송하세요"
                  % (n_pages, n_img_recv))
        if n_images == 0:
            self._log("⚠️ 이미지가 수신되지 않았습니다 — 디바이스 플래시(/btns)에 저장된 이미지가 "
                      "없거나 전송이 유실됐습니다. (직렬 로그 [IMG] flash=1 / [MREQ] dump 확인)", error=True)

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
            self._log("💾 설정 저장 + 재전송 요청 (%d페이지)" % len(config["pages"]))
        elif _is_valid_ip(ip):
            self._send_config_from_temp_socket(ip, UDP_PORT)
            self._log("💾 설정 저장 + 전송 (%s)" % ip)
        else:
            self._log("💾 설정 저장 — 디바이스 발견 시 자동 전송됩니다")

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
            self._log("⚠️ 설정 전송 실패: %s" % e, error=True)

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
                sock.sendto(pkt, (ip, port))

    def _push_config(self, sock: socket.socket, ip: str, port: int, retries: int = 1,
                     reason: str = "푸시") -> None:
        """전체 설정+이미지 푸시. retries만큼 250ms 간격 재전송 (UDP 유실 대비).

        [H] 3초 재푸시 안전망이 제거됐으므로 Apply/시작/첫 비콘 푸시는 2회 전송으로 보정한다.
        리스너 스레드에서 실행되므로 로그는 _event_queue로 라우팅 (Tkinter 직접 접근 금지).
        reason은 트리거 진단용 — "적용 안 눌렀는데 표시"는 호스트 시작/첫 비콘 푸시로 확인된다.
        """
        self._event_queue.put(("log",
                               "📤 설정+이미지 푸시 (%s) → %s:%d" % (reason, ip, port), False))
        for attempt in range(retries + 1):
            if attempt:
                time.sleep(0.25)
            self._send_all_pages(sock, ip, port)
            self._send_all_images(sock, ip, port)

    def _send_all_images(self, sock: socket.socket, ip: str, port: int) -> None:
        """버튼 이미지(MIMG) 전송 (G): 업로드 이미지 / 한글 라벨 / ASCII·빈 라벨(clear).

        설정 푸시(MCFG) 직후 같은 소스로 호출된다. 버튼별 의도 3분기:
          1. 업로드 이미지(base64) → 그대로 MIMG(fmt=0) 전송
          2. 한글(비-ASCII) 라벨 → 텍스트를 71x61 JPEG로 렌더해 MIMG(fmt=0) 전송
          3. ASCII/빈 라벨 → MIMG(fmt=1, clear) 전송 → 디바이스가 펌웨어 텍스트/색 사각형 폴백
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
                            jpeg = base64.b64decode(b64)
                        except Exception:
                            continue           # 잘못된 base64 → 이 버튼은 보내지 않음
                        self._img_cache[key] = jpeg
                    keys.add(key)
                    sock.sendto(build_image_packet(page, bid, jpeg, 0), (ip, port))
                    continue

                # (2) 한글(비-ASCII) 라벨 → 텍스트를 71x61 JPEG로 렌더해 전송
                if label and not label.isascii():
                    if not (0 <= color < len(COLOR_NAMES)):
                        color = 0
                    key = (page, bid, label, color)
                    keys.add(key)
                    jpeg = self._img_cache.get(key)
                    if jpeg is None:
                        try:
                            jpeg = _render_button_image(label, color)
                        except RuntimeError as e:
                            # Pillow 미설치 같은 시스템적 실패는 1회만 경고 (3초 비콘마다 스팸 방지)
                            if "Pillow 미설치" in str(e) and not self._img_pillow_warned:
                                self._img_pillow_warned = True
                                self._event_queue.put(("log", str(e), True))
                            else:
                                self._event_queue.put(
                                    ("log", "⚠️ 이미지 렌더 실패 page%d·#%d: %s" % (page + 1, bid, e), True))
                            continue
                        except Exception as e:
                            self._event_queue.put(
                                ("log", "⚠️ 이미지 렌더 실패 page%d·#%d: %s" % (page + 1, bid, e), True))
                            continue
                        self._img_cache[key] = jpeg
                        rendered += 1
                    sock.sendto(build_image_packet(page, bid, jpeg, 0), (ip, port))
                    continue

                # (3) ASCII/빈 라벨 → clear(fmt=1): 디바이스의 구 이미지를 제거하고 텍스트로 폴백.
                #     매 푸시마다 보내 호스트/디바이스 재시작 후에도 의도가 수렴된다.
                #     (한글→ASCII 전환 시 남는 스테일 이미지 제거 — 펌웨어는 이미지 없으면 no-op)
                keys.add((page, bid, "clear"))
                sock.sendto(build_image_packet(page, bid, b"", 1), (ip, port))
        # 캐시 정리: 더 이상 존재하지 않는 키(버튼/라벨/색/이미지 변경) 제거
        for k in list(self._img_cache):
            if k not in keys:
                del self._img_cache[k]
        # 3초 비콘 재푸시(캐시 히트)는 조용히 — 새로 렌더된 게 있을 때만 로그
        if rendered:
            self._event_queue.put(("log", "🖼 버튼 이미지 %d개 렌더+전송" % rendered, False))

    # ------------------------------------------------------------------
    # 리스너 스레드 (UDP 수신 + 액션 실행)
    # ------------------------------------------------------------------
    def _start_listener(self) -> None:
        """리스너 자동 시작: 고정 포트 UDP_PORT(8890)로 바인드해 실시간 수신."""
        if self._listener_running:
            return
        port = UDP_PORT
        with self._config_lock:
            self.config["port"] = port
        if not _pynput_installed():
            self._log("⚠️ pynput 미설치 — 단축키/문구 액션이 동작하지 않습니다 (pip install -r requirements.txt)",
                      error=True)
        self._listener_running = True
        self._listener_thread = threading.Thread(target=self._listener_worker, args=(port,),
                                                 daemon=True)
        self._listener_thread.start()
        self.listen_status.config(text="● 리스너 동작 중 · CYD 자동 검색 대기...", fg="#22c55e")
        self._log("▶ 리스너 자동 시작: UDP %d (디바이스 자동 검색)" % port)

    def _stop_listener(self) -> None:
        self._listener_running = False
        if self._listener_thread is not None:
            self._listener_thread.join(timeout=3.0)
            self._listener_thread = None
        self.listen_status.config(text="● 리스너 중지됨", fg=self.sub_text)
        self._log("■ 리스너 중지")

    def _listener_worker(self, port: int) -> None:
        """UDP 수신 리스너: 소켓 수명 전체를 이 스레드가 소유한다.

        bind → 시작 시 설정 1회 → 수신 루프(+하트비트/재전송 요청 처리) → finally: close.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError as e:
            self._listener_running = False
            self._event_queue.put(("log", "⚠️ UDP %d 바인드 실패: %s" % (port, e), True))
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
            # [H] 호스트 시작 시 1회 푸시 (IP 아는 경우에만) + 세션 첫 푸시로 기록
            self._pushed_ip = ip
            self._push_config(sock, ip, port, retries=1, reason="호스트 시작")

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
                        sock.sendto(build_mreq_packet(), (dip, UDP_PORT))
                        self._dump = {"pages": {}, "images": {}, "num_pages": None,
                                      "device_ip": dip, "start": time.time(), "last_pkt": time.time()}
                        # [H] 경쟁 방지: 불러오기 직후 첫 비콘 푸시를 억제한다.
                        #     _pushed_ip가 None(아직 미발견)인 채로 불러오기를 하면, 덤프 완료로 호스트
                        #     config가 디바이스 데이터로 채워진 뒤 들어오는 첫 비콘이 "새 IP"로 오인돼
                        #     방금 불러온 설정을 디바이스로 재푸시("Apply 안 눌렀는데 적용")한다.
                        self._pushed_ip = dip
                        sock.settimeout(0.5)   # 수집 중 더 자주 기상해 quiet 판정
                except queue.Empty:
                    pass

                # [H] 덤프 완료/타임아웃 판정
                self._maybe_finalize_dump(sock)
        finally:
            # [FIX #1] 소켓은 리스너 스레드에서만 닫는다. 메인 스레드는 절대 닫지 않음.
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
            self._dump = None
            sock.settimeout(2.0)   # 정상 타임아웃 복원
            # [H] 진단: MREQ 덤프로 받은 이미지 패킷 수 — 0이면 디바이스 플래시에 이미지가
            #     없거나 유실된 것. build_config_from_dump 결과와 함께 로그로 남긴다.
            n_img = len(d.get("images") or {})
            config = build_config_from_dump(d)
            if config is None:
                self._event_queue.put(("dump_fail",
                                       "디바이스 응답이 불완전합니다 (일부 페이지/버튼 누락). 다시 시도하세요."))
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
            self._event_queue.put((page, button, "✓ page%d · #%d: %s 실행됨" % (page + 1, button, desc),
                                   False))
            self._send_feedback(sock, addr, page, button, True)    # [B] MPOK
        except Exception as e:
            self._event_queue.put((page, button, "✗ page%d · #%d: %s" % (page + 1, button, e),
                                   True))
            self._send_feedback(sock, addr, page, button, False)   # [B] MPER

    def _send_feedback(self, sock: socket.socket, addr, page: int, button: int, ok: bool) -> None:
        """액션 실행 결과(성공=MPOK/실패=MPER)를 디바이스로 회신 — 버튼 플래시 피드백(B).

        디바이스는 항상 UDP_PORT(8890)에서 listen 중이므로, 이벤트 소스 IP + 고정 포트로 보낸다.
        (이벤트 패킷의 소스 포트는 WiFiUDP가 임의 할당하므로 회신 대상이 아니다.)
        """
        magic = MAGIC_OK if ok else MAGIC_ERR
        pkt = EVENT_HEADER.pack(magic, page, button, 0, 0)
        try:
            sock.sendto(pkt, (addr[0], UDP_PORT))
        except OSError:
            pass

    def _handle_beacon(self, sock: socket.socket, addr, port: int) -> None:
        """디스커버리 비콘("MPBE") 수신 — H 동기화 재설계.

        3초마다 전체 재푸시하던 것을 제거하고:
          (a) 호스트 IP·포트 학습용 **ACK**(MCFG count=0, ~12B) 유니캐스트 회신
              → 디바이스가 호스트 IP 변경을 재실행 없이 자가치유.
          (b) 전체 설정+이미지 푸시는 **새 디바이스 IP의 첫 비콘에만** (세션당 1회, _pushed_ip)
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
                                   "디바이스 발견 (자동 검색): %s — 설정 전송" % device_ip))
        # (a) ACK: 디바이스가 소스 IP/포트만 학습 (count=0 → 상태 변화 없음)
        with self._config_lock:
            num_pages = len(self.config.get("pages") or [])
        sock.sendto(build_ack_packet(num_pages), (device_ip, UDP_PORT))
        # (b) 전체 설정+이미지는 새 디바이스 IP의 첫 비콘에만 1회
        if device_ip != self._pushed_ip:
            self._pushed_ip = device_ip
            self._push_config(sock, device_ip, port, retries=1, reason="첫 비콘")

    def _apply_detected_ip(self, ip: str, msg: str) -> None:
        """자동 검색된 디바이스를 상태바에 반영 (메인 스레드)."""
        self._log(msg)
        port = int(self.config.get("port", UDP_PORT) or UDP_PORT)
        self.listen_status.config(
            text="● 리스너 동작 중 · 디바이스 발견: %s:%d" % (ip, port), fg="#22c55e")

    @staticmethod
    def _action_desc(btn: dict) -> str:
        atype = btn.get("action_type", "shortcut")
        aval = btn.get("action_value", "")
        if atype == "text":
            return "문구 \"%s\"" % aval[:20]
        if atype == "app":
            return "앱/URL %s" % aval
        return "단축키 %s" % aval

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
            raise RuntimeError("pynput 미설치 — pip install -r requirements.txt")
        run_input_helper("shortcut", s)   # 격리된 서브프로세스에서 실행

    def _exec_text(self, text: str) -> None:
        if not text:
            return
        if not _pynput_installed():
            raise RuntimeError("pynput 미설치 — pip install -r requirements.txt")
        # pbcopy + Cmd+V: IME 상태와 무관하게 한글 포함 텍스트 입력 (macOS)
        run_input_helper("text", text)    # 격리된 서브프로세스에서 실행

    @staticmethod
    def _exec_app(value: str) -> None:
        value = value.strip()
        if not value:
            return
        if "://" in value:
            cmd = ["open", value]                    # URL
        else:
            cmd = ["open", "-a", value]              # 앱 번들 (Finder 영문 이름)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            raise RuntimeError("앱 실행 시간 초과 (10s)")
        except OSError as e:
            raise RuntimeError("앱 실행 오류: %s" % e)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError("앱 실행 실패: %s" % (err or "open이 실패했습니다"))

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
                elif item[0] == "dump_fail":    # [H] 덤프 수집 실패/타임아웃
                    _, msg = item
                    self._log(msg, error=True)
                    messagebox.showerror("디바이스에서 불러오기 실패", msg)
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
        # [G] 실제 렌더 크기가 단일 패킷(≤JPEG_MAX_BYTES)에 들어가는지 검증 (Pillow 필요)
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
                assert len(jpg) <= JPEG_MAX_BYTES, \
                    "JPEG %dB > %dB (단일 UDP 패킷 불가): %r" % (len(jpg), JPEG_MAX_BYTES, lbl)
            # [G] 업로드 이미지 경로: 임의 크기 이미지 → 중앙 크롭 채움 71x61 JPEG → base64 왕복
            src = Image.new("RGB", (200, 100), (200, 30, 30))
            up_jpg = _image_to_button_jpeg(src)
            assert up_jpg[:2] == b"\xff\xd8", "업로드 이미지 JPEG 마커 확인"
            assert len(up_jpg) <= JPEG_MAX_BYTES, \
                "업로드 JPEG %dB > %dB (단일 UDP 패킷 불가)" % (len(up_jpg), JPEG_MAX_BYTES)
            up_b64 = base64.b64encode(up_jpg).decode("ascii")
            assert base64.b64decode(up_b64) == up_jpg               # config 저장/전송 왕복
            reopened = Image.open(io.BytesIO(up_jpg))
            assert reopened.size == (BTN_IMG_W, BTN_IMG_H), \
                "중앙 크롭 채움 결과 크기 %r ≠ 71x61" % (reopened.size,)
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
        # [H] 불완전 덤프(12버튼 미달) → None
        dump2 = {"pages": {0: {"name": "", "buttons": {}}}, "images": {}, "num_pages": 1,
                 "device_ip": "", "start": 0, "last_pkt": 0}
        for c in chunk_config_packets(0, btns[:3], 1, ""):
            assert apply_mcfg_to_dump(dump2, c)
        assert build_config_from_dump(dump2) is None, "3/12버튼 덤프는 불완전"
        print("OK: 패킷 구성/파싱 검증 통과 (헤더 8B, magic MCFG/MPAD/MIMG/MREQ, v3 엔트리 >BBBBB+액션, num_pages, ACK count=0, 청크 분할, 라벨 절단, 이벤트 파싱, 이미지 크기 ≤%dB, clear fmt=1, 업로드 변환, 덤프 왕복)" % JPEG_MAX_BYTES)
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
