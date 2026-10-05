#!/usr/bin/env bash
# 처음 실행 시 가상환경을 만들고 패키지를 설치한 뒤 에이전트를 실행한다.
# 사용법: ./run.sh [작업폴더] [--model ...] [--effort ...]
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ ! -x "$DIR/.venv/bin/python" ]; then
  echo "처음 실행: 필요한 패키지를 설치합니다..."
  python3 -m venv "$DIR/.venv" || { echo "venv 생성 실패. Debian/크롬북이면: sudo apt install python3-venv"; exit 1; }
  "$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"
fi
exec "$DIR/.venv/bin/python" "$DIR/agent.py" "$@"
