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

PAYLOAD_CHUNK_SIZE = 1400


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
        self.stream_thread: threading.Thread | None = None
        self.sock: socket.socket | None = None

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

        self.fps_label = ttk.Label(fps_frame, text="목표 FPS (25 FPS):")
        self.fps_label.pack(anchor="w")

        self.fps_scale = ttk.Scale(fps_frame, from_=10, to=30, orient=tk.HORIZONTAL, command=self.on_fps_change)
        self.fps_scale.set(25)
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

        # 회전·FPS 오버레이 제어 명령을 CYD로 전송
        ip = self.ip_entry.get().strip()
        try:
            port = int(self.port_entry.get().strip())  # [FIX #4] ValueError 안전 처리
        except ValueError:
            return
        self.send_control_command(self.sock, (ip, port), cfg.rot_code, cfg.show_fps)

    # ------------------------------------------------------------------
    # UI 콜백
    # ------------------------------------------------------------------

    def on_quality_change(self, val: str) -> None:
        q = int(float(val))
        self.q_label.config(text=f"JPEG 품질 ({q}):")
        self._update_stream_config()

    def on_fps_change(self, val: str) -> None:
        f = int(float(val))
        self.fps_label.config(text=f"목표 FPS ({f} FPS):")
        self._update_stream_config()

    def on_control_param_changed(self, event=None) -> None:
        self._update_stream_config()

    # ------------------------------------------------------------------
    # 네트워크
    # ------------------------------------------------------------------

    def send_control_command(self, sock: socket.socket, dest_addr: tuple, rot_val: int, show_fps: int) -> None:
        try:
            cmd_packet = struct.pack(">IBBBB", 0xFFFFFFFF, rot_val, show_fps, 0, 0)
            sock.sendto(cmd_packet, dest_addr)
        except Exception:
            pass

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

        # [DEBUG] 이전 워커 스레드 생존 여부 확인
        if self.stream_thread is not None and self.stream_thread.is_alive():
            logging.warning("[START] 이전 워커 스레드가 아직 살아있음! 재시작 경쟁 조건 위험. 잠시 대기...")
            self.stream_thread.join(timeout=2.0)
            if self.stream_thread.is_alive():
                logging.error("[START] 이전 워커 스레드가 2초 내 종료되지 않음. 강제 진행.")
            else:
                logging.debug("[START] 이전 워커 스레드 정상 종료 확인.")

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

        self.stream_thread = threading.Thread(
            target=self._stream_worker,
            args=(ip, port, sel_mon),
            daemon=True,
        )
        self.stream_thread.start()
        logging.debug("[START] 워커 스레드 시작됨 (id=%d)", self.stream_thread.ident or -1)

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
        if self.stream_thread is not None and self.stream_thread.is_alive():
            logging.debug("[CLOSE] 워커 스레드 종료 대기 중...")
            self.stream_thread.join(timeout=2.0)
        logging.debug("[CLOSE] 창 종료.")
        self.root.destroy()

    # ------------------------------------------------------------------
    # 워커 스레드
    # ------------------------------------------------------------------

    def _stream_worker(self, ip: str, port: int, monitor_idx: int) -> None:
        """화면 캡처 및 UDP 전송 워커 스레드.

        소켓의 생명주기 전체(생성~종료)를 이 메서드가 책임집니다.
        """
        my_thread_id = threading.current_thread().ident
        logging.debug("[WORKER:%d] 워커 시작. 대상=%s:%d, 모니터=%d", my_thread_id, ip, port, monitor_idx)

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

        frame_id = 0
        frame_count = 0
        fps_start_time = time.time()
        loop_exit_reason = "is_streaming=False (정상 종료)"

        try:
            with mss.mss() as sct:
                if monitor_idx >= len(self.monitors_info):
                    logging.warning("[WORKER:%d] 모니터 인덱스 초과(%d), 0번으로 대체.", my_thread_id, monitor_idx)
                    monitor_idx = 0
                monitor = self.monitors_info[monitor_idx]
                logging.debug("[WORKER:%d] 캡처 영역: %dx%d", my_thread_id, monitor["width"], monitor["height"])

                while self.is_streaming:
                    loop_start = time.time()

                    # [FIX #6] 스레드 안전하게 현재 설정 스냅샷 획득
                    # tkinter 위젯을 워커 스레드에서 직접 읽지 않음
                    with self._config_lock:
                        cfg = self._stream_config

                    if cfg is None:
                        time.sleep(0.01)
                        continue

                    is_portrait = cfg.rot_code in (0, 2)
                    target_w, target_h = (240, 320) if is_portrait else (320, 240)
                    frame_interval = 1.0 / cfg.target_fps

                    try:
                        sct_img = sct.grab(monitor)
                        img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

                        if cfg.aspect_mode == 0:
                            img_final = ImageOps.pad(img, (target_w, target_h), method=Image.Resampling.BILINEAR, color=(0, 0, 0))
                        elif cfg.aspect_mode == 1:
                            img_final = img.resize((target_w, target_h), Image.Resampling.BILINEAR)
                        elif cfg.aspect_mode == 2:
                            img_final = ImageOps.fit(img, (target_w, target_h), method=Image.Resampling.BILINEAR, centering=(0.5, 0.5))
                        else:
                            img_final = img.resize((target_w, target_h), Image.Resampling.BILINEAR)

                        buffer = io.BytesIO()
                        img_final.save(buffer, format="JPEG", quality=cfg.jpeg_quality)
                        jpeg_bytes = buffer.getvalue()
                        total_len = len(jpeg_bytes)

                        total_chunks = (total_len + PAYLOAD_CHUNK_SIZE - 1) // PAYLOAD_CHUNK_SIZE
                        # [FIX #2] frame_id 마스크를 streamer.py와 통일 (0x7FFFFFFF)
                        # 0xFFFFFFFF는 컨트롤 패킷 식별자로 예약되어 있으므로 명확히 구분
                        frame_id = (frame_id + 1) & 0x7FFFFFFF

                        for chunk_idx in range(total_chunks):
                            if not self.is_streaming:
                                break
                            start_offset = chunk_idx * PAYLOAD_CHUNK_SIZE
                            end_offset = min(start_offset + PAYLOAD_CHUNK_SIZE, total_len)
                            chunk_payload = jpeg_bytes[start_offset:end_offset]

                            header = struct.pack(">IHH", frame_id, total_chunks, chunk_idx)
                            sock.sendto(header + chunk_payload, dest_addr)
                            time.sleep(0.0001)  # 청크 간 미세 간격으로 패킷 폭주 방지

                        frame_count += 1

                        now = time.time()
                        if now - fps_start_time >= 0.8:
                            dt = now - fps_start_time
                            self.current_fps = frame_count / dt
                            self.current_frame_kb = total_len / 1024.0
                            self.current_kbps = (frame_count * total_len * 8) / (1024.0 * dt)
                            frame_count = 0
                            fps_start_time = now

                        elapsed = time.time() - loop_start
                        sleep_time = frame_interval - elapsed
                        if sleep_time > 0:
                            time.sleep(sleep_time)

                    except OSError as e:
                        # [FIX #5] 소켓이 닫혔거나 네트워크 오류 → 루프 탈출
                        loop_exit_reason = f"OSError: {e}"
                        logging.warning("[WORKER:%d] 소켓/네트워크 오류로 루프 탈출: %s", my_thread_id, e)
                        break
                    except Exception as e:
                        # [FIX #5] 프레임 처리 오류는 로그로 기록하고 다음 프레임 계속 시도
                        logging.warning("[WORKER:%d] 프레임 처리 오류: %s", my_thread_id, e)

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

    # ------------------------------------------------------------------
    # UI 갱신
    # ------------------------------------------------------------------

    def update_stats_ui(self) -> None:
        """500ms 주기로 통계 레이블을 갱신합니다.

        [FIX #3] 창이 닫힌 후 콜백이 실행되는 경우 TclError를 잡아 정상 종료합니다.
        """
        try:
            if self.is_streaming:
                self.stats_lbl.config(
                    text=f"전송 FPS: {self.current_fps:.1f} | 프레임: {self.current_frame_kb:.1f} KB | 대역폭: {self.current_kbps:.0f} kbps"
                )
            self.root.after(500, self.update_stats_ui)
        except tk.TclError:
            pass  # 창이 이미 닫혔을 때 정상 종료


def main() -> None:
    root = tk.Tk()
    app = CYDStreamerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
