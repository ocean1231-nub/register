@echo off
chcp 65001 >nul
rem 처음 실행 시 가상환경을 만들고 패키지를 설치한 뒤 에이전트를 실행한다.
set DIR=%~dp0
if not exist "%DIR%.venv\Scripts\python.exe" (
  echo 처음 실행: 필요한 패키지를 설치합니다...
  py -3 -m venv "%DIR%.venv" || python -m venv "%DIR%.venv"
  "%DIR%.venv\Scripts\pip" install -q -r "%DIR%requirements.txt"
)
"%DIR%.venv\Scripts\python.exe" "%DIR%agent.py" %*
