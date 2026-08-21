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
    from pynput.keyboard import Controller, Key, KeyCode
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


# [FIX] 단일 문자 키의 키코드 오매핑 해결.
# pynput의 Controller.press(char)는 유니코드→키코드 맵(get_unicode_to_keycode_map)을 쓰는데,
# 이 맵은 키코드를 0..127로 순회하며 "문자 → 키코드"를 만들 때 나중에 덮어써서, 한 문자를
# 여러 키(예: '.' = 일반 마침표 47 / 키패드 소수점 65)가 만들 수 있으면 "가장 높은 키코드"를
# 선택한다. 그래서 '.'을 키패드 키(65)로 보내게 되고, 키코드는 문자 위치가 아니라 물리 키
# 위치라 활성 레이아웃/키패드 유무에 따라 실제로 입력되지 않는다 ('.', '/', 숫자 등 공통).
# 여기서는 현재 레이아웃에서 그 문자를 만드는 "가장 낮은(주) 키코드"를 직접 찾아
# KeyCode.from_vk(vk)로 보낸다. 이후는 pynput의 _handle이 키코드·수정자 플래그를 모두
# 처리하므로 cmd+. / shift+. 같은 조합도 올바르게 유지된다.
# 참고: Controller.modifiers는 컨텍스트 매니저(@contextmanager)라 set()으로 읽으면 안 된다.
_char_keycode_cache = {}


def _char_keycode(ch: str):
    """현재 키보드 레이아웃에서 ch(1자)를 입력하는 가장 낮은 키코드(주 키).

    pynput 기본 맵이 키패드 변형(높은 키코드)을 우선하는 것과 달리, 각 문자가 만들어질
    수 있는 가장 낮은 키코드를 찾아 일반(주) 키를 선택한다 ('.'=47, '/'=44, 숫자=18..29).
    못 찾으면 None → 호출부가 pynput 기본 맵으로 폴백. 결과는 캐시한다.
    """
    if ch in _char_keycode_cache:
        return _char_keycode_cache[ch]
    vk = None
    try:
        from pynput._util.darwin import keycode_context, keycode_to_string
        with keycode_context() as ctx:
            for k in range(128):
                if keycode_to_string(ctx, k) == ch:
                    vk = k
                    break
    except Exception:
        pass
    _char_keycode_cache[ch] = vk
    return vk


def exec_shortcut(s: str) -> None:
    """'cmd+shift+4' 같은 단축키를 press→release 조합으로 실행한다."""
    kb = Controller()
    actions = []                         # 각 항목: pynput Key 또는 KeyCode(주 키)
    for part in s.split("+"):
        p = part.strip().lower()
        if not p:
            continue
        if p in _KEY_MAP:
            actions.append(_KEY_MAP[p])
        elif len(p) == 1:
            vk = _char_keycode(p)
            actions.append(KeyCode.from_vk(vk) if vk is not None else p)
    if not actions:
        return
    for a in actions:
        kb.press(a)
    for a in reversed(actions):
        kb.release(a)


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
