import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path

from openai import AsyncOpenAI

from . import db

log = logging.getLogger(__name__)

# 번역 엔진: claude (Claude Code CLI) | codex (Codex CLI) | openai (API)
TRANSLATOR = os.environ.get("TRANSLATOR", "claude").strip().lower()
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "sonnet")  # 빈 값이면 CLI 기본 모델
CODEX_MODEL = os.environ.get("CODEX_MODEL", "")  # 빈 값이면 CLI 기본 모델
MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-nano")
TARGET_LANG = os.environ.get("TARGET_LANG", "한국어")
# 추론 모델(gpt-5 계열) 속도 조절용. 추론 모델이 아니면 .env에서 빈 값으로 둔다.
REASONING_EFFORT = os.environ.get("OPENAI_REASONING_EFFORT", "low")

# CLI는 호출마다 프로세스를 띄우므로 배치를 크게, 동시 실행은 적게 잡는다.
BATCH_CHARS, CONCURRENCY = (2000, 8) if TRANSLATOR == "openai" else (4000, 3)
CLI_TIMEOUT = 300

SYSTEM_PROMPT = f"""You translate academic paper paragraphs into {TARGET_LANG}.
The user sends JSON {{"paragraphs": [{{"id": 0, "text": "..."}}, ...]}}. Reply with only JSON \
{{"translations": [{{"id": 0, "text": "..."}}, ...]}} holding one translation per paragraph id, and nothing else.
- Use a natural academic register. Keep the meaning exact; do not summarize or add commentary.
- The paragraphs were extracted from a PDF: line breaks are gone, and inline math is flattened \
(e.g. "dk" for d_k, "√dk", "W Q i"). Read each paragraph as continuous prose and restore the intended sentence.
- Write inline math as LaTeX wrapped in $...$ (e.g. $d_k$, $\\sqrt{{d_k}}$, $W_i^Q$). Do not use $ for anything else.
- Keep citations like [12] or (Kim et al., 2020), URLs, code and proper nouns unchanged.
- On first use of a technical term, give the translation followed by the original in parentheses.
- If a paragraph is only a formula, a table fragment, or otherwise untranslatable, return it unchanged."""

_client: AsyncOpenAI | None = None
_running: dict[int, asyncio.Task] = {}
# CLI가 이 프로젝트의 설정·문서를 읽지 않도록 빈 디렉터리에서 실행한다.
_cli_dir = Path(tempfile.mkdtemp(prefix="paper-workbench-cli-"))


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(timeout=180)
    return _client


async def _run_cli(args: list[str], stdin: str) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=_cli_dir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise RuntimeError(f"{args[0]} CLI를 찾을 수 없습니다. 설치·로그인 후 다시 시도하세요") from e
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin.encode()), CLI_TIMEOUT)
    except TimeoutError:
        proc.kill()
        raise RuntimeError(f"{args[0]} CLI가 {CLI_TIMEOUT}초 안에 끝나지 않았습니다") from None
    if proc.returncode != 0:
        detail = (err.decode(errors="replace").strip() or out.decode(errors="replace").strip())[-300:]
        raise RuntimeError(f"{args[0]} CLI 실패 (exit {proc.returncode}): {detail}")
    return out.decode(errors="replace")


async def _complete_claude(system: str, user: str) -> str:
    args = ["claude", "-p", "--output-format", "json", "--strict-mcp-config", "--no-session-persistence"]
    args += ["--tools", "", "--system-prompt", system]
    if CLAUDE_MODEL:
        args += ["--model", CLAUDE_MODEL]
    events = json.loads(await _run_cli(args, user))
    result = next(e for e in reversed(events if isinstance(events, list) else [events]) if e.get("type") == "result")
    if result.get("is_error"):
        raise RuntimeError(f"claude CLI 오류: {str(result.get('result'))[:300]}")
    return result.get("result") or ""


async def _complete_codex(system: str, user: str) -> str:
    with tempfile.NamedTemporaryFile(dir=_cli_dir, suffix=".txt") as last_message:
        args = ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only"]
        args += ["-o", last_message.name]
        if CODEX_MODEL:
            args += ["-m", CODEX_MODEL]
        await _run_cli([*args, "-"], f"{system}\n\n{user}")
        return Path(last_message.name).read_text()


async def _complete_openai(system: str, user: str) -> str:
    extra = {"reasoning_effort": REASONING_EFFORT} if REASONING_EFFORT else {}
    resp = await get_client().chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        **extra,
    )
    return resp.choices[0].message.content or ""


_ENGINES = {"claude": _complete_claude, "codex": _complete_codex, "openai": _complete_openai}


async def complete_json(system: str, user: str) -> dict:
    """설정된 엔진(TRANSLATOR)에 한 번 묻고, 답에 담긴 JSON 객체를 돌려준다."""
    if TRANSLATOR not in _ENGINES:
        raise RuntimeError(f"TRANSLATOR 값이 올바르지 않습니다: {TRANSLATOR!r} (claude | codex | openai)")
    reply = await _ENGINES[TRANSLATOR](system, user)
    # CLI는 JSON 앞뒤에 다른 글자를 붙일 수 있어 바깥 중괄호 구간만 읽는다.
    body = reply[reply.find("{") : reply.rfind("}") + 1] or "{}"
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        # 모델이 LaTeX의 역슬래시를 JSON 문자열 안에서 이스케이프하지 않은 경우 (\\phi 대신 \phi). 고쳐서 한 번 더 읽는다.
        return json.loads(re.sub(r"(?<!\\)\\(?![\\\"/])", r"\\\\", body))


# 이스케이프 안 된 \theta, \frac, \beta, \rho 는 JSON으로 읽히면서 탭·폼피드·백스페이스·캐리지리턴으로 바뀐다.
_EATEN_ESCAPES = {"\t": "\\t", "\f": "\\f", "\b": "\\b", "\r": "\\r"}


def _restore_latex(text: str) -> str:
    return re.sub(r"[\t\f\b\r](?=[a-z])", lambda m: _EATEN_ESCAPES[m.group()], text)


async def _translate_texts(texts: list[str]) -> dict[int, str]:
    """문단 목록을 번역해 {문단 인덱스: 번역문}으로 돌려준다. 모델이 빠뜨린 문단은 결과에 없다."""
    paragraphs = [{"id": i, "text": t} for i, t in enumerate(texts)]
    reply = await complete_json(SYSTEM_PROMPT, json.dumps({"paragraphs": paragraphs}, ensure_ascii=False))
    items = reply.get("translations") or []
    out = {}
    for item in items:
        if isinstance(item, dict) and item.get("id") in range(len(texts)) and item.get("text"):
            out[item["id"]] = _restore_latex(str(item["text"]))
    return out


def _batches(rows: list) -> list[list]:
    batches, cur, size = [], [], 0
    for row in rows:
        if cur and size + len(row["original"]) > BATCH_CHARS:
            batches.append(cur)
            cur, size = [], 0
        cur.append(row)
        size += len(row["original"])
    if cur:
        batches.append(cur)
    return batches


def _save(results: list[tuple[int, str | None, str | None]]) -> None:
    with db.connect() as con:
        for block_id, text, error in results:
            con.execute(
                "UPDATE blocks SET translated = ?, status = ?, error = ? WHERE id = ?",
                (text, "failed" if error else "done", error, block_id),
            )


async def _translate_batch(rows: list, sem: asyncio.Semaphore) -> None:
    error = "모델이 이 문단의 번역을 돌려주지 않았습니다"
    async with sem:
        try:
            done = await _translate_texts([r["original"] for r in rows])
        except Exception as e:
            log.warning("번역 요청 실패 (문단 %d개): %s", len(rows), e)
            done, error = {}, str(e)[:500]
        _save([(r["id"], done[i], None) for i, r in enumerate(rows) if i in done])
        missing = [r for i, r in enumerate(rows) if i not in done]
        if len(rows) == 1 and missing:
            _save([(rows[0]["id"], None, error)])
            return
    # 배치에서 빠진 문단은 하나씩 다시 시도한다.
    await asyncio.gather(*(_translate_batch([r], sem) for r in missing))


async def _run(paper_id: int) -> None:
    with db.connect() as con:
        rows = con.execute(
            "SELECT id, original FROM blocks WHERE paper_id = ? AND status IN ('pending', 'failed') ORDER BY id",
            (paper_id,),
        ).fetchall()
        con.execute(
            "UPDATE blocks SET status = 'pending', error = NULL WHERE paper_id = ? AND status = 'failed'",
            (paper_id,),
        )
    sem = asyncio.Semaphore(CONCURRENCY)
    await asyncio.gather(*(_translate_batch(b, sem) for b in _batches(rows)))


def start(paper_id: int) -> None:
    """논문의 미번역 문단을 백그라운드에서 번역한다. 이미 진행 중이면 아무것도 하지 않는다."""
    if paper_id in _running:
        return
    task = asyncio.create_task(_run(paper_id))
    _running[paper_id] = task
    task.add_done_callback(lambda _t: _running.pop(paper_id, None))


def is_running(paper_id: int) -> bool:
    return paper_id in _running


def resume_all() -> None:
    with db.connect() as con:
        ids = [r[0] for r in con.execute("SELECT DISTINCT paper_id FROM blocks WHERE status = 'pending'")]
    for paper_id in ids:
        start(paper_id)
