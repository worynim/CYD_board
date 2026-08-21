#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
매크로 패드 입력 헬퍼 (격리 서브프로세스)

pynput(Quartz/CGEventPost)은 네이티브 코드라 드물게 프로세스를 통째로 죽일 수 있다
(try/except로 잡을 수 없는 세그폴트 등). 이 크래시가 호스트 GUI를 죽이지 않도록,
키보드 입력(단축키/붙여넣기)만 담당하는 별도 프로세스로 분리한다.

GUI는 이 헬퍼를 subprocess로 호출하고, 헬퍼가 죽더라도 GUI는 살아남아
이벤트 로그에 실패를 표시한다.

프로토콜: 표준입력으로 JSON 한 줄(UTF-8)을 받는다.
  {"type": "shortcut", "value": "cmd+shift+4"}
  {"type": "text", "value": "안녕하세요"}
성공 시 exit 0, 실패 시 stderr에 메시지 + exit 1, pynput 미설치 시 exit 2.
"""

import json
import subprocess
import sys

try:
    from pynput.keyboard import Controller, Key
except ImportError:
    print("ERROR: pynput 미설치 (pip install -r requirements.txt)", file=sys.stderr)
    sys.exit(2)

# macOS 한정 키는 플랫폼마다 없을 수 있어 getattr로 방어
_KEY_MAP = {
    "cmd": Key.cmd, "command": Key.cmd,
    "ctrl": Key.ctrl, "control": Key.ctrl,
    "alt": Key.alt, "option": Key.alt,
    "shift": Key.shift,
    "space": Key.space,
    "enter": Key.enter, "return": Key.enter,
    "tab": Key.tab,
    "esc": Key.esc, "escape": Key.esc,
    "up": Key.up, "down": Key.down, "left": Key.left, "right": Key.right,
    "backspace": Key.backspace,
    "delete": Key.delete,
    "caps_lock": Key.caps_lock,
    "home": Key.home, "end": Key.end,
    "page_up": Key.page_up, "page_down": Key.page_down,
}
for _fn in ("fn", "insert", "print_screen"):
    if hasattr(Key, _fn):
        _KEY_MAP[_fn] = getattr(Key, _fn)
# F1~F20 (pynput Key.f1..f20). 플랫폼마다 없는 키는 hasattr로 방어.
for _i in range(1, 21):
    _fn = "f%d" % _i
    if hasattr(Key, _fn):
        _KEY_MAP[_fn] = getattr(Key, _fn)


def exec_shortcut(s: str) -> None:
    """'cmd+shift+4' 같은 단축키를 press→release 조합으로 실행한다."""
    keys = []
    for part in s.split("+"):
        p = part.strip().lower()
        if not p:
            continue
        if p in _KEY_MAP:
            keys.append(_KEY_MAP[p])
        elif len(p) == 1:
            keys.append(p)   # 일반 문자 키
    if not keys:
        return
    kb = Controller()
    for k in keys:
        kb.press(k)
    for k in reversed(keys):
        kb.release(k)


def exec_text(text: str) -> None:
    """클립보드 + Cmd+V: IME 상태와 무관하게 한글 포함 텍스트 입력 (macOS)."""
    if not text:
        return
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=False)
    kb = Controller()
    kb.press(Key.cmd)
    kb.press("v")
    kb.release("v")
    kb.release(Key.cmd)


def main() -> None:
    raw = sys.stdin.buffer.read().decode("utf-8")
    payload = json.loads(raw)                # 잘못된 입력은 아래 except에서 exit 1
    atype = payload.get("type")
    value = payload.get("value") or ""
    if atype == "shortcut":
        exec_shortcut(value)
    elif atype == "text":
        exec_text(value)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        print("ERROR: %s" % e, file=sys.stderr)
        sys.exit(1)
