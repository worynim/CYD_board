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
import tkinter as tk
from tkinter import ttk, messagebox
from typing import List, Dict

try:
    import mss
    from PIL import Image, ImageOps
except ImportError:
    print("[-] 필수 라이브러리가 설치되지 않았습니다: pip install mss Pillow")
    sys.exit(1)


PAYLOAD_CHUNK_SIZE = 1400


class CYDStreamerGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("CYD Wireless Monitor Control Panel")
        self.root.geometry("490x690")
        self.root.resizable(False, False)

        self.is_streaming = False
        self.stream_thread: threading.Thread | None = None
        self.sock: socket.socket | None = None

        self.current_fps = 0.0
        self.current_kbps = 0.0
        self.current_frame_kb = 0.0

        self.monitors_info = self.get_individual_monitors()

        self.setup_ui_style()
        self.create_widgets()

    def get_individual_monitors(self) -> List[Dict]:
        with mss.mss() as sct:
            if len(sct.monitors) > 1:
                return sct.monitors[1:]
            return sct.monitors

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

    def on_quality_change(self, val: str) -> None:
        q = int(float(val))
        self.q_label.config(text=f"JPEG 품질 ({q}):")

    def on_fps_change(self, val: str) -> None:
        f = int(float(val))
        self.fps_label.config(text=f"목표 FPS ({f} FPS):")

    def on_control_param_changed(self, event=None) -> None:
        if self.is_streaming and self.sock:
            ip = self.ip_entry.get().strip()
            port = int(self.port_entry.get().strip())
            rot_code = self.get_target_rotation_code(self.mon_combo.current())
            show_fps = 1 if self.show_fps_var.get() else 0
            self.send_control_command(self.sock, (ip, port), rot_code, show_fps)

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

        self.is_streaming = True
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

    def stop_streaming(self) -> None:
        self.is_streaming = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass

        self.start_btn.config(text="▶ 스트리밍 시작", bg="#22c55e")
        self.status_lbl.config(text="상태: 정지됨 (Stopped)", fg="#fbbf24")
        self.stats_lbl.config(text="전송 FPS: 0.0 | 프레임: 0.0 KB | 대역폭: 0 kbps")

        self.ip_entry.config(state="normal")
        self.port_entry.config(state="normal")
        self.mon_combo.config(state="readonly")

    def _stream_worker(self, ip: str, port: int, monitor_idx: int) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        dest_addr = (ip, port)

        rot_code = self.get_target_rotation_code(monitor_idx)
        show_fps = 1 if self.show_fps_var.get() else 0
        self.send_control_command(self.sock, dest_addr, rot_code, show_fps)

        frame_id = 0
        frame_count = 0
        fps_start_time = time.time()

        with mss.mss() as sct:
            if monitor_idx >= len(self.monitors_info):
                monitor_idx = 0
            monitor = self.monitors_info[monitor_idx]

            while self.is_streaming:
                loop_start = time.time()

                rot_code = self.get_target_rotation_code(monitor_idx)
                is_portrait = rot_code in (0, 2)
                target_w, target_h = (240, 320) if is_portrait else (320, 240)

                aspect_mode = self.aspect_combo.current()
                target_fps = max(5, int(self.fps_scale.get()))
                jpeg_quality = max(20, int(self.q_scale.get()))
                frame_interval = 1.0 / target_fps

                try:
                    sct_img = sct.grab(monitor)
                    img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

                    if aspect_mode == 0:
                        img_final = ImageOps.pad(img, (target_w, target_h), method=Image.Resampling.BILINEAR, color=(0, 0, 0))
                    elif aspect_mode == 1:
                        img_final = img.resize((target_w, target_h), Image.Resampling.BILINEAR)
                    elif aspect_mode == 2:
                        img_final = ImageOps.fit(img, (target_w, target_h), method=Image.Resampling.BILINEAR, centering=(0.5, 0.5))
                    else:
                        img_final = img.resize((target_w, target_h), Image.Resampling.BILINEAR)

                    buffer = io.BytesIO()
                    img_final.save(buffer, format="JPEG", quality=jpeg_quality)
                    jpeg_bytes = buffer.getvalue()
                    total_len = len(jpeg_bytes)

                    total_chunks = (total_len + PAYLOAD_CHUNK_SIZE - 1) // PAYLOAD_CHUNK_SIZE
                    frame_id = (frame_id + 1) & 0x7FFFFFFF

                    for chunk_idx in range(total_chunks):
                        if not self.is_streaming:
                            break
                        start_offset = chunk_idx * PAYLOAD_CHUNK_SIZE
                        end_offset = min(start_offset + PAYLOAD_CHUNK_SIZE, total_len)
                        chunk_payload = jpeg_bytes[start_offset:end_offset]

                        header = struct.pack(">IHH", frame_id, total_chunks, chunk_idx)
                        self.sock.sendto(header + chunk_payload, dest_addr)
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

                except Exception:
                    pass

    def update_stats_ui(self) -> None:
        if self.is_streaming:
            self.stats_lbl.config(
                text=f"전송 FPS: {self.current_fps:.1f} | 프레임: {self.current_frame_kb:.1f} KB | 대역폭: {self.current_kbps:.0f} kbps"
            )
        self.root.after(500, self.update_stats_ui)


def main() -> None:
    root = tk.Tk()
    app = CYDStreamerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
