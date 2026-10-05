#!/usr/bin/env python3
"""로컬 파일 작업 에이전트 (Claude API).

터미널에서 대화하면서, 지정한 작업 폴더 안의 파일을 읽고/찾고/고치고,
명령을 실행하는 에이전트. 파일 변경과 명령 실행은 매번 사용자 승인을 받는다.

사용법:
    python agent.py [작업폴더] [--model MODEL] [--effort LEVEL]
"""

from __future__ import annotations

import argparse
import difflib
import fnmatch
import getpass
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    import readline  # noqa: F401  (Linux/macOS 입력 기록)
except ImportError:
    pass

try:
    import anthropic
except ImportError:
    sys.exit("anthropic 패키지가 없습니다. 먼저 실행하세요:  pip install -r requirements.txt")

# ---------------------------------------------------------------- 설정

# 가격: USD / 100만 토큰 (출처: https://platform.claude.com/docs/en/about-claude/pricing, 2026-10 확인)
MODELS = {
    "claude-opus-5-5": {"input": 4.00, "cache_write": 5.00, "cache_read": 0.20, "output": 20.00},
    "claude-sonnet-5-5": {"input": 2.00, "cache_write": 2.50, "cache_read": 0.20, "output": 10.00},
    "claude-haiku-4-5": {"input": 1.00, "cache_write": 1.25, "cache_read": 0.10, "output": 5.00},
}
DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}  # 서버측 거절 대체(fallbacks: "default") 지원
EFFORT_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}  # effort / adaptive thinking 지원
MAX_TOKENS = 64000
CONFIG_DIR = Path.home() / ".config" / "claude-file-agent"
CONFIG_FILE = CONFIG_DIR / "config.json"
TRASH_DIR = ".agent-trash"
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", TRASH_DIR}
READ_CHAR_LIMIT = 100_000
OUTPUT_CHAR_LIMIT = 30_000
COMMAND_TIMEOUT = 120

if os.name == "nt":
    os.system("")  # Windows 콘솔에서 ANSI 색상 활성화

DIM, BOLD, RED, GREEN, YELLOW, CYAN, RESET = (
    "\033[2m", "\033[1m", "\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[0m",
)


def say(msg: str, color: str = "") -> None:
    print(f"{color}{msg}{RESET}" if color else msg, flush=True)


# ---------------------------------------------------------------- API 키

def load_api_key() -> str | None:
    """환경변수 -> 저장된 설정 -> 직접 입력 순서로 API 키를 찾는다."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return None  # SDK가 환경변수를 그대로 사용
    try:
        key = json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("api_key")
        if key:
            return key
    except (OSError, ValueError):
        pass
    say("Anthropic API 키가 필요합니다. (https://platform.claude.com/settings/keys 에서 발급)", YELLOW)
    key = getpass.getpass("API 키 입력 (화면에 표시되지 않음): ").strip()
    if not key:
        sys.exit("API 키가 없어 종료합니다.")
    if input("이 컴퓨터에 저장할까요? 다음부터 묻지 않습니다 [y/N]: ").strip().lower() == "y":
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps({"api_key": key}), encoding="utf-8")
        try:
            CONFIG_FILE.chmod(0o600)
        except OSError:
            pass
        say(f"저장됨: {CONFIG_FILE}", DIM)
    return key


# ---------------------------------------------------------------- 도구 정의

def tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


STR = {"type": "string"}
INT = {"type": "integer"}

TOOLS = [
    tool("list_files",
         "작업 폴더 안의 파일과 폴더 목록을 본다. pattern을 주면 하위 폴더까지 glob으로 찾는다 (예: '**/*.md').",
         {"path": {**STR, "description": "작업 폴더 기준 상대 경로. 기본 '.'"},
          "pattern": {**STR, "description": "선택. glob 패턴"}},
         []),
    tool("read_file",
         "텍스트 파일을 줄 번호와 함께 읽는다. 큰 파일은 offset/limit로 나눠 읽는다.",
         {"path": STR,
          "offset": {**INT, "description": "시작 줄 (1부터). 기본 1"},
          "limit": {**INT, "description": "읽을 줄 수. 기본 2000"}},
         ["path"]),
    tool("search_files",
         "작업 폴더의 텍스트 파일 내용을 정규식으로 검색한다.",
         {"query": {**STR, "description": "정규식 (잘못된 정규식이면 일반 문자열로 검색)"},
          "path": {**STR, "description": "검색할 하위 폴더. 기본 '.'"},
          "glob": {**STR, "description": "선택. 파일 이름 필터 (예: '*.py')"}},
         ["query"]),
    tool("write_file",
         "파일을 새로 만들거나 전체 내용을 덮어쓴다. 사용자 승인이 필요하다. 일부만 고칠 때는 edit_file을 쓴다.",
         {"path": STR, "content": STR},
         ["path", "content"]),
    tool("edit_file",
         "파일에서 old_text를 찾아 new_text로 바꾼다. old_text는 파일 안에서 정확히 한 번만 나와야 한다. 사용자 승인이 필요하다.",
         {"path": STR, "old_text": STR, "new_text": STR},
         ["path", "old_text", "new_text"]),
    tool("move_file",
         "파일이나 폴더를 옮기거나 이름을 바꾼다. 사용자 승인이 필요하다.",
         {"source": STR, "destination": STR},
         ["source", "destination"]),
    tool("delete_file",
         f"파일이나 폴더를 삭제한다. 실제로는 작업 폴더의 {TRASH_DIR}/ 로 옮겨서 복구할 수 있다. 사용자 승인이 필요하다.",
         {"path": STR},
         ["path"]),
    tool("run_command",
         f"작업 폴더에서 셸 명령을 실행하고 출력을 돌려받는다 (제한 시간 {COMMAND_TIMEOUT}초). 사용자 승인이 필요하다.",
         {"command": STR},
         ["command"]),
]
TOOL_SCHEMAS = {t["name"]: t["input_schema"] for t in TOOLS}
NEEDS_APPROVAL = {"write_file", "edit_file", "move_file", "delete_file", "run_command"}


def validate_input(name: str, args: object) -> str | None:
    """스트리밍된 도구 입력을 스키마와 대조한다. 문제가 없으면 None."""
    schema = TOOL_SCHEMAS.get(name)
    if schema is None:
        return f"알 수 없는 도구: {name}"
    if not isinstance(args, dict):
        return "입력이 객체가 아닙니다"
    props = schema["properties"]
    for key in schema["required"]:
        if key not in args:
            return f"필수 항목 누락: {key}"
    for key, value in args.items():
        if key not in props:
            return f"알 수 없는 항목: {key}"
        expected = props[key]["type"]
        if expected == "string" and not isinstance(value, str):
            return f"{key}는 문자열이어야 합니다"
        if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
            return f"{key}는 정수여야 합니다"
    return None


# ---------------------------------------------------------------- 도구 실행

class ToolError(Exception):
    pass


class Workspace:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.always_allow: set[str] = set()

    def resolve(self, rel: str) -> Path:
        """모델이 준 경로를 작업 폴더 안으로 제한한다 (.., 절대경로, 심볼릭 링크 탈출 차단)."""
        target = (self.root / rel).resolve()
        if target != self.root and not target.is_relative_to(self.root):
            raise ToolError(f"작업 폴더 밖의 경로는 사용할 수 없습니다: {rel}")
        return target

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix() or "."

    # --- 읽기 전용 (승인 없이 실행)

    def list_files(self, path: str = ".", pattern: str | None = None) -> str:
        base = self.resolve(path)
        if not base.is_dir():
            raise ToolError(f"폴더가 아닙니다: {path}")
        if pattern:
            hits = [p for p in base.glob(pattern) if not (set(p.relative_to(self.root).parts) & SKIP_DIRS)]
            lines = [self.rel(p) + ("/" if p.is_dir() else "") for p in sorted(hits)[:500]]
            return "\n".join(lines) or "(일치하는 파일 없음)"
        lines = []
        for p in sorted(base.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if p.name in SKIP_DIRS:
                continue
            if p.is_dir():
                lines.append(f"{p.name}/")
            else:
                lines.append(f"{p.name}  ({p.stat().st_size:,} bytes)")
        return "\n".join(lines[:500]) or "(빈 폴더)"

    def _read_text(self, p: Path) -> str:
        if not p.is_file():
            raise ToolError(f"파일이 없습니다: {self.rel(p) if p.is_relative_to(self.root) else p}")
        with p.open("rb") as f:
            if b"\0" in f.read(8192):
                raise ToolError("바이너리 파일은 읽을 수 없습니다")
        return p.read_text(encoding="utf-8", errors="replace")

    def read_file(self, path: str, offset: int = 1, limit: int = 2000) -> str:
        lines = self._read_text(self.resolve(path)).splitlines()
        start = max(offset, 1) - 1
        chunk = lines[start:start + max(limit, 1)]
        out = "\n".join(f"{i}\t{line}" for i, line in enumerate(chunk, start + 1))
        if len(out) > READ_CHAR_LIMIT:
            out = out[:READ_CHAR_LIMIT] + "\n... (글자 수 제한으로 잘림 - offset/limit로 나눠 읽으세요)"
        if start + len(chunk) < len(lines):
            out += f"\n... (전체 {len(lines)}줄 중 {start + 1}-{start + len(chunk)}줄)"
        return out or "(빈 파일)"

    def search_files(self, query: str, path: str = ".", glob: str | None = None) -> str:
        try:
            rx = re.compile(query)
        except re.error:
            rx = re.compile(re.escape(query))
        base = self.resolve(path)
        results = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                if glob and not fnmatch.fnmatch(name, glob):
                    continue
                p = Path(dirpath) / name
                try:
                    if p.stat().st_size > 2_000_000:
                        continue
                    text = self._read_text(p)
                except (ToolError, OSError):
                    continue
                for i, line in enumerate(text.splitlines(), 1):
                    if rx.search(line):
                        results.append(f"{self.rel(p)}:{i}: {line.strip()[:200]}")
                        if len(results) >= 200:
                            return "\n".join(results) + "\n... (200개에서 중단)"
        return "\n".join(results) or "(일치하는 내용 없음)"

    # --- 변경 (승인 후 실행)

    def write_file(self, path: str, content: str) -> str:
        p = self.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"저장 완료: {self.rel(p)} ({len(content):,}자)"

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        p = self.resolve(path)
        text = self._read_text(p)
        count = text.count(old_text)
        if count != 1:
            raise ToolError(f"old_text가 {count}번 나옵니다. 정확히 한 번 나오도록 앞뒤 문맥을 더 넣으세요.")
        p.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"수정 완료: {self.rel(p)}"

    def move_file(self, source: str, destination: str) -> str:
        src, dst = self.resolve(source), self.resolve(destination)
        if not src.exists():
            raise ToolError(f"없는 경로: {source}")
        if dst.exists():
            raise ToolError(f"대상이 이미 있습니다: {destination}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return f"이동 완료: {self.rel(src)} -> {self.rel(dst)}"

    def delete_file(self, path: str) -> str:
        p = self.resolve(path)
        if p == self.root:
            raise ToolError("작업 폴더 자체는 삭제할 수 없습니다")
        if not p.exists():
            raise ToolError(f"없는 경로: {path}")
        dest = self.root / TRASH_DIR / time.strftime("%Y%m%d-%H%M%S") / self.rel(p)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(dest))
        return f"삭제 완료 (복구 가능: {self.rel(dest)})"

    def run_command(self, command: str) -> str:
        try:
            r = subprocess.run(command, shell=True, cwd=self.root, capture_output=True,
                               text=True, errors="replace", timeout=COMMAND_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise ToolError(f"{COMMAND_TIMEOUT}초 안에 끝나지 않아 중단했습니다")
        out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
        if len(out) > OUTPUT_CHAR_LIMIT:
            out = out[:OUTPUT_CHAR_LIMIT // 2] + "\n... (중략) ...\n" + out[-OUTPUT_CHAR_LIMIT // 2:]
        return f"[종료 코드 {r.returncode}]\n{out.strip()}"

    # --- 승인 화면

    def preview(self, name: str, args: dict) -> str:
        """승인 전에 보여줄 변경 미리보기."""
        if name == "run_command":
            return f"$ {args['command']}"
        if name == "move_file":
            return f"{args['source']}  ->  {args['destination']}"
        if name == "delete_file":
            return f"삭제: {args['path']}  ({TRASH_DIR}/ 로 이동)"
        p = self.resolve(args["path"])
        old = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
        if name == "write_file":
            new = args["content"]
        else:
            if old.count(args["old_text"]) != 1:
                return f"{args['path']} 수정 (old_text를 찾지 못해 실행 시 오류가 납니다)"
            new = old.replace(args["old_text"], args["new_text"], 1)
        diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                         f"{args['path']} (현재)", f"{args['path']} (변경 후)",
                                         lineterm="", n=2))
        if not diff:
            return f"{args['path']}: 변경 내용 없음"
        colored = []
        for line in diff[:80]:
            if line.startswith("+") and not line.startswith("+++"):
                colored.append(GREEN + line + RESET)
            elif line.startswith("-") and not line.startswith("---"):
                colored.append(RED + line + RESET)
            else:
                colored.append(line)
        if len(diff) > 80:
            colored.append(f"{DIM}... ({len(diff) - 80}줄 더){RESET}")
        return "\n".join(colored)

    def ask_approval(self, name: str, args: dict) -> tuple[bool, str]:
        """(허용 여부, 거부 시 사용자 피드백)."""
        if name in self.always_allow:
            return True, ""
        try:
            preview = self.preview(name, args)
        except ToolError:
            return True, ""  # 실행 단계에서 같은 오류를 모델에게 돌려준다
        say(f"\n{YELLOW}{BOLD}[승인 요청] {name}{RESET}")
        print(preview)
        while True:
            ans = input(f"{YELLOW}허용? [y] 예  [a] 이번 실행 동안 {name} 항상 허용  [n] 거부: {RESET}").strip().lower()
            if ans in ("y", "yes", "ㅛ"):
                return True, ""
            if ans in ("a", "ㅁ"):
                self.always_allow.add(name)
                return True, ""
            if ans in ("n", "no", "ㅜ", ""):
                fb = input(f"{YELLOW}거부 이유나 원하는 방향 (엔터로 생략): {RESET}").strip()
                return False, fb

    def execute(self, name: str, args: dict) -> tuple[str, bool]:
        """도구를 실행하고 (결과, 오류 여부)를 돌려준다."""
        if name in NEEDS_APPROVAL:
            ok, feedback = self.ask_approval(name, args)
            if not ok:
                msg = "사용자가 이 작업을 거부했습니다."
                if feedback:
                    msg += f" 사용자 피드백: {feedback}"
                return msg, True
        try:
            return getattr(self, name)(**args), False
        except ToolError as e:
            return f"오류: {e}", True
        except OSError as e:
            return f"오류: {e.strerror or e}", True


# ---------------------------------------------------------------- 비용

class CostMeter:
    def __init__(self, model: str):
        self.price = MODELS[model]
        self.tokens = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}

    def add(self, usage) -> float:
        before = self.total()
        self.tokens["input"] += usage.input_tokens or 0
        self.tokens["cache_write"] += getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.tokens["cache_read"] += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.tokens["output"] += usage.output_tokens or 0
        return self.total() - before

    def total(self) -> float:
        return sum(self.tokens[k] * self.price[k] for k in self.tokens) / 1_000_000

    def report(self) -> str:
        t = self.tokens
        return (f"입력 {t['input']:,} / 캐시쓰기 {t['cache_write']:,} / 캐시읽기 {t['cache_read']:,} / "
                f"출력 {t['output']:,} 토큰  ->  약 ${self.total():.4f}")


# ---------------------------------------------------------------- 에이전트

SYSTEM_PROMPT = """당신은 사용자의 컴퓨터에서 동작하는 파일 작업 도우미입니다.
사용자와 한국어로 대화하며, 작업 폴더 안의 파일을 도구로 읽고, 찾고, 고치고, 명령을 실행해 요청을 처리합니다.

작업 폴더: {root}
운영체제: {os}

원칙:
- 파일을 고치기 전에 관련 파일을 먼저 읽고 현재 상태를 확인하세요.
- 변경 도구(write_file, edit_file, move_file, delete_file, run_command)는 실행 전에 사용자가 승인합니다. 사용자가 거부하고 피드백을 주면, 그 피드백을 반영해 방법을 바꾸세요. 같은 작업을 그대로 다시 요청하지 마세요.
- 요청이 모호하거나, 되돌리기 어려운 큰 변경이라면 실행 전에 짧게 계획을 설명하고 확인을 받으세요.
- 작업이 끝나면 무엇을 바꿨는지 짧게 정리하세요. 확인하지 못한 부분은 확인하지 못했다고 말하세요.
- 답변은 간결하게 하세요."""


class Agent:
    def __init__(self, client: anthropic.Anthropic, ws: Workspace, model: str, effort: str):
        self.client = client
        self.ws = ws
        self.model = model
        self.effort = effort
        self.cost = CostMeter(model)
        self.messages: list = []
        self.interrupted = False
        # system/tools는 대화 내내 바꾸지 않는다 (프롬프트 캐시 유지)
        self.system = SYSTEM_PROMPT.format(root=ws.root, os=f"{platform.system()} {platform.release()}")

    def request_params(self) -> dict:
        params = dict(model=self.model, max_tokens=MAX_TOKENS, system=self.system, tools=TOOLS,
                      messages=self.messages, cache_control={"type": "ephemeral"})
        if self.model in EFFORT_MODELS:
            params["thinking"] = {"type": "adaptive"}
            params["output_config"] = {"effort": self.effort}
        if self.model in FALLBACK_MODELS:
            # 안전 분류기가 요청을 거절하면 서버가 다른 모델로 자동 재시도한다
            params["betas"] = ["server-side-fallback-2026-07-01"]
            params["fallbacks"] = "default"
        return params

    def stream_turn(self):
        """한 번의 API 요청을 스트리밍으로 보내고 최종 메시지를 돌려준다."""
        json_retries = 0
        while True:
            try:
                printed_text = False
                with self.client.beta.messages.stream(**self.request_params()) as stream:
                    for event in stream:
                        if event.type == "text":
                            if not printed_text:
                                print()
                                printed_text = True
                            print(event.text, end="", flush=True)
                        elif event.type == "content_block_start" and event.content_block.type == "tool_use":
                            print(f"\n{CYAN}  > {event.content_block.name}{RESET}", end="", flush=True)
                    message = stream.get_final_message()
                print()
                return message
            except ValueError:
                # 스트리밍된 도구 입력 JSON을 해석하지 못함 -> 같은 요청 재시도 (최대 2번)
                json_retries += 1
                if json_retries > 2:
                    raise
                say("\n(도구 입력을 해석하지 못해 다시 시도합니다)", DIM)

    def run(self, user_text: str) -> None:
        """사용자 메시지 하나를 처리한다 (도구 호출이 끝날 때까지 반복)."""
        if self.interrupted:
            user_text = "(직전 작업은 사용자가 중간에 중단했습니다.)\n" + user_text
            self.interrupted = False
        turn_start = len(self.messages)
        self.messages.append({"role": "user", "content": user_text})
        try:
            while True:
                say(f"{DIM}(작업 중...){RESET}")
                message = self.stream_turn()
                spent = self.cost.add(message.usage)
                say(f"{DIM}[이번 요청 약 ${spent:.4f} / 누적 약 ${self.cost.total():.4f}]{RESET}")

                if message.stop_reason == "refusal":
                    # 거절된 요청은 기록에서 빼서 다음 대화에 다시 보내지 않는다
                    del self.messages[turn_start:]
                    category = getattr(message.stop_details, "category", None) if message.stop_details else None
                    say(f"요청이 안전 정책에 의해 거절되었습니다 (분류: {category}). 다르게 요청해 주세요.", RED)
                    return
                if message.stop_reason == "pause_turn":
                    self.messages.append({"role": "assistant", "content": message.content})
                    continue

                tool_uses = [b for b in message.content if b.type == "tool_use"]
                if not tool_uses:
                    self.messages.append({"role": "assistant", "content": message.content})
                    if message.stop_reason == "max_tokens":
                        say("(응답 길이 한도에 도달해 잘렸습니다. '계속'이라고 입력하면 이어서 작성합니다.)", YELLOW)
                    return
                if message.stop_reason == "max_tokens":
                    del self.messages[turn_start:]
                    say("응답이 길이 한도에 걸려 도구 입력이 잘렸습니다. 작업을 더 작게 나눠 요청해 주세요.", RED)
                    return

                results = []
                for block in tool_uses:
                    problem = validate_input(block.name, block.input)
                    if problem:
                        results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                        "content": f"INVALID_INPUT: {problem}"})
                        continue
                    output, is_error = self.ws.execute(block.name, block.input)
                    mark = f"{RED}x{RESET}" if is_error else f"{GREEN}v{RESET}"
                    say(f"  {mark} {block.name}: {output.splitlines()[0][:100] if output else ''}")
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": output, "is_error": is_error})
                # 어시스턴트 응답과 도구 결과는 항상 한 쌍으로 기록한다
                self.messages.append({"role": "assistant", "content": message.content})
                self.messages.append({"role": "user", "content": results})
        except KeyboardInterrupt:
            # 마지막으로 완성된 지점까지만 기록에 남긴다. 기록이 사용자 쪽으로 끝나면
            # 다음 입력이 같은 차례로 합쳐진다 (API가 연속된 user 메시지를 허용).
            self.interrupted = True
            if len(self.messages) == turn_start + 1:
                del self.messages[turn_start:]
                self.interrupted = False
            say("\n중단했습니다.", YELLOW)
        except anthropic.AuthenticationError:
            del self.messages[turn_start:]
            say(f"API 키가 올바르지 않습니다. 저장된 키를 지우려면 {CONFIG_FILE} 파일을 삭제하세요.", RED)
        except anthropic.PermissionDeniedError as e:
            del self.messages[turn_start:]
            say(f"권한 오류: {e.message}", RED)
        except anthropic.RateLimitError:
            del self.messages[turn_start:]
            say("요청 한도를 넘었습니다. 잠시 후 다시 시도하세요.", RED)
        except anthropic.BadRequestError as e:
            del self.messages[turn_start:]
            say(f"요청 오류: {e.message}", RED)
        except anthropic.APIStatusError as e:
            del self.messages[turn_start:]
            say(f"API 오류 {e.status_code}: {e.message}", RED)
        except anthropic.APIConnectionError:
            del self.messages[turn_start:]
            say("인터넷 연결을 확인하세요.", RED)


# ---------------------------------------------------------------- 메인

HELP = """명령:
  /cost   지금까지 사용한 토큰과 예상 비용
  /clear  대화 기록 지우기 (새로 시작)
  /help   이 도움말
  /exit   종료 (Ctrl+D 도 가능)
작업 중 Ctrl+C 를 누르면 현재 작업을 중단합니다."""


def main() -> None:
    parser = argparse.ArgumentParser(description="로컬 파일 작업 에이전트 (Claude)")
    parser.add_argument("folder", nargs="?", default=".", help="작업 폴더 (기본: 현재 폴더)")
    parser.add_argument("--model", default=DEFAULT_MODEL, choices=sorted(MODELS), help="사용할 모델")
    parser.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"],
                        help="생각 깊이 (높을수록 정확하지만 비쌈). Haiku에서는 무시됨")
    args = parser.parse_args()

    root = Path(args.folder).expanduser()
    if not root.is_dir():
        sys.exit(f"폴더가 없습니다: {root}")

    key = load_api_key()
    client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
    agent = Agent(client, Workspace(root), args.model, args.effort)

    say(f"{BOLD}파일 작업 에이전트{RESET}  모델 {args.model} · 작업 폴더 {agent.ws.root}")
    say(f"{DIM}{HELP}{RESET}\n")

    while True:
        try:
            text = input(f"{BOLD}나>{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text in ("/exit", "/quit"):
            break
        if text == "/help":
            say(HELP)
            continue
        if text == "/cost":
            say(agent.cost.report())
            continue
        if text == "/clear":
            agent.messages.clear()
            agent.interrupted = False
            say("대화 기록을 지웠습니다.", DIM)
            continue
        agent.run(text)
        print()

    say(f"종료. {agent.cost.report()}", DIM)


if __name__ == "__main__":
    main()
