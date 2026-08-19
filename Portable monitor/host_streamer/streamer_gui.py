#!/usr/bin/env python3
"""
CYD Portable Monitor - Stable High-Speed GUI Streamer
- 캡처 화면: 개별 모니터 선택 (0번 전체 영역 제외)
- 화면 비율 모드: Letterbox(원본 비율 유지), Stretch(채우기), Crop(크롭 맞춤)
- 안정적인 청크 송신 제어로 패킷 유실 방지 및 깔끔한 화면 전송
"""

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
from typing import List, Dict

try:
    import mss
    from PIL import Image, ImageOps
except ImportError:
    print("[-] 필수 라이브러리가 설치되지 않았습니다: pip install mss Pillow")
    sys.exit(1)


logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)

# UDP 데이터 페이로드 크기(바이트). 펌웨어 CYD_Wireless_Monitor.ino의
# PACKET_PAYLOAD_SIZE(1400)와 반드시 일치해야 하는 크로스파일 계약 상수.
# 다르면 청크 경계가 어긋나 화면이 깨지므로, 한쪽을 바꿀 때는 반드시 함께 바꿀 것.
PAYLOAD_CHUNK_SIZE = 1400

# [FIX #12] 디바이스 렌더 상한. rt≈54ms(픽셀 푸시 17ms + 디코드 37ms) → 최대 ~18fps.
# 이보다 빠르게 전송하면 commitFrame()의 isRendering 가드에 걸려 프레임이 폐기됨
# (호스트 전송 FPS만 높고 CYD는 recv≈0·drop 폭증). 안전 마진을 두고 20fps로 클램프.
DEVICE_RENDER_FPS_CAP = 20


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

        # 2. 모니터 선택
        mon_frame = tk.Frame(card, bg=self.card_bg)
        mon_frame.pack(fill=tk.X, pady=4)

        ttk.Label(mon_frame, text="캡처 화면:").pack(side=tk.LEFT)
        monitor_options = []
        for idx, m in enumerate(self.monitors_info):
            orient = "세로" if m["height"] > m["width"] else "가로"
            monitor_options.append(f"모니터 #{idx + 1} [{orient}] ({m['width']}x{m['height']})")

        self.mon_combo = ttk.Combobox(mon_frame, values=monitor_options, state="readonly", width=28)
        self.mon_combo.current(0)
        self.mon_combo.pack(side=tk.LEFT, padx=6)

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

        self.q_label = ttk.Label(q_frame, text="JPEG 품질 (45):")
        self.q_label.pack(anchor="w")

        self.q_scale = ttk.Scale(q_frame, from_=20, to=85, orient=tk.HORIZONTAL, command=self.on_quality_change)
        self.q_scale.set(45)
        self.q_scale.pack(fill=tk.X, pady=2)

        # 7. 목표 FPS
        fps_frame = tk.Frame(card, bg=self.card_bg)
        fps_frame.pack(fill=tk.X, pady=4)

        self.fps_label = ttk.Label(fps_frame, text="목표 FPS (16 FPS):")
        self.fps_label.pack(anchor="w")

        # [FIX #12] 디바이스 렌더가 54ms/frame(≈18fps 상한)이므로 기본 16으로 설정.
        # 위로 올려도 안전 상한 16에서 전송량이 클램프됨(아래 _capture_worker 참조).
        self.fps_scale = ttk.Scale(fps_frame, from_=10, to=30, orient=tk.HORIZONTAL, command=self.on_fps_change)
        self.fps_scale.set(16)
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
        rot_code = self.get_target_rotation_code(self.mon_combo.current())
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
        # [FIX #12] 디바이스 렌더 상한(≈16fps) 반영해 실제 전송 FPS를 표시
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
    def get_target_rotation_code(self, monitor_idx: int) -> int:
        idx = self.rot_combo.current()
        if idx == 0:
            monitor = self.monitors_info[monitor_idx]
            if monitor["height"] > monitor["width"]:
                return 0
            else:
                return 3
        elif idx == 1:
            return 3
        elif idx == 2:
            return 1
        elif idx == 3:
            return 0
        elif idx == 4:
            return 2
        return 3

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

        sel_mon = self.mon_combo.current()
        if sel_mon < 0:
            sel_mon = 0

        # [DEBUG] 이전 워커/캡처 스레드 생존 여부 확인
        for tname, t in (("워커", self.stream_thread), ("캡처", self.capture_thread)):
            if t is not None and t.is_alive():
                logging.warning("[START] 이전 %s 스레드가 아직 살아있음! 재시작 경쟁 조건 위험. 잠시 대기...", tname)
                t.join(timeout=2.0)
                if t.is_alive():
                    logging.error("[START] 이전 %s 스레드가 2초 내 종료되지 않음. 강제 진행.", tname)
                else:
                    logging.debug("[START] 이전 %s 스레드 정상 종료 확인.", tname)

        logging.debug("[START] 스트리밍 시작 → %s:%d, 모니터=%d", ip, port, sel_mon)
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
        self.capture_thread = threading.Thread(target=self._capture_worker, args=(sel_mon,), daemon=True)
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

                    for chunk_idx in range(total_packets):  # 마지막 인덱스 = 패리티
                        if not self.is_streaming:
                            break
                        start_offset = chunk_idx * PAYLOAD_CHUNK_SIZE
                        header = struct.pack(">IHH", frame_id, total_chunks, chunk_idx)
                        payload = (parity_bytes if chunk_idx == data_chunks
                                   else padded[start_offset:start_offset + PAYLOAD_CHUNK_SIZE])
                        sock.sendto(header + payload, dest_addr)
                        time.sleep(inter_chunk)  # [FIX #13/#14] 전체 패킷 균등 분산 + 프레임 간 여유

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

    def _capture_worker(self, monitor_idx: int) -> None:
        """캡처 + 변환 + JPEG 인코딩 생산자 스레드.

        Tkinter 위젯을 건드리지 않고 _config_lock으로 설정 스냅샷을 읽습니다.
        인코딩된 JPEG를 _frame_queue에 넣고, 큐가 가득 차면 오래된 프레임을 버려 최신을 유지합니다.
        """
        my_thread_id = threading.current_thread().ident
        logging.debug("[CAP:%d] 생산자 시작. 모니터=%d", my_thread_id, monitor_idx)
        # 재시작 시 self._frame_queue가 교체돼도 이 스레드의 큐를 고정 (오염 방지)
        my_queue = self._frame_queue

        try:
            with mss.mss() as sct:
                if monitor_idx >= len(self.monitors_info):
                    logging.warning("[CAP:%d] 모니터 인덱스 초과(%d), 0번으로 대체.", my_thread_id, monitor_idx)
                    monitor_idx = 0
                monitor = self.monitors_info[monitor_idx]
                logging.debug("[CAP:%d] 캡처 영역: %dx%d", my_thread_id, monitor["width"], monitor["height"])

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
                    # [FIX #12] 디바이스 렌더(rt≈54ms → ~18fps)를 넘는 전송은 커밋이 렌더 중
                    # 차단돼 프레임이 폐기됨. 안전 상한(16fps)으로 전송률을 클램프.
                    frame_interval = 1.0 / min(cfg.target_fps, DEVICE_RENDER_FPS_CAP)

                    try:
                        t0 = time.time()
                        sct_img = sct.grab(monitor)
                        img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
                        t1 = time.time()

                        # [FIX #10] 대형 소스(최대 3.7MP) 다운스케일은 BOX 리샘플 사용.
                        # 소스 전체에 BILINEAR를 직접 걸면 세로 모니터(1296x2304) 기준 ~25ms → 호스트 15fps 벽.
                        # BOX(C 타일 필터, 대형 축소 전용) + 목표 비율 선크롭으로 절반 이하로 단축.
                        img_final = self._fast_resize_for_display(img, target_w, target_h, cfg.aspect_mode)
                        t2 = time.time()

                        buffer = io.BytesIO()
                        img_final.save(buffer, format="JPEG", quality=cfg.jpeg_quality)
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
    def _fast_resize_for_display(img, tw: int, th: int, aspect_mode: int):
        """대형 스크린샷 → 표시용(tw x th) 다운스케일. BILINEAR 대신 BOX 리샘플 사용.

        소스 전체에 BILINEAR를 걸면 픽셀 2D 필터가 3.7MP 전체를 훑어 느림.
        BOX(C 타일 박스 필터, 대형 축소 전용)로 한 번에 줄이고, 크롭 모드는
        목표 비율로 먼저 자른 뒤 축소해 낭비 픽셀을 없앱니다.
        aspect_mode: 0=letterbox(여백) · 1=stretch(비율 무시) · 2=crop(중앙 크롭)
        """
        iw, ih = img.size
        if aspect_mode == 1:
            return img.resize((tw, th), Image.Resampling.BOX)

        if aspect_mode == 2:
            # cover: 소스를 목표 비율(tw:th)로 중앙 크롭한 뒤 BOX 축소
            tar = tw / th
            if iw / ih > tar:
                nw = int(ih * tar)
                x0 = (iw - nw) // 2
                img = img.crop((x0, 0, x0 + nw, ih))
            else:
                nh = int(iw / tar)
                y0 = (ih - nh) // 2
                img = img.crop((0, y0, iw, y0 + nh))
            return img.resize((tw, th), Image.Resampling.BOX)

        # contain(letterbox): 내용 크기(원본 비율 유지)로 BOX 축소 후 검은 여백만 pad
        scale = min(tw / iw, th / ih)
        cw = max(1, round(iw * scale))
        ch = max(1, round(ih * scale))
        small = img.resize((cw, ch), Image.Resampling.BOX)
        return ImageOps.pad(small, (tw, th), method=Image.Resampling.BILINEAR, color=(0, 0, 0))

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
