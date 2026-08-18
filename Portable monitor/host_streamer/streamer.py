#!/usr/bin/env python3
"""
CYD Portable Monitor - Low-Latency Screen Streamer (Step 3 Optimized)
청크 간 미세 슬립 및 프레임 큐 적체 방지 로직이 적용된 호스트 스트리머입니다.
"""

import sys
import time
import io
import socket
import struct
import argparse
from typing import List, Dict

try:
    import mss
    from PIL import Image
except ImportError:
    print("[-] 필수 라이브러리가 설치되지 않았습니다. 다음 명령으로 설치해주세요:")
    print("    pip install -r requirements.txt")
    sys.exit(1)


PAYLOAD_CHUNK_SIZE = 1400  # MTU(1500) 이하 안전한 UDP 청크 크기


def get_available_monitors() -> List[Dict]:
    """시스템에서 사용 가능한 모든 모니터 목록을 조회합니다."""
    with mss.mss() as sct:
        return sct.monitors


def stream_screen_udp(
    cyd_ip: str,
    port: int = 8888,
    monitor_index: int = 1,
    target_width: int = 320,
    target_height: int = 240,
    jpeg_quality: int = 45,
    target_fps: int = 25,
) -> None:
    """
    화면을 캡처하여 고속 UDP 청크로 분할 전송합니다.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # 소켓 버퍼 및 즉시 전송 최적화
    dest_addr = (cyd_ip, port)
    frame_interval = 1.0 / target_fps

    print("=" * 65)
    print(f"[*] CYD Low-Latency Streamer 시작 (Step 3 최적화)")
    print(f"[*] 대상 주소     : {cyd_ip}:{port} (AsyncUDP)")
    print(f"[*] 캡처 모니터   : #{monitor_index}")
    print(f"[*] 해상도 / 품질 : {target_width}x{target_height} / Quality {jpeg_quality}")
    print(f"[*] 목표 FPS      : {target_fps} FPS")
    print("=" * 65)
    print("[*] 종료하려면 Ctrl+C 를 누르세요.\n")

    frame_id = 0
    frame_count = 0
    fps_start_time = time.time()

    with mss.mss() as sct:
        if monitor_index >= len(sct.monitors):
            print(f"[-] 유효하지 않은 모니터 번호입니다. 전체 모니터 개수: {len(sct.monitors) - 1}")
            return

        monitor = sct.monitors[monitor_index]
        print(f"[+] 캡처 영역: {monitor['width']}x{monitor['height']} (x={monitor['left']}, y={monitor['top']})")

        try:
            while True:
                loop_start = time.time()

                # 1. 고속 화면 캡처
                sct_img = sct.grab(monitor)

                # 2. 크기 조정 (320x240) - 고속 NEAREST/BILINEAR 리사이징
                img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
                img_resized = img.resize((target_width, target_height), Image.Resampling.BILINEAR)

                # 3. JPEG 압축 (품질 40~50 권장: 프레임당 4~6KB로 전송 대역 극소화)
                buffer = io.BytesIO()
                img_resized.save(buffer, format="JPEG", quality=jpeg_quality)
                jpeg_bytes = buffer.getvalue()
                total_len = len(jpeg_bytes)

                # 4. 청크 분할 및 UDP 전송
                # 헤더 포맷: [frame_id (4B), total_chunks (2B), chunk_idx (2B)]
                total_chunks = (total_len + PAYLOAD_CHUNK_SIZE - 1) // PAYLOAD_CHUNK_SIZE
                frame_id = (frame_id + 1) & 0xFFFFFFFF

                for chunk_idx in range(total_chunks):
                    start_offset = chunk_idx * PAYLOAD_CHUNK_SIZE
                    end_offset = min(start_offset + PAYLOAD_CHUNK_SIZE, total_len)
                    chunk_payload = jpeg_bytes[start_offset:end_offset]

                    header = struct.pack(">IHH", frame_id, total_chunks, chunk_idx)
                    sock.sendto(header + chunk_payload, dest_addr)
                    # 청크 폭주로 인한 ESP32 Wi-Fi 드랍 방지 미세 간격
                    time.sleep(0.0001)

                frame_count += 1

                # 5. 실시간 FPS 및 대역폭 통계
                now = time.time()
                if now - fps_start_time >= 1.0:
                    fps = frame_count / (now - fps_start_time)
                    kb_size = total_len / 1024.0
                    bitrate_kbps = (frame_count * total_len * 8) / (1024.0 * (now - fps_start_time))
                    print(
                        f"\r[UDP Stream] Host FPS: {fps:.1f} | Frame: {kb_size:.1f} KB | Bitrate: {bitrate_kbps:.0f} kbps",
                        end="",
                        flush=True,
                    )
                    frame_count = 0
                    fps_start_time = now

                # 6. FPS 주기 맞춤
                elapsed = time.time() - loop_start
                sleep_time = frame_interval - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)

        except KeyboardInterrupt:
            print("\n\n[*] 스트리밍을 정상 종료합니다.")
        finally:
            sock.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="CYD 무선 디스플레이 초저지연 UDP 스트리머 (Mac/Windows)")
    parser.add_argument("--ip", type=str, required=True, help="CYD 보드의 IP 주소 (예: 192.168.10.56)")
    parser.add_argument("--port", type=int, default=8888, help="UDP 포트 (기본값: 8888)")
    parser.add_argument("--monitor", type=int, default=1, help="캡처할 모니터 번호 (기본값: 1)")
    parser.add_argument("--quality", type=int, default=45, help="JPEG 압축 품질 (기본값: 45)")
    parser.add_argument("--fps", type=int, default=25, help="목표 FPS (기본값: 25)")
    parser.add_argument("--list-monitors", action="store_true", help="연결된 모니터 목록 출력")

    args = parser.parse_args()

    if args.list_monitors:
        monitors = get_available_monitors()
        print("\n=== 사용 가능한 모니터 목록 ===")
        for idx, m in enumerate(monitors):
            if idx == 0:
                print(f"  [0] 전체 통합 가상 영역 ({m['width']}x{m['height']})")
            else:
                print(f"  [{idx}] 모니터 #{idx}: {m['width']}x{m['height']} (위치: x={m['left']}, y={m['top']})")
        print()
        return

    stream_screen_udp(
        cyd_ip=args.ip,
        port=args.port,
        monitor_index=args.monitor,
        jpeg_quality=args.quality,
        target_fps=args.fps,
    )


if __name__ == "__main__":
    main()
