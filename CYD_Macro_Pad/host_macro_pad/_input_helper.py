#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
매크로 패드 입력 헬퍼 (격리 서브프로세스)

pynput(네이티브 입력: macOS Quartz / Windows SendInput)은 네이티브 코드라 드물게
프로세스를 통째로 죽일 수 있다 (try/except로 잡을 수 없는 세그폴트 등).
이 크래시가 호스트 GUI를 죽이지 않도록, 키보드 입력(단축키/붙여넣기)만
담당하는 별도 프로세스로 분리한다.

GUI는 이 헬퍼를 subprocess로 호출하고, 헬퍼가 죽더라도 GUI는 살아남아
이벤트 로그에 실패를 표시한다.

프로토콜: 표준입력으로 JSON 한 줄(UTF-8)을 받는다.
  {"type": "shortcut", "value": "cmd+shift+4"}
  {"type": "text", "value": "안녕하세요"}
성공 시 exit 0, 실패 시 stderr에 메시지 + exit 1, pynput 미설치 시 exit 2.
"""

import json
import os
import subprocess
import sys
import tempfile

try:
    from pynput.keyboard import Controller, Key, KeyCode
except ImportError:
    print("ERROR: pynput 미설치 (pip install -r requirements.txt)", file=sys.stderr)
    sys.exit(2)


def _cmd_key():
    """cmd/command 키 해석.

    macOS : Key.cmd (실제 커맨드 키).
    Windows: pynput 1.8+에는 Key.cmd가 VK.LWIN(윈도우 키)으로 존재하지만,
             Win+... 조합은 OS가 가로채 유용하지 않다. 관례대로 cmd → Ctrl로
             해석하면 기존 macOS 설정(cmd+...)을 그대로 쓸 수 있다.
             (구버전 pynput은 Key.cmd가 아예 없어 getattr 없이는 import 크래시)
    Linux : Super(Key.cmd)보다 Ctrl이 관례.
    """
    if sys.platform == "darwin":
        return Key.cmd
    return Key.ctrl


def _win_key():
    """win/windows 키 (⊞ 윈도우 키).

    Windows : Key.cmd_l (LWIN). cmd(→Ctrl)와 달리 실제 윈도우 키로 동작한다.
              예: win+r → 실행 대화상자, win+l → 잠금.
    macOS/Linux : 실제 윈도우 키가 없으므로 OS 메타 키(Key.cmd)로 폴백.
    """
    if sys.platform == "win32":
        # pynput 전 버전에 존재하는 cmd_l(LWIN) 우선, 없으면 cmd 폴백
        return getattr(Key, "cmd_l", getattr(Key, "cmd", Key.ctrl))
    if hasattr(Key, "cmd"):
        return Key.cmd
    return Key.ctrl


# macOS 한정 키는 플랫폼마다 없을 수 있어 getattr로 방어
_KEY_MAP = {
    "cmd": _cmd_key(), "command": _cmd_key(),
    "win": _win_key(), "windows": _win_key(),
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


def _set_clipboard(text: str) -> None:
    """클립보드에 텍스트를 설정한다 (플랫폼별 OS 명령).

    macOS : pbcopy (UTF-8 stdin)
    Windows: PowerShell Set-Clipboard. clip.exe는 콘솔 코드페이지(CP949 등) 의존이라
             한글이 깨질 수 있어, BOM 포함 UTF-8 임시 파일을 만들어
             Get-Content -Encoding UTF8 -Raw로 읽어 넣는다 (코드페이지와 무관하게 안전).
    Linux : xclip -selection clipboard
    """
    # 클립보드 설정은 best-effort: 실패하면 기존 클립보드가 붙여넣어질 뿐이고,
    # OSError(pbcopy/powershell/xclip 부재 등)로 헬퍼 자체는 죽지 않는다.
    if sys.platform == "darwin":
        try:
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=False)
        except OSError:
            pass
    elif sys.platform == "win32":
        fd, path = tempfile.mkstemp(suffix=".txt")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(b"\xef\xbb\xbf" + text.encode("utf-8"))   # BOM → PS 5.1도 확실히 UTF-8
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "Get-Content -Encoding UTF8 -LiteralPath '%s' -Raw | Set-Clipboard" % path],
                    check=False)
            except OSError:
                pass
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    else:
        try:
            subprocess.run(["xclip", "-selection", "clipboard"],
                           input=text.encode("utf-8"), check=False)
        except OSError:
            pass


def exec_text(text: str) -> None:
    """클립보드 + 붙여넣기: IME 상태와 무관하게 한글 포함 텍스트 입력.

    macOS : pbcopy + Cmd+V  /  Windows: Set-Clipboard + Ctrl+V
    """
    if not text:
        return
    _set_clipboard(text)
    kb = Controller()
    paste = Key.cmd if sys.platform == "darwin" else Key.ctrl
    kb.press(paste)
    kb.press("v")
    kb.release("v")
    kb.release(paste)


def check_accessibility() -> bool:
    """macOS: 현재 프로세스 트리가 손쉬운 사용(Accessibility) 권한을 가졌는지.

    헬퍼는 호스트 GUI(.app 또는 파이썬)가 spawn하므로, 이 값은 곧 호출자의 권한
    상태다. 권한이 없으면 pynput의 키 이벤트(CGEventPost)가 예외 없이 조용히
    버려져 단축키/텍스트가 아무 오류 로그 없이 '동작하지 않는' 것처럼 보인다.
    """
    try:
        from HIServices import AXIsProcessTrusted
        return bool(AXIsProcessTrusted())
    except Exception:
        return False


def main() -> None:
    if "--trust" in sys.argv:
        # 진단 모드: stdin JSON이 아닌 권한 확인. exit 0=권한 있음, 3=없음/확인 불가.
        sys.exit(0 if check_accessibility() else 3)
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
