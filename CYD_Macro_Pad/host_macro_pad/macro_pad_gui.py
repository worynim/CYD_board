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

import importlib.util
import json
import logging
import queue
import socket
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

# ==========================================
# 크로스파일 계약 (CYD_Macro_Pad/CYD_Macro_Pad.ino와 정확히 일치해야 함)
# ==========================================
UDP_PORT = 8890              # 매크로 패드 전용 포트 (스트리밍 8888과 독립)
MAGIC_CONFIG = 0x4D434647    # "MCFG" 호스트→디바이스 설정 패킷
MAGIC_EVENT = 0x4D504144     # "MPAD" 디바이스→호스트 이벤트 패킷
MAGIC_BEACON = 0x4D504245    # "MPBE" 디바이스→브로드캐스트 디스커버리 비콘
PAGES = 2
GRID_COLS = 4
GRID_ROWS = 3
BUTTONS_PER_PAGE = GRID_COLS * GRID_ROWS   # 12
LABEL_MAX = 24               # 라벨 최대 바이트 (UTF-8)

# 양쪽 모두 8바이트 헤더: magic u32 + 필드 4 × u8
CONFIG_HEADER = struct.Struct(">IBBBB")    # magic, page, count, rsvd, rsvd
EVENT_HEADER = struct.Struct(">IBBBB")     # magic, page, button, flags, rsvd

ACTION_TYPES = ["shortcut", "text", "app"]


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


def build_config_packet(page_idx: int, buttons) -> bytes:
    """한 페이지 설정 패킷을 만든다. buttons는 {'label', ...} 딕셔너리 리스트.

    >IBBBB 헤더(magic, page, count, 0, 0) + count개 엔트리(>BB + label bytes)
    """
    entries = bytearray()
    for bid, btn in enumerate(buttons):
        label = _trunc_utf8(btn.get("label") or "", LABEL_MAX)
        entries += struct.pack(">BB", bid, len(label))
        entries += label
    return CONFIG_HEADER.pack(MAGIC_CONFIG, page_idx, len(buttons), 0, 0) + bytes(entries)


def parse_event_packet(data: bytes):
    """이벤트 패킷 파싱. 유효하면 (page, button, flags), 아니면 None."""
    if len(data) < 8:
        return None
    magic, page, button, flags, rsvd = EVENT_HEADER.unpack(data[:8])
    if magic != MAGIC_EVENT:
        return None
    return (page, button, flags)


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


class MacroPadGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("CYD Wireless Macro Pad Host")
        self.root.geometry("520x820")
        self.root.resizable(False, False)

        self.config_path = Path(__file__).resolve().parent / "macro_config.json"
        self.config = self._load_config()          # dict (런타임 스냅샷: 메인/리스너가 lock으로 읽음)

        # 스레드 동기화
        self._config_lock = threading.Lock()
        self._resend_queue: "queue.Queue[str]" = queue.Queue()  # Apply→리스너 재전송 요청
        self._event_queue: "queue.Queue[tuple]" = queue.Queue()  # 리스너→UI 로그
        self._listener_running = False
        self._listener_thread: threading.Thread | None = None
        self._ip_manual_edit_time = 0.0   # 사용자가 IP 직접 입력 시각 (자동검색 덮어쓰기 10s 보호)

        self._button_widgets: list = []            # [page][button] → 위젯 dict

        self.setup_ui_style()
        self.create_widgets()
        self._populate_from_config()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll_event_queue()

    # ------------------------------------------------------------------
    # 설정 로드/저장
    # ------------------------------------------------------------------
    def _load_config(self) -> dict:
        if self.config_path.exists():
            try:
                return json.loads(self.config_path.read_text(encoding="utf-8"))
            except Exception as e:
                logging.warning("[CFG] 설정 파싱 실패, 기본값 사용: %s", e)
        return self._default_config()

    @staticmethod
    def _default_config() -> dict:
        empty = [{"label": "", "action_type": "shortcut", "action_value": ""}
                 for _ in range(BUTTONS_PER_PAGE)]
        return {
            "version": 1,
            "port": UDP_PORT,
            "device_ip": "",
            "pages": [{"name": "Page %d" % (i + 1), "buttons": list(empty)}
                      for i in range(PAGES)],
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

        # 디바이스 카드
        dev_card = tk.Frame(self.root, bg=self.card_bg, bd=1, relief=tk.SOLID, padx=12, pady=10)
        dev_card.pack(fill=tk.X, padx=16, pady=(0, 10))
        dev_row = tk.Frame(dev_card, bg=self.card_bg)
        dev_row.pack(fill=tk.X)
        ttk.Label(dev_row, text="CYD IP:").pack(side=tk.LEFT)
        self.ip_entry = ttk.Entry(dev_row, width=15)
        self.ip_entry.pack(side=tk.LEFT, padx=6)
        self.ip_entry.bind("<KeyRelease>", lambda e: self._on_ip_edited())
        ttk.Label(dev_row, text="포트:").pack(side=tk.LEFT)
        self.port_entry = ttk.Entry(dev_row, width=6)
        self.port_entry.pack(side=tk.LEFT, padx=6)
        self.listen_btn = tk.Button(dev_row, text="● 리스너 시작", command=self._toggle_listener,
                                    bg="#22c55e", fg="white", relief=tk.FLAT, padx=14, pady=6,
                                    cursor="pointinghand", font=("Pretendard", 10, "bold"))
        self.listen_btn.pack(side=tk.RIGHT)
        self.ip_hint = tk.Label(dev_card, text="(IP 비우면 자동검색)", bg=self.card_bg,
                                fg=self.sub_text, font=("Pretendard", 8), anchor="w")
        self.ip_hint.pack(fill=tk.X, pady=(4, 0))
        self.listen_status = tk.Label(dev_card, text="● 리스너 중지됨", bg=self.card_bg,
                                      fg=self.sub_text, font=("Pretendard", 9), anchor="w")
        self.listen_status.pack(fill=tk.X, pady=(8, 0))

        # 페이지 노트북 (2페이지 × 4×3 버튼)
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 10))
        self._button_widgets = []
        for page in range(PAGES):
            tab = tk.Frame(self.notebook, bg=self.card_bg, padx=8, pady=8)
            self.notebook.add(tab, text="Page %d" % (page + 1))
            grid = tk.Frame(tab, bg=self.card_bg)
            grid.pack(fill=tk.BOTH, expand=True)
            page_widgets = []
            for bid in range(BUTTONS_PER_PAGE):
                r, c = divmod(bid, GRID_COLS)
                card = self._make_button_card(grid, bid)
                card["frame"].grid(row=r, column=c, padx=5, pady=5, sticky="nsew")
                page_widgets.append(card)
            for c in range(GRID_COLS):
                grid.columnconfigure(c, weight=1, uniform="col")
            for r in range(GRID_ROWS):
                grid.rowconfigure(r, weight=1, uniform="row")
            self._button_widgets.append(page_widgets)

        # 하단 카드: 적용 / 하트비트 / 이벤트 로그
        bot_card = tk.Frame(self.root, bg=self.card_bg, bd=1, relief=tk.SOLID, padx=12, pady=10)
        bot_card.pack(fill=tk.X, padx=16)
        bot_row = tk.Frame(bot_card, bg=self.card_bg)
        bot_row.pack(fill=tk.X)
        self.apply_btn = tk.Button(bot_row, text="💾 설정 적용 (Apply)", command=self._apply,
                                   bg="#0ea5e9", fg="white", relief=tk.FLAT, padx=14, pady=6,
                                   cursor="pointinghand", font=("Pretendard", 10, "bold"))
        self.apply_btn.pack(side=tk.LEFT)
        self.hb_var = tk.BooleanVar(value=bool(self.config.get("heartbeat_enabled", True)))
        self.hb_var.trace_add("write", lambda *a: self._hb_changed())
        ttk.Checkbutton(bot_row, text="주기 재전송 (20s)", variable=self.hb_var).pack(side=tk.LEFT, padx=14)
        log_frame = tk.Frame(bot_card, bg=self.card_bg)
        log_frame.pack(fill=tk.BOTH, pady=(8, 0))
        self.log_text = tk.Text(log_frame, height=6, bg="#020617", fg=self.text_color,
                                insertbackground=self.text_color, state=tk.DISABLED,
                                font=("Pretendard", 9), relief=tk.FLAT, padx=6, pady=4)
        self.log_text.pack(fill=tk.BOTH)
        self.log_text.tag_configure("error", foreground="#ef4444")
        self._log("준비됨. CYD에 설정을 적용하려면 '설정 적용'을 누르세요.")

    def _make_button_card(self, parent: tk.Widget, bid: int) -> dict:
        """버튼 한 개의 편집 카드 (라벨/동작/값 + 힌트)."""
        f = tk.Frame(parent, bg="#1e293b", bd=1, relief=tk.SOLID, padx=6, pady=5)
        tk.Label(f, text="#%d" % bid, bg="#1e293b", fg=self.sub_text,
                 font=("Pretendard", 8)).pack(anchor="w")
        lbl = ttk.Entry(f, width=12)
        lbl.pack(fill=tk.X, pady=(2, 2))
        act = ttk.Combobox(f, values=ACTION_TYPES, width=10, state="readonly")
        act.pack(fill=tk.X, pady=(0, 2))
        val = ttk.Entry(f, width=12)
        val.pack(fill=tk.X, pady=(0, 2))
        hint = tk.Label(f, text="", bg="#1e293b", fg=self.sub_text,
                        font=("Pretendard", 7), anchor="w")
        hint.pack(fill=tk.X)
        act.bind("<<ComboboxSelected>>",
                 lambda e, h=hint, a=act: self._update_hint(h, a))
        return {"frame": f, "label": lbl, "action": act, "value": val, "hint": hint}

    @staticmethod
    def _update_hint(hint: tk.Label, act: ttk.Combobox) -> None:
        hints = {"shortcut": "단축키: cmd+shift+4",
                 "text": "문구: 한글 가능",
                 "app": "영문 앱명(예: Calculator) or URL"}
        hint.config(text=hints.get(act.get(), ""))

    # ------------------------------------------------------------------
    # 설정 ↔ 위젯
    # ------------------------------------------------------------------
    def _on_ip_edited(self) -> None:
        """IP 필드를 직접 입력하면 시각 기록 — 자동 검색이 10초간 덮어쓰지 않도록."""
        self._ip_manual_edit_time = time.time()

    def _populate_from_config(self) -> None:
        self.ip_entry.delete(0, tk.END)
        self.ip_entry.insert(0, str(self.config.get("device_ip") or ""))
        self.port_entry.delete(0, tk.END)
        self.port_entry.insert(0, str(self.config.get("port") or UDP_PORT))
        pages = self.config.get("pages") or []
        for page in range(PAGES):
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
                self._update_hint(w["hint"], w["action"])

    def _collect_config(self, ip: str, port: int) -> dict:
        pages = []
        for page in range(PAGES):
            buttons = []
            for bid in range(BUTTONS_PER_PAGE):
                w = self._button_widgets[page][bid]
                buttons.append({
                    "label": w["label"].get().strip()[:LABEL_MAX],
                    "action_type": w["action"].get() or "shortcut",
                    "action_value": w["value"].get(),
                })
            pages.append({"name": "Page %d" % (page + 1), "buttons": buttons})
        return {"version": 1, "port": port, "device_ip": ip,
                "heartbeat_enabled": bool(self.hb_var.get()), "pages": pages}

    def _validate_addr(self, require_ip: bool = True):
        """IP/포트 검증. 오류 시 메시지박스 표시 후 (None, None).

        require_ip=False면 IP가 비어 있어도 허용 (자동 검색 모드).
        """
        ip = self.ip_entry.get().strip()
        if not ip:
            if require_ip:
                messagebox.showerror("주소 오류", "CYD IP를 입력하세요.")
                return (None, None)
            ip = ""
        try:
            port = int(self.port_entry.get().strip())
        except ValueError:
            messagebox.showerror("포트 오류", "포트는 숫자여야 합니다.")
            return (None, None)
        return (ip, port)

    def _hb_changed(self) -> None:
        with self._config_lock:
            self.config["heartbeat_enabled"] = bool(self.hb_var.get())

    # ------------------------------------------------------------------
    # 설정 적용 (Apply)
    # ------------------------------------------------------------------
    def _apply(self) -> None:
        ip, port = self._validate_addr(require_ip=False)
        if ip is None:
            return
        config = self._collect_config(ip, port)
        self._save_config(config)
        with self._config_lock:
            self.config = config
        if not _is_valid_ip(ip):
            # IP 미지정 → 리스너 시작 후 자동 검색으로 전송 (여기선 저장만)
            self._log("💾 설정 저장 — CYD IP 미지정, 리스너 시작하면 자동 검색으로 전송됩니다")
            return
        # 리스너가 실행 중이면 리스너 소켓으로 재전송 (소스 포트 = 수신 포트 보장)
        if self._listener_running:
            self._resend_queue.put("resend")
            self._log("💾 설정 저장 + 재전송 요청 (%s:%d)" % (ip, port))
        else:
            self._send_config_from_temp_socket(ip, port)
            self._log("💾 설정 저장 + 전송 (%s:%d)" % (ip, port))

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
            finally:
                sock.close()
        except OSError as e:
            self._log("⚠️ 설정 전송 실패: %s" % e, error=True)

    def _send_all_pages(self, sock: socket.socket, ip: str, port: int) -> None:
        with self._config_lock:
            pages = self.config.get("pages") or []
        for page in range(PAGES):
            btns = pages[page].get("buttons", []) if page < len(pages) else []
            if len(btns) < BUTTONS_PER_PAGE:
                btns = btns + [{"label": ""}] * (BUTTONS_PER_PAGE - len(btns))
            sock.sendto(build_config_packet(page, btns), (ip, port))

    # ------------------------------------------------------------------
    # 리스너 스레드 (UDP 수신 + 액션 실행)
    # ------------------------------------------------------------------
    def _toggle_listener(self) -> None:
        if self._listener_running:
            self._stop_listener()
        else:
            self._start_listener()

    def _start_listener(self) -> None:
        ip, port = self._validate_addr(require_ip=False)
        if ip is None:
            return
        with self._config_lock:
            self.config["device_ip"] = ip
            self.config["port"] = port
        if not _pynput_installed():
            self._log("⚠️ pynput 미설치 — 단축키/문구 액션이 동작하지 않습니다 (pip install -r requirements.txt)",
                      error=True)
        self._listener_running = True
        self._listener_thread = threading.Thread(target=self._listener_worker, args=(port,),
                                                 daemon=True)
        self._listener_thread.start()
        self.listen_btn.config(text="■ 수신 중지", bg="#ef4444")
        if _is_valid_ip(ip):
            self.listen_status.config(
                text="● 리스너 동작 중 (%s:%d)  ·  접근성 권한: 시스템설정 → 손쉬운 사용" % (ip, port),
                fg="#22c55e")
            self._log("▶ 리스너 시작: %s:%d" % (ip, port))
        else:
            self.listen_status.config(
                text="● 리스너 동작 중 · CYD IP 자동 검색 대기 중...", fg="#22c55e")
            self._log("▶ 리스너 시작: CYD IP 자동 검색 대기 중 (디바이스 발견 시 자동 설정)")

    def _stop_listener(self) -> None:
        self._listener_running = False
        if self._listener_thread is not None:
            self._listener_thread.join(timeout=3.0)
            self._listener_thread = None
        self.listen_btn.config(text="● 리스너 시작", bg="#22c55e")
        self.listen_status.config(text="● 리스너 중지됨", fg=self.sub_text)
        self._log("■ 리스너 중지")

    def _listener_worker(self, port: int) -> None:
        """UDP 수신 리스너: 소켓 수명 전체를 이 스레드가 소유한다.

        bind → 시작 시 설정 1회 → 수신 루프(+하트비트/재전송 요청 처리) → finally: close.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        sock.settimeout(2.0)   # 주기적 기상: 하트비트/재전송/종료 폴링

        with self._config_lock:
            ip = self.config.get("device_ip", "") or ""
        if _is_valid_ip(ip):
            self._send_all_pages(sock, ip, port)     # 시작 시 1회 설정 푸시 (IP 아는 경우에만)
        last_heartbeat = time.time()

        try:
            while self._listener_running:
                try:
                    data, addr = sock.recvfrom(4096)
                    if parse_beacon_packet(data):
                        self._handle_beacon(sock, addr, port)   # IP 자동 검색
                    else:
                        self._handle_event_packet(data, addr)
                except socket.timeout:
                    pass
                except OSError:
                    break

                with self._config_lock:
                    hb_enabled = bool(self.config.get("heartbeat_enabled", True))
                now = time.time()
                if hb_enabled and now - last_heartbeat >= 20.0:
                    with self._config_lock:
                        ip = self.config.get("device_ip", "") or ""
                    if _is_valid_ip(ip):
                        self._send_all_pages(sock, ip, port)
                        last_heartbeat = now
                try:
                    self._resend_queue.get_nowait()   # Apply 재전송 요청
                    with self._config_lock:
                        ip = self.config.get("device_ip", "") or ""
                    if _is_valid_ip(ip):
                        self._send_all_pages(sock, ip, port)
                        last_heartbeat = now
                except queue.Empty:
                    pass
        finally:
            # [FIX #1] 소켓은 리스너 스레드에서만 닫는다. 메인 스레드는 절대 닫지 않음.
            sock.close()
            self._log("리스너 스레드 종료", debug=True)

    def _handle_event_packet(self, data: bytes, addr) -> None:
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
        except Exception as e:
            self._event_queue.put((page, button, "✗ page%d · #%d: %s" % (page + 1, button, e),
                                   True))

    def _handle_beacon(self, sock: socket.socket, addr, port: int) -> None:
        """디스커버리 비콘("MPBE") 수신: 소스 IP로 디바이스 주소를 학습하고 설정을 푸시한다."""
        device_ip = addr[0]
        with self._config_lock:
            cur = self.config.get("device_ip", "") or ""
            if device_ip == cur:
                return   # 이미 아는 주소 — 재전송 불필요
            self.config["device_ip"] = device_ip
            self.config["port"] = port
        self._event_queue.put(("ip", device_ip,
                               "디바이스 발견 (자동 검색): %s — 설정 전송" % device_ip))
        self._send_all_pages(sock, device_ip, port)

    def _apply_detected_ip(self, ip: str, msg: str) -> None:
        """자동 검색된 IP를 UI에 반영 (메인 스레드).

        사용자가 직접 입력한 지 10초 안에는 덮어쓰지 않는다(_ip_manual_edit_time).
        """
        self._log(msg)
        if time.time() - self._ip_manual_edit_time > 10:
            self.ip_entry.delete(0, tk.END)
            self.ip_entry.insert(0, ip)
        port = int(self.config.get("port", UDP_PORT) or UDP_PORT)
        self.listen_status.config(
            text="● 리스너 동작 중 (%s:%d) · 디바이스 자동 발견" % (ip, port), fg="#22c55e")

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
        btns = [{"label": "Copy", "action_type": "shortcut", "action_value": "cmd+c"},
                {"label": "안녕", "action_type": "text", "action_value": "x"},
                {"label": "", "action_type": "app", "action_value": ""}] + \
               [{"label": "", "action_type": "shortcut", "action_value": ""}] * 9
        pkt = build_config_packet(0, btns)
        assert pkt[:8] == struct.pack(">IBBBB", MAGIC_CONFIG, 0, 12, 0, 0)
        assert len(pkt) == 8 + 12 * 2 + 4 + 6   # 4="Copy", 6="안녕" UTF-8
        magic, page, count, r, r2 = CONFIG_HEADER.unpack(pkt[:8])
        assert magic == MAGIC_CONFIG and page == 0 and count == 12
        # 라벨 24바이트 초과 절단 검증 (멀티바이트 비분할)
        long_label = _trunc_utf8("가" * 30, LABEL_MAX)
        assert len(long_label) <= LABEL_MAX and long_label.decode("utf-8").endswith("가")
        # 이벤트 파싱
        evt = struct.pack(">IBBBB", MAGIC_EVENT, 1, 5, 0, 0)
        assert parse_event_packet(evt) == (1, 5, 0)
        assert parse_event_packet(evt[:7]) is None          # 짧은 패킷
        bad = struct.pack(">IBBBB", 0xDEADBEEF, 0, 0, 0, 0)  # 잘못된 magic
        assert parse_event_packet(bad) is None
        print("OK: 패킷 구성/파싱 검증 통과 (헤더 8B, magic MCFG/MPAD, 라벨 절단, 이벤트 파싱)")
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
