#!/usr/bin/env python3
"""
CYD Portable Monitor - Stable High-Speed GUI Streamer
- 캡처 소스: 개별 모니터 선택 또는 특정 창(window) 캡처 (macOS, [WINCAP])
- 화면 비율 모드: Letterbox(원본 비율 유지), Stretch(채우기), Crop(크롭 맞춤)
- 안정적인 청크 송신 제어로 패킷 유실 방지 및 깔끔한 화면 전송
"""

import os
import sys
import time
import io
import socket
import struct
import threading
import queue
import logging
from dataclasses import dataclass
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Dict, List, Optional

try:
    import mss
    from PIL import Image
except ImportError:
    print("[-] 필수 라이브러리가 설치되지 않았습니다: pip install mss Pillow")
    sys.exit(1)

# [WINCAP] 창(window) 캡처 — macOS에서만 지원. Quartz(CGWindowList)로 보이는 앱 창의
# 위치/크기를 얻어 mss grab 영역으로 넘긴다. pyobjc-framework-Quartz 미설치·비-macOS여도
# 기존 모니터 캡처는 그대로 동작해야 하므로 ImportError를 삼켜 기능만 비활성화한다.
IS_MACOS = sys.platform == "darwin"
HAS_WINDOW_SOURCE = False
if IS_MACOS:
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGWindowListOptionOnScreenOnly,
            kCGNullWindowID,
            kCGWindowName,
            kCGWindowOwnerName,
            kCGWindowOwnerPID,
            kCGWindowNumber,
            kCGWindowLayer,
            kCGWindowBounds,
        )
        HAS_WINDOW_SOURCE = True
    except ImportError:
        logging.warning("[WINCAP] pyobjc-framework-Quartz 없음 — 창 캡처 비활성 "
                        "(pip install pyobjc-framework-Quartz)")


logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)

# UDP 데이터 페이로드 크기(바이트). 펌웨어 CYD_Wireless_Monitor.ino의
# PACKET_PAYLOAD_SIZE(1400)와 반드시 일치해야 하는 크로스파일 계약 상수.
# 다르면 청크 경계가 어긋나 화면이 깨지므로, 한쪽을 바꿀 때는 반드시 함께 바꿀 것.
PAYLOAD_CHUNK_SIZE = 1400

# [FIX #12 → 실험 상한] 디바이스 물리 한계는 rt≈54ms(SPI 푸시 17ms + 디코드 37ms) ≈ ~18fps.
# 과거엔 발신 초과 시 recv≈0·drop 폭증(누적 백로그) 때문에 16~20으로 하향했으나,
# [LATEST-WINS]+[FIX#R2 pendingCommit] 도입으로 호스트·디바이스 측 낡은 프레임 누적은 사라졌으므로
# 디바이스 한계 실험을 위해 슬라이더 최대(30)까지 허용한다. 장기 안정치는 [STAT] drop/miss/rend_fps로 판단.
# 단 최종 병목은 AP 다운링크 큐다: 발신 바이트가 무선 배출률을 넘으면 공유기에 수초 백로그가 쌓이며
# 이 지연은 어느 쪽 코드 버퍼에도 없는 것이라 회수 불가하다(ping RTT 단조 증가로 확인, 2026-08-24).
DEVICE_RENDER_FPS_CAP = 30


@dataclass
class StreamConfig:
    """워커 스레드에 전달하는 스트리밍 파라미터 스냅샷 (스레드 안전).

    메인 스레드가 생성하고, 워커 스레드는 Lock을 통해 읽기만 합니다.
    """

    aspect_mode: int   # 0=Letterbox, 1=Stretch, 2=Crop
    target_fps: int    # 목표 FPS
    jpeg_quality: int  # JPEG 품질 (20~85)
    rot_code: int      # CYD 회전 코드 (0~3)
    show_fps: int      # CYD FPS 오버레이 표시 여부 (0/1)


@dataclass
class CaptureSource:
    """[WINCAP] 캡처 소스 스냅샷.

    스트리밍 시작 시 메인 스레드에서 만들어 _capture_worker에 인자로 전달된다.
    워커는 이 객체만 읽으므로 UI 위젯 접근 없이 스레드 안전이다(재시작 시에도
    새 스냅샷을 받는 기존 monitor_idx 패턴과 동일한 계약).
    """

    kind: str            # "monitor" | "window"
    mon_idx: int = 0     # kind="monitor": monitors_info 인덱스
    window_id: int = -1  # kind="window": CGWindowNumber
    label: str = ""      # 로그용 표시 ("앱 — 제목" / 모니터 라벨)


class CYDStreamerGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("CYD Wireless Monitor Control Panel")
        self.root.geometry("490x690")
        self.root.resizable(False, False)

        self.is_streaming = False
        self.stream_thread: threading.Thread | None = None   # 소비자(UDP 전송)
        self.capture_thread: threading.Thread | None = None  # 생산자(캡처+인코딩)
        self._frame_queue: queue.Queue | None = None         # 생산자→소비자 JPEG 큐
        self._stage_times: Dict[str, float] = {}             # 스테이지별 누적 타이밍 (2s 로그용)
        self._stage_last_log = 0.0
        # GUI 표시용 스테이지 누적 (500ms 갱신). 캡처 스레드가 쓰고 메인(Tkinter) 스레드가
        # GIL 하에서 읽고 초기화하므로 lock이 없다 — benign race. 최악의 경우 통계 한 샘플이
        # 유실될 뿐 동작엔 영향 없음 (단순 표시 전용 데이터).
        self._ui_stage: Dict[str, float] = {}
        self._ui_frames: int = 0
        self.sock: socket.socket | None = None
        self._last_sent_ctrl: tuple = (-1, -1)  # 마지막으로 보낸 (rot_code, show_fps) — [FIX #11] 중복 전송 방지

        # 워커 스레드와 메인 스레드 간 파라미터 공유 (스레드 안전)
        # 메인 스레드가 새 StreamConfig 객체로 교체하고, 워커 스레드는 Lock으로 읽음
        self._config_lock = threading.Lock()
        self._stream_config: StreamConfig | None = None

        self.current_fps = 0.0
        self.current_kbps = 0.0
        self.current_frame_kb = 0.0

        self.monitors_info = self.get_individual_monitors()

        # [WINCAP] 창 캡처 상태 (아래 create_widgets → _build_capture_options가 채움)
        # _windows: 콤보에 표시할 창 항목 [{desc, window_id, w, h, ...}] — 메인 스레드 전용.
        # _mon_count: 콤보에서 모니터 구간 크기. 그 뒤 인덱스는 구분선(더미) 1개 + 창 목록.
        # _last_src_idx: 구분선 더미 선택을 되돌리기 위한 마지막 유효 선택.
        # _sel_win_size: 선택 시점 창의 논리 크기 — 회전 자동 감지용 캐시.
        # _last_window_region: 창이 닫혔을 때 쓰는 마지막 grab 영역 폴백 — 캡처 스레드 전용 필드
        #   (워커만 읽고 쓴다. 메인은 start_streaming에서 초기화).
        self._windows: List[Dict] = []
        self._mon_count = len(self.monitors_info)
        self._last_src_idx = 0
        self._sel_win_size: tuple = (0, 0)
        self._last_window_region: Optional[Dict] = None

        self.setup_ui_style()
        self.create_widgets()
        # [FIX #9] 창 닫기 이벤트 핸들러 등록: 스트리밍 중단 후 안전하게 종료
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def get_individual_monitors(self) -> List[Dict]:
        """개별 모니터 목록을 반환합니다.

        [FIX #7] 단일 모니터 환경(노트북 단독)에서도 빈 리스트가 반환되지 않도록
        처리합니다. monitors[0]은 전체 통합 가상 영역이므로 제외하고,
        개별 모니터가 없을 때만 fallback으로 포함합니다.
        """
        with mss.mss() as sct:
            individuals = list(sct.monitors[1:])
            return individuals if individuals else list(sct.monitors[:1])

    # ------------------------------------------------------------------
    # [WINCAP] 창(window) 캡처
    # ------------------------------------------------------------------

    @staticmethod
    def list_app_windows() -> List[Dict]:
        """화면에 보이는 앱 창 목록을 수집합니다 (macOS 전용, [WINCAP]).

        CGWindowListCopyWindowInfo(OnScreenOnly)의 결과를 UI 표시 항목으로 정리한다.
        - kCGWindowLayer != 0 : 메뉴바·Dock 등 오버레이 → 제외
        - 자기 프로세스(PID 일치) : 이 GUI의 Tkinter 창 → 제외
        - 제목(kCGWindowName)이 없는 보조 창(툴팁·오버레이) → 제외
          ※ macOS 10.15+ 에서는 화면 기록(Screen Recording) 권한이 있어야 타 앱 창의
           제목이 채워진다. 모니터 캡처(mss)에 이미 필요한 권한이라 추가 허용은 불필요.
        - 64x64 미만 미니 창 → 제외

        mss 좌표계와의 정합성: mss는 macOS에서 모니터 좌표를 CGDisplayBounds
        (논리 포인트)로 잡으므(darwin.py _monitors_impl), kCGWindowBounds의 포인트 값과
        같은 좌표계다 — grab 영역으로 그대로 넘길 수 있어 Retina 배율 변환이 불필요하다.
        단 grab 결과 이미지는 네이티브(Retina 2x) 해상도로 돌아올 수 있으나 기존 파이프라인은
        sct_img.size(실제 픽셀)를 읽으므로 자동 처리된다.
        """
        if not HAS_WINDOW_SOURCE:
            return []
        own_pid = os.getpid()
        wins: List[Dict] = []
        try:
            infos = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
        except Exception as e:
            logging.warning("[WINCAP] 창 목록 조회 실패: %s", e)
            return []
        for info in infos or []:
            try:
                if int(info.get(kCGWindowLayer, 0)) != 0:
                    continue
                if int(info.get(kCGWindowOwnerPID, -1)) == own_pid:
                    continue
                owner = str(info.get(kCGWindowOwnerName) or "")
                title = str(info.get(kCGWindowName) or "")
                if not title:
                    continue
                b = info.get(kCGWindowBounds) or {}
                w, h = int(b.get("Width", 0)), int(b.get("Height", 0))
                if w < 64 or h < 64:
                    continue
                wins.append({
                    "desc": f"{owner} — {title}" if owner and owner != title else title,
                    "window_id": int(info.get(kCGWindowNumber, -1)),
                    "w": w, "h": h,
                    "owner": owner, "title": title,
                })
            except Exception:
                continue  # 개별 항목 파싱 실패는 무시하고 다음 창 계속
        return wins

    def _get_window_region(self, window_id: int) -> Optional[Dict]:
        """[WINCAP] 창의 현재 위치/크기를 mss grab 영역({left,top,width,height})으로 반환.

        매 프레임 재조회해 창 이동·리사이즈를 실시간 추적한다. 조회 비용(CGWindowList
        전체 열거)은 프레임당 수 ms 수준으로 기존 grab(~30ms) 대비 작지만, 정확한
        오버헤드는 [TIMING] grab 스테이지에 자연히 반영된다.

        반환 None 의미:
        - 창을 찾지 못함(닫힘) 또는 크기가 사실상 0 → 호출자가 폴백/스킵 판단.
        - 완전히 화면 밖(모든 개별 모니터와 교집합 0) → grab이 검은 이미지를 내놓는
          것을 막기 위해 스킵.

        스레드 계약: _capture_worker(캡처 스레드)에서만 호출. 메인 스레드는 시작
        전 유효성 확인(start_streaming)에서만 호출한다.
        """
        if not HAS_WINDOW_SOURCE:
            return None
        try:
            infos = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
        except Exception:
            return None
        for info in infos or []:
            try:
                if int(info.get(kCGWindowNumber, -2)) != window_id:
                    continue
                b = info.get(kCGWindowBounds) or {}
                left, top = int(b.get("X", 0)), int(b.get("Y", 0))
                width, height = int(b.get("Width", 0)), int(b.get("Height", 0))
                if width < 8 or height < 8:
                    return None
                region = {"left": left, "top": top, "width": width, "height": height}
                for mon in self.monitors_info:
                    ox = max(region["left"], mon["left"])
                    oy = max(region["top"], mon["top"])
                    ix = min(region["left"] + region["width"],
                             mon["left"] + mon["width"]) - ox
                    iy = min(region["top"] + region["height"],
                             mon["top"] + mon["height"]) - oy
                    if ix > 0 and iy > 0:
                        return region  # 어느 모니터든 걸치면 유효
                return None  # 완전히 화면 밖
            except Exception:
                continue
        return None

    def _build_capture_options(self) -> List[str]:
        """[WINCAP] 캡처 소스 콤보의 표시 항목을 만듭니다 (메인 스레드 전용).

        구성: 모니터 항목들 → 구분선 더미 1개 → 창 항목들.
        부작용으로 self._windows / self._mon_count를 갱신한다. 인덱스 ↔ 소스 매핑은
        _current_capture_source가 동일한 규칙으로 해석하므로 두 함수는 함께 수정할 것.
        """
        opts = []
        for idx, m in enumerate(self.monitors_info):
            orient = "세로" if m["height"] > m["width"] else "가로"
            opts.append(f"모니터 #{idx + 1} [{orient}] ({m['width']}x{m['height']})")
        self._mon_count = len(opts)

        if HAS_WINDOW_SOURCE:
            self._windows = self.list_app_windows()
            if self._windows:
                opts.append("─── 창(window) ───")  # 더미 항목(선택 시 이전 선택으로 되돌림)
                opts.extend(f"🪟 {w['desc']}" for w in self._windows)
        return opts

    def _current_capture_source(self) -> CaptureSource:
        """현재 콤보 선택을 CaptureSource 스냅샷으로 변환합니다 (메인 스레드 전용)."""
        sel = self.mon_combo.current()
        if sel < 0 or sel < self._mon_count:
            idx = max(0, sel)
            return CaptureSource(kind="monitor", mon_idx=idx, label=f"모니터 #{idx + 1}")
        # 창 구간: [모니터들][구분선][창들] — 창 인덱스 = sel - 모니터수 - 1
        widx = sel - self._mon_count - 1
        if 0 <= widx < len(self._windows):
            w = self._windows[widx]
            return CaptureSource(kind="window", window_id=w["window_id"], label=w["desc"])
        return CaptureSource(kind="monitor", mon_idx=0, label="모니터 #1")

    def _on_capture_source_changed(self, event=None) -> None:
        """캡처 소스 변경 콜백 — 구분선 더미 선택 되돌림 + 설정 갱신 ([WINCAP])."""
        sel = self.mon_combo.current()
        if sel == self._mon_count:  # 구분선 더미 → 이전 유효 선택으로 복원
            self.mon_combo.current(self._last_src_idx)
            return
        src = self._current_capture_source()
        if src.kind == "window":
            w = next((w for w in self._windows if w["window_id"] == src.window_id), None)
            if w is not None:
                self._sel_win_size = (w["w"], w["h"])  # 회전 자동 감지용 캐시
        self._last_src_idx = sel
        self._update_stream_config()

    def refresh_window_list(self) -> None:
        """🔄 윈도우 목록 재조회 — 콤보를 다시 만들고 이전 선택을 복원합니다.

        창 목록은 수시로 바뀌므로(창 열기/닫기/제목 변경) 시작 전에 눌러 최신화한다.
        이전 선택이 window였는데 그 창이 사라졌으면 모니터 #1로 폴백한다.
        """
        prev = self._current_capture_source()
        self.mon_combo.config(values=self._build_capture_options())
        restore = 0
        if prev.kind == "monitor" and prev.mon_idx < self._mon_count:
            restore = prev.mon_idx
        elif prev.kind == "window":
            for i, w in enumerate(self._windows):
                if w["window_id"] == prev.window_id:
                    restore = self._mon_count + 1 + i
                    break
        self.mon_combo.current(restore)
        self._last_src_idx = restore
        logging.debug("[WINCAP] 창 목록 새로고침: %d개", len(self._windows))

    def setup_ui_style(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")

        self.bg_color = "#1e293b"
        self.card_bg = "#0f172a"
        self.text_color = "#f8fafc"
        self.sub_text = "#94a3b8"

        self.root.configure(bg=self.bg_color)
        style.configure("TLabel", background=self.card_bg, foreground=self.text_color, font=("Pretendard", 10))
        style.configure("Header.TLabel", background=self.bg_color, foreground=self.text_color, font=("Pretendard", 14, "bold"))
        style.configure("TCheckbutton", background=self.card_bg, foreground=self.text_color, font=("Pretendard", 10))
        style.configure("TFrame", background=self.card_bg)

    def create_widgets(self) -> None:
        header_frame = tk.Frame(self.root, bg=self.bg_color, pady=10)
        header_frame.pack(fill=tk.X, padx=16)

        title_lbl = ttk.Label(header_frame, text="🖥️ CYD Wireless Monitor Host", style="Header.TLabel")
        title_lbl.pack(anchor="w")

        sub_lbl = tk.Label(
            header_frame,
            text="ESP32-2432S028 High-Speed UDP Streamer",
            bg=self.bg_color,
            fg=self.sub_text,
            font=("Pretendard", 9),
        )
        sub_lbl.pack(anchor="w")

        card = tk.Frame(self.root, bg=self.card_bg, bd=1, relief=tk.SOLID, padx=16, pady=12)
        card.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 16))

        # 1. IP / 포트
        ip_frame = tk.Frame(card, bg=self.card_bg)
        ip_frame.pack(fill=tk.X, pady=4)

        ttk.Label(ip_frame, text="CYD IP:").pack(side=tk.LEFT)
        self.ip_entry = ttk.Entry(ip_frame, width=16)
        self.ip_entry.insert(0, "192.168.10.56")
        self.ip_entry.pack(side=tk.LEFT, padx=6)

        ttk.Label(ip_frame, text="포트:").pack(side=tk.LEFT, padx=(4, 2))
        self.port_entry = ttk.Entry(ip_frame, width=6)
        self.port_entry.insert(0, "8888")
        self.port_entry.pack(side=tk.LEFT)

        # 2. 캡처 소스 선택 — [WINCAP] 개별 모니터 + 보이는 창을 한 콤보에 통합
        #    (모니터 항목들 → 구분선 더미 → 🪟 창 항목들)
        mon_frame = tk.Frame(card, bg=self.card_bg)
        mon_frame.pack(fill=tk.X, pady=4)

        ttk.Label(mon_frame, text="캡처 화면:").pack(side=tk.LEFT)
        self.mon_combo = ttk.Combobox(mon_frame, values=self._build_capture_options(),
                                      state="readonly", width=30)
        self.mon_combo.current(0)
        self._last_src_idx = 0
        self.mon_combo.pack(side=tk.LEFT, padx=6)
        self.mon_combo.bind("<<ComboboxSelected>>", self._on_capture_source_changed)

        self.win_refresh_btn = tk.Button(
            mon_frame,
            text="🔄",
            command=self.refresh_window_list,
            relief=tk.FLAT,
            bg=self.card_bg,
            fg=self.text_color,
            font=("Pretendard", 10),
            cursor="pointinghand",
        )
        self.win_refresh_btn.pack(side=tk.LEFT)
        if not HAS_WINDOW_SOURCE:
            self.win_refresh_btn.config(state=tk.DISABLED)

        # 3. 화면 비율 설정
        aspect_frame = tk.Frame(card, bg=self.card_bg)
        aspect_frame.pack(fill=tk.X, pady=4)

        ttk.Label(aspect_frame, text="화면 비율:").pack(side=tk.LEFT)
        self.aspect_combo = ttk.Combobox(
            aspect_frame,
            values=[
                "원본 비율 유지 (Letterbox - 검은 여백)",
                "꽉 채우기 (Stretch - 늘리기)",
                "비율 맞춤 크롭 (Crop & Fill - 잘라내어 채움)",
            ],
            state="readonly",
            width=28,
        )
        self.aspect_combo.current(0)
        self.aspect_combo.pack(side=tk.LEFT, padx=6)
        # aspect_combo 변경 시 설정 갱신 (원본에서 누락된 바인딩 추가)
        self.aspect_combo.bind("<<ComboboxSelected>>", self.on_control_param_changed)

        # 4. 화면 회전
        rot_frame = tk.Frame(card, bg=self.card_bg)
        rot_frame.pack(fill=tk.X, pady=4)

        ttk.Label(rot_frame, text="화면 회전:").pack(side=tk.LEFT)
        self.rot_combo = ttk.Combobox(
            rot_frame,
            values=[
                "자동 감지 (모니터 가로/세로 비율)",
                "가로 정방향 (Landscape - 320x240)",
                "가로 180도 반전 (Landscape Rev)",
                "세로 모드 (Portrait - 240x320)",
                "세로 180도 반전 (Portrait Rev)",
            ],
            state="readonly",
            width=28,
        )
        self.rot_combo.current(0)
        self.rot_combo.pack(side=tk.LEFT, padx=6)
        self.rot_combo.bind("<<ComboboxSelected>>", self.on_control_param_changed)

        # 5. FPS 표시 토글 (기본 OFF)
        fps_check_frame = tk.Frame(card, bg=self.card_bg)
        fps_check_frame.pack(fill=tk.X, pady=4)

        self.show_fps_var = tk.BooleanVar(value=False)
        self.fps_check = tk.Checkbutton(
            fps_check_frame,
            text="CYD 화면에 실제 렌더링 FPS 표시 (우측 하단)",
            variable=self.show_fps_var,
            bg=self.card_bg,
            fg=self.text_color,
            selectcolor="#334155",
            activebackground=self.card_bg,
            activeforeground=self.text_color,
            font=("Pretendard", 10),
            command=self.on_control_param_changed,
        )
        self.fps_check.pack(anchor="w")

        # 6. JPEG 품질
        q_frame = tk.Frame(card, bg=self.card_bg)
        q_frame.pack(fill=tk.X, pady=4)

        self.q_label = ttk.Label(q_frame, text="JPEG 품질 (60):")
        self.q_label.pack(anchor="w")

        self.q_scale = ttk.Scale(q_frame, from_=20, to=85, orient=tk.HORIZONTAL, command=self.on_quality_change)
        self.q_scale.set(60)
        self.q_scale.pack(fill=tk.X, pady=2)

        # 7. 목표 FPS
        fps_frame = tk.Frame(card, bg=self.card_bg)
        fps_frame.pack(fill=tk.X, pady=4)

        self.fps_label = ttk.Label(fps_frame, text="목표 FPS (18 FPS):")
        self.fps_label.pack(anchor="w")

        # [FIX #12 → 실험 상한] 기본 18(디바이스 렌더 한계 ~18fps 부근). 슬라이더 최대 30까지
        # DEVICE_RENDER_FPS_CAP로 클램프되며, 클램프는 호스트 발신율 제어일 뿐 AP 큐 적체까지는 못 막음.
        self.fps_scale = ttk.Scale(fps_frame, from_=10, to=30, orient=tk.HORIZONTAL, command=self.on_fps_change)
        self.fps_scale.set(18)
        self.fps_scale.pack(fill=tk.X, pady=2)

        # 8. 통계 패널
        stat_frame = tk.Frame(card, bg="#020617", bd=1, relief=tk.RIDGE, padx=10, pady=8)
        stat_frame.pack(fill=tk.X, pady=8)

        self.status_lbl = tk.Label(stat_frame, text="상태: 대기 중 (Stopped)", bg="#020617", fg="#fbbf24", font=("Pretendard", 10, "bold"))
        self.status_lbl.pack(anchor="w")

        self.stats_lbl = tk.Label(stat_frame, text="전송 FPS: 0.0 | 프레임: 0.0 KB | 대역폭: 0 kbps", bg="#020617", fg="#38bdf8", font=("Pretendard", 9))
        self.stats_lbl.pack(anchor="w", pady=(3, 0))

        # 9. 시작 / 정지 버튼
        btn_frame = tk.Frame(card, bg=self.card_bg)
        btn_frame.pack(fill=tk.X, pady=4)

        self.start_btn = tk.Button(
            btn_frame,
            text="▶ 스트리밍 시작",
            bg="#22c55e",
            fg="black",
            font=("Pretendard", 11, "bold"),
            relief=tk.FLAT,
            padx=12,
            pady=8,
            command=self.toggle_streaming,
            cursor="pointinghand",
        )
        self.start_btn.pack(fill=tk.X)

        self.update_stats_ui()

    # ------------------------------------------------------------------
    # 설정 헬퍼
    # ------------------------------------------------------------------

    def _build_stream_config(self) -> StreamConfig:
        """현재 UI 위젯 값으로 StreamConfig 스냅샷을 생성합니다.

        반드시 메인 스레드(tkinter 루프)에서 호출해야 합니다.
        워커 스레드는 이 메서드를 직접 호출하지 않고 _config_lock으로 읽습니다.
        """
        rot_code = self.get_target_rotation_code(self._current_capture_source())
        return StreamConfig(
            aspect_mode=self.aspect_combo.current(),
            target_fps=max(5, int(self.fps_scale.get())),
            jpeg_quality=max(20, int(self.q_scale.get())),
            rot_code=rot_code,
            show_fps=1 if self.show_fps_var.get() else 0,
        )

    def _update_stream_config(self) -> None:
        """UI 값으로 스트림 설정을 원자적으로 갱신하고, CYD로 제어 명령을 전송합니다.

        메인 스레드에서 호출됩니다. 스트리밍 중이 아니면 아무것도 하지 않습니다.
        """
        if not self.is_streaming or self.sock is None:
            return

        cfg = self._build_stream_config()
        with self._config_lock:
            self._stream_config = cfg

        # [FIX #11] 제어 패킷은 회전(rot_code)·오버레이(show_fps)가 실제로 바뀔 때만 전송.
        # 품질·FPS·비율은 호스트 전용 설정이라 디바이스 알림이 불필요. 슬라이더 드래그마다
        # 제어 패킷을 보내면 디바이스가 세션 리셋(재조립 버퍼 클리어)을 반복해 프레임이 폐기됨.
        if (cfg.rot_code, cfg.show_fps) == self._last_sent_ctrl:
            return
        ip = self.ip_entry.get().strip()
        try:
            port = int(self.port_entry.get().strip())  # [FIX #4] ValueError 안전 처리
        except ValueError:
            return
        self.send_control_command(self.sock, (ip, port), cfg.rot_code, cfg.show_fps)
        self._last_sent_ctrl = (cfg.rot_code, cfg.show_fps)

    # ------------------------------------------------------------------
    # UI 콜백
    # ------------------------------------------------------------------

    def on_quality_change(self, val: str) -> None:
        q = int(float(val))
        self.q_label.config(text=f"JPEG 품질 ({q}):")
        self._update_stream_config()

    def on_fps_change(self, val: str) -> None:
        f = int(float(val))
        # [FIX #12] 전송 상한(DEVICE_RENDER_FPS_CAP=30) 적용 결과를 표시
        eff = min(f, DEVICE_RENDER_FPS_CAP)
        self.fps_label.config(text=f"목표 FPS ({f} FPS){'' if eff == f else f' → {eff} 적용'}:")
        self._update_stream_config()

    def on_control_param_changed(self, event=None) -> None:
        self._update_stream_config()

    # ------------------------------------------------------------------
    # 네트워크
    # ------------------------------------------------------------------

    def send_control_command(self, sock: socket.socket, dest_addr: tuple, rot_val: int, show_fps: int) -> None:
        try:
            # data[6] = FEC 프로토콜 활성 플래그 (1). 펌웨어가 마지막 청크를 패리티로 해석하도록 통보.
            # 레거시(패딩 없음) 호스트와 섞여도 펌웨어가 프로토콜을 구분해 화면 보존.
            cmd_packet = struct.pack(">IBBBB", 0xFFFFFFFF, rot_val, show_fps, 1, 0)
            sock.sendto(cmd_packet, dest_addr)
        except Exception:
            pass

    # 회전 코드 매핑 — 펌웨어/CLAUDE.md의 프로토콜 표와 반드시 동기 유지할 것:
    #   0 = portrait(240x320) · 1 = landscape 180°(reversed) · 2 = portrait 180°(reversed) · 3 = landscape(320x240, 기본)
    # UI 콤보 인덱스: 0 자동감지 · 1 가로 정방향 · 2 가로 180° · 3 세로 · 4 세로 180°
    def get_target_rotation_code(self, src: CaptureSource) -> int:
        """자동 감지는 캡처 소스의 가로/세로 비율을 따른다 ([WINCAP] 창 소스 지원).

        창 소스일 때는 선택 시점에 캐시한 논리 크기(_sel_win_size)를 사용한다.
        매 프레임 추적은 grab 영역만 갱신하고, 회전 코드는 제어 패킷([FIX #11] 중복
        전송 방지)을 유발하므로 실시간으로 뒤집지 않는다 — 창을 극단적으로 리사이즈해
        방향이 바뀌면 다음 파라미터 변경 시점에 반영된다.
        """
        idx = self.rot_combo.current()
        if idx == 1:
            return 3
        if idx == 2:
            return 1
        if idx == 3:
            return 0
        if idx == 4:
            return 2

        # 자동 감지 (idx == 0)
        if src.kind == "window":
            w, h = self._sel_win_size
            return 0 if h > w else 3
        mon_idx = min(src.mon_idx, len(self.monitors_info) - 1)
        monitor = self.monitors_info[max(0, mon_idx)]
        return 0 if monitor["height"] > monitor["width"] else 3

    # ------------------------------------------------------------------
    # 스트리밍 제어
    # ------------------------------------------------------------------

    def toggle_streaming(self) -> None:
        if not self.is_streaming:
            self.start_streaming()
        else:
            self.stop_streaming()

    def start_streaming(self) -> None:
        ip = self.ip_entry.get().strip()
        if not ip:
            messagebox.showerror("오류", "CYD IP 주소를 입력해주세요.")
            return

        try:
            port = int(self.port_entry.get().strip())
        except ValueError:
            messagebox.showerror("오류", "유효한 포트 번호를 입력해주세요.")
            return

        # [WINCAP] 캡처 소스 스냅샷 + 창 소스 유효성 사전 확인.
        # 워커는 이 스냅샷만 사용하므로 스트리밍 중 목록 새로고침/재선택이 현재 세션을 건드리지 않는다.
        sel_src = self._current_capture_source()
        if sel_src.kind == "window":
            if self._get_window_region(sel_src.window_id) is None:
                messagebox.showerror(
                    "오류",
                    "선택한 창을 찾을 수 없습니다 (닫혔거나 완전히 화면 밖).\n"
                    "🔄 새로고침 후 다시 선택해주세요.",
                )
                return
            # 폴백 초기값: 시작 직후 첫 조회 실패 대비 마지막 영역 캐시 리셋
            self._last_window_region = None

        logging.debug("[START] 스트리밍 시작 → %s:%d, 소스=%s(%s)", ip, port,
                      sel_src.kind, sel_src.label)

        # [DEBUG] 이전 워커/캡처 스레드 생존 여부 확인
        for tname, t in (("워커", self.stream_thread), ("캡처", self.capture_thread)):
            if t is not None and t.is_alive():
                logging.warning("[START] 이전 %s 스레드가 아직 살아있음! 재시작 경쟁 조건 위험. 잠시 대기...", tname)
                t.join(timeout=2.0)
                if t.is_alive():
                    logging.error("[START] 이전 %s 스레드가 2초 내 종료되지 않음. 강제 진행.", tname)
                else:
                    logging.debug("[START] 이전 %s 스레드 정상 종료 확인.", tname)

        self.is_streaming = True

        # 초기 스트림 설정 스냅샷 생성 (메인 스레드에서 안전하게)
        initial_config = self._build_stream_config()
        with self._config_lock:
            self._stream_config = initial_config
        logging.debug("[START] 초기 StreamConfig: %s", initial_config)

        self.start_btn.config(text="⏹ 스트리밍 정지", bg="#ef4444")
        self.status_lbl.config(text="상태: 스트리밍 전송 중...", fg="#4ade80")

        self.ip_entry.config(state="disabled")
        self.port_entry.config(state="disabled")
        self.mon_combo.config(state="disabled")
        self.win_refresh_btn.config(state=tk.DISABLED)  # [WINCAP] 스트리밍 중 목록 갱신 방지

        # [Phase 4] 파이프라인: 캡처(생산자) → JPEG 큐 → UDP 전송(소비자)
        # 큐가 가득 차면 오래된 프레임을 버리고 최신 프레임을 유지 → 지연 최소화
        self._frame_queue = queue.Queue(maxsize=2)
        self._stage_times.clear()
        self._stage_last_log = time.time()
        self._ui_stage.clear()
        self._ui_frames = 0
        # [FIX #11] 새 스트림 시작 시 초기 제어 명령은 반드시 재전송되도록 리셋
        self._last_sent_ctrl = (-1, -1)

        self.stream_thread = threading.Thread(target=self._stream_worker, args=(ip, port), daemon=True)
        self.capture_thread = threading.Thread(target=self._capture_worker, args=(sel_src,), daemon=True)
        self.stream_thread.start()
        self.capture_thread.start()
        logging.debug("[START] 워커(id=%d) + 캡처(id=%d) 스레드 시작됨",
                      self.stream_thread.ident or -1, self.capture_thread.ident or -1)

    def stop_streaming(self) -> None:
        """스트리밍 플래그만 해제합니다.

        [FIX #1] 소켓을 여기서 직접 닫지 않습니다.
        소켓 정리는 워커 스레드의 finally 블록이 담당하여
        sendto() 도중 소켓이 닫히는 경쟁 조건(Race Condition)을 방지합니다.
        """
        logging.debug("[STOP] 스트리밍 정지 요청. 현재 sock=%s", self.sock)
        self.is_streaming = False
        logging.debug("[STOP] is_streaming = False 설정 완료. 워커 스레드가 루프를 빠져나오길 기다리는 중...")

        self.start_btn.config(text="▶ 스트리밍 시작", bg="#22c55e")
        self.status_lbl.config(text="상태: 정지됨 (Stopped)", fg="#fbbf24")
        self.stats_lbl.config(text="전송 FPS: 0.0 | 프레임: 0.0 KB | 대역폭: 0 kbps")

        self.ip_entry.config(state="normal")
        self.port_entry.config(state="normal")
        self.mon_combo.config(state="readonly")
        if HAS_WINDOW_SOURCE:
            self.win_refresh_btn.config(state=tk.NORMAL)

    def on_close(self) -> None:
        """[FIX #9] 창 닫기 이벤트 핸들러.

        스트리밍 중이면 먼저 중단하고, 워커 스레드가 소켓을 정리할 시간을 준 후 창을 닫습니다.
        """
        logging.debug("[CLOSE] 창 닫기 요청.")
        if self.is_streaming:
            self.stop_streaming()
        for tname, t in (("워커", self.stream_thread), ("캡처", self.capture_thread)):
            if t is not None and t.is_alive():
                logging.debug("[CLOSE] %s 스레드 종료 대기 중...", tname)
                t.join(timeout=2.0)
        logging.debug("[CLOSE] 창 종료.")
        self.root.destroy()

    # ------------------------------------------------------------------
    # 워커 스레드
    # ------------------------------------------------------------------

    def _stream_worker(self, ip: str, port: int) -> None:
        """UDP 전송 소비자 스레드: 청크 분할 + FEC 패리티 + 전송.

        소켓의 생명주기 전체(생성~종료)를 이 메서드가 책임집니다.
        캡처/인코딩은 _capture_worker(생산자)가 수행하고, JPEG 바이트만 큐로 전달받습니다.
        """
        my_thread_id = threading.current_thread().ident
        logging.debug("[WORKER:%d] 소비자 시작. 대상=%s:%d", my_thread_id, ip, port)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock = sock
        dest_addr = (ip, port)
        logging.debug("[WORKER:%d] 소켓 생성 완료 (fd=%s)", my_thread_id, sock.fileno())

        # 초기 제어 명령 전송
        with self._config_lock:
            cfg = self._stream_config
        if cfg is not None:
            logging.debug("[WORKER:%d] 초기 제어 명령 전송 → rot=%d, show_fps=%d", my_thread_id, cfg.rot_code, cfg.show_fps)
            self.send_control_command(sock, dest_addr, cfg.rot_code, cfg.show_fps)
            # [FIX #11] 초기 전송분을 추적해 UI 콜백이 중복 전송하지 않도록 함
            self._last_sent_ctrl = (cfg.rot_code, cfg.show_fps)

        frame_id = 0
        frame_count = 0
        fps_start_time = time.time()
        loop_exit_reason = "is_streaming=False (정상 종료)"
        # 재시작 시 self._frame_queue가 교체돼도 이 스레드의 큐를 고정 (오염 방지)
        my_queue = self._frame_queue

        try:
            while True:
                # 생산자가 넣은 JPEG 바이트 (None = 생산자 종료 시그널)
                jpeg_bytes = my_queue.get()
                if jpeg_bytes is None:
                    loop_exit_reason = "캡처(생산자) 스레드 종료"
                    break

                # [LATEST-WINS] 큐에 더 새 프레임이 남아 있으면 낡은 것은 폐기하고 최신으로 덮어쓴다.
                # 호스트 측(앱 큐 이전 단계)에는 낡은 프레임이 쌓이지 않는다. 목표 FPS와 무관하게 동작.
                # 주의(2026-08-24 ping 진단): 이미 sendto를 지난 데이터그램은 회수 불가 — 발신 바이트량이
                # AP 다운링크 배출률을 넘으면 공유기 큐에 수초 백로그가 생기며, latest-wins로는 못 막는다.
                # "정지 후 수초 재생" 지연이 재현되면 코드보다 전송량(품질/FPS)↓ 또는 무선 환경 개선이 처방.
                eof_seen = False
                while True:
                    try:
                        newer = my_queue.get_nowait()
                    except queue.Empty:
                        break
                    if newer is None:
                        eof_seen = True  # 생산자 종료 시그널 — 유실 방지 위해 되돌려 놓는다
                        break
                    jpeg_bytes = newer
                if eof_seen:
                    my_queue.put(None)

                if not self.is_streaming:
                    break

                try:
                    # [FEC] 제로패딩 블록 → XOR 패리티 1개를 마지막 청크로 전송
                    total_len = len(jpeg_bytes)
                    data_chunks = (total_len + PAYLOAD_CHUNK_SIZE - 1) // PAYLOAD_CHUNK_SIZE
                    padded = jpeg_bytes + b"\x00" * (data_chunks * PAYLOAD_CHUNK_SIZE - total_len)

                    # 패리티(XOR) 계산: 1400B 블록을 bigint로 취급해 C 수준 XOR → 프레임당 ~0.1ms로
                    # 캡처/인코딩(~30-40ms) 대비 미미. 의도적으로 단순하게 유지한다 (과최적화 금지).
                    parity = 0
                    for i in range(data_chunks):
                        parity ^= int.from_bytes(padded[i * PAYLOAD_CHUNK_SIZE:(i + 1) * PAYLOAD_CHUNK_SIZE], "little")
                    parity_bytes = parity.to_bytes(PAYLOAD_CHUNK_SIZE, "little")
                    total_chunks = data_chunks + 1

                    # [FIX #2] frame_id 마스크 (0xFFFFFFFF는 컨트롤 패킷 식별자로 예약)
                    frame_id = (frame_id + 1) & 0x7FFFFFFF

                    # [FIX #13] 청크 미세버스트 제거: 15개 청크를 1.5ms에 몰아 보내면
                    # Wi-Fi AP/CYD RX가 버스트 꼬리(마지막 데이터+패리티)를 연속 유실 →
                    # 프레임당 2+ 청크 유실로 프레임 폐기(drop 폭증, fec≈0, rend_fps 폭락).
                    # 프레임 주기에 걸쳐 분산하면 손실이 단일 청크(FEC 복원)에 그친다.
                    #
                    # [FIX #14] 패리티도 분산 전송 + 프레임 간격 확보:
                    # 이전엔 패리티 직후 다음 프레임 chunk 0을 백투백(간격 0) 전송 →
                    # 2패킷 미니버스트로 다음 프레임의 첫 청크가 드롭되어
                    # "[FEC] ... 청크 0 복원"이 반복되는 패턴이 발생. (시간 경과 저하의 신호)
                    # 이제 (데이터+패리티) 전체를 프레임 주기에 균등 분산하고 패리티 뒤에도
                    # sleep을 남겨 다음 프레임 시작 전 10ms 여유를 둔다.
                    with self._config_lock:
                        cur_cfg = self._stream_config
                    eff_fps = min(cur_cfg.target_fps if cur_cfg else DEVICE_RENDER_FPS_CAP,
                                  DEVICE_RENDER_FPS_CAP)
                    frame_period = 1.0 / eff_fps
                    total_packets = data_chunks + 1  # 데이터 + 패리티
                    # 프레임 주기에서 10ms를 뺀 구간에 (데이터+패리티)를 균등 분산 → 프레임 간 10ms 여유.
                    # inter_chunk 하한 1.5ms를 보장해 패킷이 몰리는 것을 방지한다.
                    inter_chunk = max(1.5e-3, (frame_period - 0.010) / total_packets)
                    # [PERF C/P0-2] 상대 sleep(time.sleep(inter_chunk))은 macOS 오버슈트(요청 3.3ms →
                    # 실측 5~10ms)가 누적돼 프레임 전송이 예산(예: 52ms)을 초과하고 뭉치게 된다.
                    # monotonic 절대 deadline 슬롯으로 바꿔 sendto 소요 시간까지 슬롯에 포함하고
                    # 누적 오차를 제거한다. [FIX #13/#14]의 분산·프레임 간 여유 원칙은 그대로 유지.
                    next_slot = time.monotonic() + inter_chunk

                    for chunk_idx in range(total_packets):  # 마지막 인덱스 = 패리티
                        if not self.is_streaming:
                            break
                        start_offset = chunk_idx * PAYLOAD_CHUNK_SIZE
                        header = struct.pack(">IHH", frame_id, total_chunks, chunk_idx)
                        payload = (parity_bytes if chunk_idx == data_chunks
                                   else padded[start_offset:start_offset + PAYLOAD_CHUNK_SIZE])
                        sock.sendto(header + payload, dest_addr)
                        # 절대 슬롯까지 남은 시간만 잔다(음수 금지). 뒤처진 프레임은 밀린 만큼 몰아보내지만
                        # 슬롯 기준이 프레임 시작 고정이라 다음 프레임부터 자동 정상화된다.
                        delay = next_slot - time.monotonic()
                        if delay > 0:
                            time.sleep(delay)
                        next_slot += inter_chunk

                    frame_count += 1

                    now = time.time()
                    if now - fps_start_time >= 0.8:
                        dt = now - fps_start_time
                        self.current_fps = frame_count / dt
                        self.current_frame_kb = total_len / 1024.0
                        self.current_kbps = (frame_count * total_len * 8) / (1024.0 * dt)
                        frame_count = 0
                        fps_start_time = now

                except OSError as e:
                    # [FIX #5] 소켓이 닫혔거나 네트워크 오류 → 루프 탈출
                    loop_exit_reason = f"OSError: {e}"
                    logging.warning("[WORKER:%d] 소켓/네트워크 오류로 루프 탈출: %s", my_thread_id, e)
                    break
                except Exception as e:
                    # 프레임 전송 오류는 로그로 기록하고 다음 프레임 계속 시도
                    logging.warning("[WORKER:%d] 프레임 전송 오류: %s", my_thread_id, e)

        finally:
            # [FIX #1 강화] 자신이 만든 소켓만 정리하고, self.sock 참조도 자신 것일 때만 None으로 설정.
            # 재시작 시 새 워커가 이미 self.sock을 교체했을 경우 덮어쓰지 않도록 방어.
            logging.debug("[WORKER:%d] finally 진입. 루프 종료 사유: %s", my_thread_id, loop_exit_reason)
            try:
                sock.close()
                logging.debug("[WORKER:%d] 소켓 닫기 완료.", my_thread_id)
            except Exception as e:
                logging.debug("[WORKER:%d] 소켓 닫기 중 예외(무시): %s", my_thread_id, e)
            # self.sock이 아직 자신의 소켓을 가리킬 때만 None으로 초기화
            # (새 워커가 이미 self.sock을 교체했다면 건드리지 않음)
            if self.sock is sock:
                self.sock = None
                logging.debug("[WORKER:%d] self.sock = None 처리 완료.", my_thread_id)
            else:
                logging.debug(
                    "[WORKER:%d] self.sock이 이미 새 소켓으로 교체됨. self.sock 건드리지 않음.",
                    my_thread_id,
                )
            logging.debug("[WORKER:%d] 워커 종료.", my_thread_id)

    def _capture_worker(self, capture_src: CaptureSource) -> None:
        """캡처 + 변환 + JPEG 인코딩 생산자 스레드.

        Tkinter 위젯을 건드리지 않고 _config_lock으로 설정 스냅샷을 읽습니다.
        인코딩된 JPEG를 _frame_queue에 넣고, 큐가 가득 차면 오래된 프레임을 버려 최신을 유지합니다.

        [WINCAP] capture_src가 창(kind="window")이면 매 프레임 창 위치/크기를 재조회해
        grab한다 — 윈도우 이동·리사이즈 실시간 추적. 조회는 이 스레드 전용이며
        _last_window_region(폴백)도 이 스레드만 읽고 쓴다.
        """
        my_thread_id = threading.current_thread().ident
        logging.debug("[CAP:%d] 생산자 시작. 소스=%s(%s)", my_thread_id, capture_src.kind, capture_src.label)
        # 재시작 시 self._frame_queue가 교체돼도 이 스레드의 큐를 고정 (오염 방지)
        my_queue = self._frame_queue

        try:
            with mss.mss() as sct:
                if capture_src.kind == "monitor":
                    mon_idx = capture_src.mon_idx
                    if mon_idx >= len(self.monitors_info):
                        logging.warning("[CAP:%d] 모니터 인덱스 초과(%d), 0번으로 대체.", my_thread_id, mon_idx)
                        mon_idx = 0
                    monitor = self.monitors_info[mon_idx]
                    logging.debug("[CAP:%d] 캡처 영역: 모니터 %dx%d", my_thread_id,
                                  monitor["width"], monitor["height"])
                else:
                    # [WINCAP] 시작 직후 창이 닫기는 경쟁 대비: 첫 조회 실패 시 생산자 즉시 종료
                    # (None 시그널로 소비자도 같이 종료된다).
                    first_region = self._get_window_region(capture_src.window_id)
                    if first_region is None:
                        logging.error("[CAP:%d] 시작 직후 창을 찾지 못함 (id=%d, %s). 생산자 종료.",
                                      my_thread_id, capture_src.window_id, capture_src.label)
                        return
                    self._last_window_region = first_region
                    logging.debug("[CAP:%d] 캡처 영역: 창 '%s' %dx%d", my_thread_id,
                                  capture_src.label, first_region["width"], first_region["height"])

                while self.is_streaming:
                    loop_start = time.time()

                    # [FIX #6] 스레드 안전하게 현재 설정 스냅샷 획득
                    with self._config_lock:
                        cfg = self._stream_config

                    if cfg is None:
                        time.sleep(0.01)
                        continue

                    is_portrait = cfg.rot_code in (0, 2)
                    target_w, target_h = (240, 320) if is_portrait else (320, 240)
                    # [FIX #12 → 실험 상한] 발신률 클램프. 디바이스 렌더 한계는 rt≈54ms(~18fps)지만,
                    # [LATEST-WINS]+pendingCommit으로 초과분은 폐기가 아니라 최신 프레임 교체가 되므로
                    # 실험을 위해 CAP=30까지 허용. 단 AP 다운링크 큐 적체(수초 지연)는 이 클램프로도 못 막음.
                    frame_interval = 1.0 / min(cfg.target_fps, DEVICE_RENDER_FPS_CAP)

                    try:
                        # [WINCAP] grab 영역 결정: 모니터는 고정, 창은 매 프레임 재조회(실시간 추적).
                        # 창이 닫혔거나 완전히 화면 밖이면 마지막 영역으로 계속 전송(정지 화면 유지),
                        # 폴백도 없으면 이 프레임만 스킵. 조회 비용은 [TIMING] grab 스테이지에 반영됨.
                        if capture_src.kind == "monitor":
                            region = monitor
                        else:
                            region = self._get_window_region(capture_src.window_id)
                            if region is not None:
                                self._last_window_region = region
                            else:
                                region = self._last_window_region
                                if region is None:
                                    time.sleep(frame_interval)
                                    continue

                        t0 = time.time()
                        sct_img = sct.grab(region)
                        # [PERF B1] BGRA 원본을 RGB로 미리 변환하지 않는다. 기존 frombytes("RGB", …,
                        # "BGRX")는 3MP 전체를 매 프레임 변환+복사(~9MB)했지만, frombuffer RGBA 뷰는
                        # 복사 없이 원본 바이트를 공유하고, RGB 변환을 축소 후 소형(320x240)에서 한 번만
                        # 수행해 변환 픽셀을 1/40 수준으로 줄인다.
                        img = Image.frombuffer("RGBA", sct_img.size, sct_img.bgra, "raw", "BGRA", 0, 1)
                        t1 = time.time()

                        # [FIX #10]+[PERF B2] BOX 리샘플 + 목표 비율 선크롭 + 정수배 reduce 2단 고속 축소.
                        img_final = self._fast_resize_for_display(img, target_w, target_h, cfg.aspect_mode)
                        t2 = time.time()

                        buffer = io.BytesIO()
                        # [PERF D] optimize=True: 최적화 허프만 테이블로 바이트 5~10% 절감 → 청크 수 감소 +
                        # 디바이스 디코드(엔트로피 비례) 가속. 인코딩 CPU는 늘지만 B1/B2로 확보한 여유 안이다.
                        img_final.save(buffer, format="JPEG", quality=cfg.jpeg_quality, optimize=True)
                        jpeg_bytes = buffer.getvalue()
                        t3 = time.time()

                        # [Phase 1] 스테이지별 타이밍 계측
                        self._accum_stage("grab", t1 - t0)
                        self._accum_stage("convert", t2 - t1)
                        self._accum_stage("encode", t3 - t2)
                        self._ui_frames += 1

                        # 큐가 가득 차면 오래된 프레임을 버리고 최신 유지 (지연 최소화)
                        while my_queue.full():
                            try:
                                my_queue.get_nowait()
                            except queue.Empty:
                                break
                        my_queue.put(jpeg_bytes)

                    except OSError as e:
                        # [FIX #5] 캡처 오류 → 루프 탈출
                        logging.warning("[CAP:%d] 캡처/네트워크 오류로 루프 탈출: %s", my_thread_id, e)
                        break
                    except Exception as e:
                        # [FIX #5] 프레임 처리 오류는 로그로 기록하고 다음 프레임 계속 시도
                        logging.warning("[CAP:%d] 프레임 처리 오류: %s", my_thread_id, e)

                    elapsed = time.time() - loop_start
                    sleep_time = frame_interval - elapsed
                    if sleep_time > 0:
                        time.sleep(sleep_time)

        finally:
            # 소비자가 queue.get()에서 영원히 대기하지 않도록 종료 시그널 전달
            # (고정된 my_queue로만 시그널 → 재시작 후 새 큐를 오염시키지 않음)
            try:
                my_queue.put(None)
            except Exception:
                pass
            logging.debug("[CAP:%d] 생산자 종료.", my_thread_id)

    @staticmethod
    def _downscale_box(img, cw: int, ch: int):
        """[PERF B2] img를 (cw x ch)로 축소 — 정수배 reduce 선축소 + BOX resize 마무리.

        BOX 필터 커널은 축소 배율에 비례해 넓어져 3MP→320x240 직접 축소가 비싸다.
        정수 배수만큼 reduce()(정박스 평균 전용 고속 경로)로 먼저 크게 줄이면
        남은 배율이 소수 수준이 돼 최종 BOX resize가 저렴해진다.
        """
        iw, ih = img.size
        k = min(iw // cw, ih // ch)
        if k > 1:
            img = img.reduce(k)
        return img.resize((cw, ch), Image.Resampling.BOX)

    @staticmethod
    def _fast_resize_for_display(img, tw: int, th: int, aspect_mode: int):
        """대형 스크린샷 → 표시용(tw x th) 다운스케일. 반환은 항상 RGB(JPEG 인코딩 요건).

        입력은 캡처 원본의 RGBA 뷰([PERF B1] BGRA 바이트 공유, 복사 없음)여도 무방하다 —
        RGB 변환은 축소 후 소형 이미지에서 한 번만 수행해 전체 해상도 변환(~9MB/프레임)을 제거했다.
        aspect_mode: 0=letterbox(여백) · 1=stretch(비율 무시) · 2=crop(중앙 크롭)
        """
        iw, ih = img.size
        if aspect_mode == 1:
            return CYDStreamerGUI._downscale_box(img, tw, th).convert("RGB")

        if aspect_mode == 2:
            # cover: 소스를 목표 비율(tw:th)로 중앙 크롭한 뒤 축소 (낭비 픽셀 제거)
            tar = tw / th
            if iw / ih > tar:
                nw = int(ih * tar)
                x0 = (iw - nw) // 2
                img = img.crop((x0, 0, x0 + nw, ih))
            else:
                nh = int(iw / tar)
                y0 = (ih - nh) // 2
                img = img.crop((0, y0, iw, y0 + nh))
            return CYDStreamerGUI._downscale_box(img, tw, th).convert("RGB")

        # contain(letterbox): 내용 크기(원본 비율 유지)로 축소 후 검은 여백 paste.
        # [P3-1] ImageOps.pad는 반올림 1px 차이로 내부 재축소를 유발할 수 있어 검은 캔버스+paste로 대체.
        scale = min(tw / iw, th / ih)
        cw = max(1, round(iw * scale))
        ch = max(1, round(ih * scale))
        small = CYDStreamerGUI._downscale_box(img, cw, ch).convert("RGB")
        canvas = Image.new("RGB", (tw, th), (0, 0, 0))
        canvas.paste(small, ((tw - cw) // 2, (th - ch) // 2))
        return canvas

    def _accum_stage(self, name: str, dt: float) -> None:
        """[Phase 1] 스테이지별 소요 시간을 누적하고 2초마다 로그로 출력합니다."""
        self._stage_times[name] = self._stage_times.get(name, 0.0) + dt
        self._ui_stage[name] = self._ui_stage.get(name, 0.0) + dt
        now = time.time()
        if now - self._stage_last_log >= 2.0:
            ms = {k: v * 1000 for k, v in self._stage_times.items()}
            logging.info("[TIMING] 2s 누적 → %s", " | ".join(f"{k}={v:.0f}ms" for k, v in ms.items()))
            self._stage_times.clear()
            self._stage_last_log = now

    # ------------------------------------------------------------------
    # UI 갱신
    # ------------------------------------------------------------------

    def update_stats_ui(self) -> None:
        """500ms 주기로 통계 레이블을 갱신합니다.

        [FIX #3] 창이 닫힌 후 콜백이 실행되는 경우 TclError를 잡아 정상 종료합니다.
        """
        try:
            if self.is_streaming:
                # [FIX #10] 스테이지별 평균(프레임당 ms) 표시 — 병목(캡처 vs 리사이즈 vs 인코딩) 즉시 파악용
                stage_str = ""
                if self._ui_frames > 0:
                    stage_str = " | " + " ".join(
                        f"{k}={v * 1000 / self._ui_frames:.1f}ms" for k, v in sorted(self._ui_stage.items())
                    )
                self.stats_lbl.config(
                    text=f"전송 FPS: {self.current_fps:.1f} | 프레임: {self.current_frame_kb:.1f} KB | 대역폭: {self.current_kbps:.0f} kbps{stage_str}"
                )
                self._ui_stage.clear()
                self._ui_frames = 0
            self.root.after(500, self.update_stats_ui)
        except tk.TclError:
            pass  # 창이 이미 닫혔을 때 정상 종료


def main() -> None:
    root = tk.Tk()
    app = CYDStreamerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
