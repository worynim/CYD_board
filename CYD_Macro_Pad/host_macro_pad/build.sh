#!/usr/bin/env bash
#
# CYD 무선 매크로 패드 호스트 — macOS .app 빌드 스크립트
#
# 두 스펙을 순서대로 빌드한다 (헬퍼를 먼저, 앱이 그걸 내장하므로):
#   1) macro_input_helper.spec → dist/macro_input_helper        (입력 헬퍼 단독 바이너리)
#   2) CYD Macro Pad.spec      → dist/CYD Macro Pad.app         (헬퍼를 번들 안에 내장)
#
# 사용법:
#   ./build.sh                 클린 빌드 (build/ dist/ 삭제 후 재빌드, 권장)
#   ./build.sh --skip-clean    이전 산출물을 지우지 않고 재빌드 (증분, 더 빠름)
#
# 주의: `pyinstaller` 콘솔 스크립트(/opt/homebrew/bin/pyinstaller)는 Homebrew
#   python 링크가 깨지면 "bad interpreter"로 죽으므로, 항상
#   `python3 -m PyInstaller`로 호출한다 (framework Python에 Pillow/pynput 있음).
#
set -euo pipefail
cd "$(dirname "$0")"          # spec의 상대경로(macro_pad_gui.py, dist/...) 대응

HELPER_SPEC="macro_input_helper.spec"
APP_SPEC="CYD Macro Pad.spec"
APP_BUNDLE="dist/CYD Macro Pad.app"
HELPER_EMBEDDED="$APP_BUNDLE/Contents/Frameworks/helper/macro_input_helper"

# 0) 전제조건: PyInstaller 가용 확인
if ! python3 -m PyInstaller --version >/dev/null 2>&1; then
    echo "ERR: PyInstaller를 실행할 수 없습니다. 먼저: pip install pyinstaller"
    exit 1
fi

# 1) 산출물 정리 (재현 가능한 클린 빌드가 기본)
if [[ "${1:-}" != "--skip-clean" ]]; then
    echo "==> 이전 build/ dist/ 삭제 후 클린 빌드"
    rm -rf build dist
else
    echo "==> --skip-clean: build/ dist/ 유지한 채 증분 빌드"
fi

# 2) 헬퍼 먼저 (앱 스펙이 dist/macro_input_helper 를 내장하기 때문)
echo "==> [1/2] 입력 헬퍼 빌드 ($HELPER_SPEC)"
python3 -m PyInstaller --noconfirm "$HELPER_SPEC"

# 3) 앱 번들
echo "==> [2/2] 앱 번들 빌드 ($APP_SPEC)"
python3 -m PyInstaller --noconfirm "$APP_SPEC"

# 4) 헬퍼 내장 검증 (없으면 배포 후 입력 액션이 전부 실패함)
if [[ ! -x "$HELPER_EMBEDDED" ]]; then
    echo "ERR: 앱 번들 안에 헬퍼가 없습니다 -> $HELPER_EMBEDDED"
    echo "     (헬퍼 스펙을 먼저 빌드했는지 확인)"
    exit 1
fi

echo ""
echo "OK: 빌드 완료 -> $APP_BUNDLE  ($(du -sh "$APP_BUNDLE" | cut -f1))"
echo "    헬퍼 내장 확인: $HELPER_EMBEDDED"
echo ""
echo "배포: '$APP_BUNDLE' 폴더를 통째로 대상 맥의 응용 프로그램 폴더에 복사하세요."
echo "      (.app은 디렉터리 번들이라 실행 파일만 복사하면 안 됩니다)"
echo "첫 실행 시 Gatekeeper가 막으면: 우클릭 → '열기' 또는"
echo "    xattr -dr com.apple.quarantine '$APP_BUNDLE'"
