import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote as quote_url

import httpx
import pymupdf
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

from . import ask, cards, db, library, synthesis, translate  # noqa: E402  (.env를 먼저 읽어야 한다)

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
SCAN_SECONDS = 3


async def _watch_folder():
    """논문 폴더를 주기적으로 훑어 새로 들어온 PDF를 가져오고 번역을 시작한다."""
    while True:
        try:
            for paper_id in await asyncio.to_thread(library.scan):
                translate.start(paper_id)
            cards.kick()
            synthesis.kick()
        except Exception:
            log.exception("논문 폴더를 훑는 중 오류")
        await asyncio.sleep(SCAN_SECONDS)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    library.backfill_hashes()
    library.upgrade_extractions()  # 번역 재개보다 먼저: 다시 뽑힌 문단이 번역 대기열에 들어가게 한다
    cards.link_mentions()
    translate.resume_all()
    watcher = asyncio.create_task(_watch_folder())
    yield
    watcher.cancel()


app = FastAPI(title="Paper Workbench", lifespan=lifespan)


def _pdf_url(url: str) -> str:
    """arXiv 초록 페이지 같은 링크를 PDF 직링크로 바꾼다."""
    m = re.match(r"https?://(?:www\.)?arxiv\.org/abs/([^?#]+)", url)
    return f"https://arxiv.org/pdf/{m.group(1)}" if m else url


async def _download(url: str) -> bytes:
    if not re.match(r"https?://", url):
        raise HTTPException(400, "http(s) 링크만 지원합니다")
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
            resp = await client.get(_pdf_url(url), headers={"User-Agent": "paper-workbench/0.1"})
            resp.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"링크에서 PDF를 받지 못했습니다: {e}") from e
    return resp.content


def _summary(con, paper_id: int) -> dict:
    paper = con.execute(
        "SELECT id, title, source, page_count, created_at, card, card_error FROM papers WHERE id = ?", (paper_id,)
    ).fetchone()
    if paper is None:
        raise HTTPException(404, "논문을 찾을 수 없습니다")
    counts = dict(
        con.execute("SELECT status, COUNT(*) FROM blocks WHERE paper_id = ? GROUP BY status", (paper_id,)).fetchall()
    )
    paper = dict(paper)
    card, card_error = paper.pop("card"), paper.pop("card_error")
    return {
        **paper,
        "one_line": json.loads(card)["one_line"] if card else None,
        "card_status": "done" if card else "failed" if card_error else "pending",
        "progress": {
            "done": counts.get("done", 0),
            "failed": counts.get("failed", 0),
            "pending": counts.get("pending", 0),
            "running": translate.is_running(paper_id),
        },
    }


class UrlBody(BaseModel):
    url: str


@app.post("/api/papers")
async def add_from_url(body: UrlBody):
    """링크에서 PDF를 받아 논문 폴더에 저장하고 서재에 추가한다."""
    url = body.url.strip()
    data = await _download(url)
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "PDF 파일이 아닙니다. PDF 직링크나 arXiv 링크를 넣어 주세요")
    try:
        paper_id, created = await asyncio.to_thread(library.import_pdf, data, url)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if created:
        library.save_download(data, url)
        translate.start(paper_id)
    return {"id": paper_id}


@app.get("/api/inbox")
def inbox():
    """PDF를 넣어 두는 폴더와, 가져오지 못한 파일들."""
    return {"dir": str(library.PAPERS_DIR), "errors": library.errors}


@app.get("/api/papers")
def list_papers():
    with db.connect() as con:
        ids = [r[0] for r in con.execute("SELECT id FROM papers ORDER BY id DESC")]
        return [_summary(con, i) for i in ids]


@app.get("/api/papers/{paper_id}")
def get_paper(paper_id: int):
    with db.connect() as con:
        paper = _summary(con, paper_id)
        paper["note"] = con.execute("SELECT note FROM papers WHERE id = ?", (paper_id,)).fetchone()[0]
        paper["summary"] = cards.paper_card(con, paper_id)
        paper["blocks"] = []
        for r in con.execute(
            "SELECT id, page, x0, y0, x1, y1, kind, original, translated, status, error, parts"
            " FROM blocks WHERE paper_id = ? ORDER BY id",
            (paper_id,),
        ):
            block = dict(r)
            # 블록이 차지하는 영역들 [[쪽, x0, y0, x1, y1], ...]. 단·쪽을 넘는 문단은 여러 개다.
            block["parts"] = json.loads(r["parts"]) if r["parts"] else [[r["page"], r["x0"], r["y0"], r["x1"], r["y1"]]]
            paper["blocks"].append(block)
        return paper


@app.get("/api/papers/{paper_id}/card")
def get_card(paper_id: int):
    with db.connect() as con:
        _summary(con, paper_id)
        return cards.paper_card(con, paper_id)


@app.post("/api/papers/{paper_id}/card")
async def regenerate_card(paper_id: int):
    """요약 카드와 개념을 다시 만든다."""
    with db.connect() as con:
        _summary(con, paper_id)
    cards.regenerate(paper_id)
    return {"ok": True}


@app.get("/api/concepts")
def list_concepts():
    return cards.concept_index()


@app.get("/api/synthesis")
def get_synthesis():
    """서재 전체 종합: 주제, 논문 사이의 관계, 비교표, 아이디어."""
    return synthesis.state()


@app.post("/api/synthesis")
async def regenerate_synthesis():
    synthesis.regenerate()
    return {"ok": True}


class LibraryQuestion(BaseModel):
    question: str


@app.get("/api/library/messages")
def library_messages():
    return synthesis.messages()


@app.post("/api/library/ask")
async def ask_library(body: LibraryQuestion):
    """서재 전체(종합과 논문 요약 카드들)에 질문한다."""
    if not body.question.strip():
        raise HTTPException(400, "질문을 입력해 주세요")
    try:
        return await synthesis.ask(body.question.strip())
    except Exception as e:
        raise HTTPException(502, f"답변을 받지 못했습니다: {e}") from e


@app.delete("/api/library/messages")
def clear_library_messages():
    with db.connect() as con:
        con.execute("DELETE FROM library_messages")
    return {"ok": True}


@app.get("/api/library.md")
def library_markdown():
    """서재 전체 정리(논문별 요약·개념·노트와 개념 색인)를 Markdown 파일로 내려준다."""
    return Response(
        cards.library_markdown(synthesis.markdown_lines()),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="papers.md"'},
    )


@app.get("/api/papers/{paper_id}/pdf")
def get_pdf(paper_id: int):
    path = db.pdf_path(paper_id)
    if not path.exists():
        raise HTTPException(404, "PDF 파일이 없습니다")
    return FileResponse(path, media_type="application/pdf")


@app.get("/api/papers/{paper_id}/blocks/{block_id}/image")
def block_image(paper_id: int, block_id: int):
    """수식·그림 블록이 차지하는 원본 PDF 영역을 잘라 PNG로 내려준다."""
    with db.connect() as con:
        b = con.execute(
            "SELECT page, x0, y0, x1, y1 FROM blocks WHERE id = ? AND paper_id = ?", (block_id, paper_id)
        ).fetchone()
    path = db.pdf_path(paper_id)
    if b is None or not path.exists():
        raise HTTPException(404, "블록을 찾을 수 없습니다")
    with pymupdf.open(path) as doc:
        page = doc[b["page"] - 1]
        w, h = page.rect.width, page.rect.height
        clip = pymupdf.Rect(b["x0"] * w, b["y0"] * h, b["x1"] * w, b["y1"] * h) + (-3, -3, 3, 3)
        png = page.get_pixmap(clip=clip, matrix=pymupdf.Matrix(2.5, 2.5)).tobytes("png")
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


class NoteBody(BaseModel):
    note: str


@app.put("/api/papers/{paper_id}/note")
def save_note(paper_id: int, body: NoteBody):
    with db.connect() as con:
        cur = con.execute("UPDATE papers SET note = ? WHERE id = ?", (body.note, paper_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "논문을 찾을 수 없습니다")
    return {"ok": True}


@app.post("/api/papers/{paper_id}/translate")
async def retry_translation(paper_id: int):
    """실패했거나 남아 있는 문단을 다시 번역한다."""
    with db.connect() as con:
        _summary(con, paper_id)
    translate.start(paper_id)
    return {"ok": True}


@app.post("/api/papers/{paper_id}/reprocess")
async def reprocess(paper_id: int):
    """PDF에서 문단을 다시 뽑아 처음부터 다시 번역한다. 노트와 질문 기록은 그대로 둔다."""
    with db.connect() as con:
        _summary(con, paper_id)
    if translate.is_running(paper_id):
        raise HTTPException(409, "번역이 진행 중입니다. 끝난 뒤 다시 시도하세요")
    try:
        await asyncio.to_thread(library.reprocess, paper_id)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e)) from e
    translate.start(paper_id)
    return {"ok": True}


class Quote(BaseModel):
    text: str
    block_id: int | None = None  # 구절이 속한 번역 문단


class AskBody(BaseModel):
    question: str
    quotes: list[Quote] = []
    images: list[str] = []  # PDF에서 잘라낸 영역들 (data URL)


@app.get("/api/papers/{paper_id}/messages")
def list_messages(paper_id: int):
    return ask.history(paper_id)


@app.post("/api/papers/{paper_id}/ask")
async def ask_question(paper_id: int, body: AskBody):
    with db.connect() as con:
        _summary(con, paper_id)
    if not body.question.strip():
        raise HTTPException(400, "질문을 입력해 주세요")
    if not all(url.startswith("data:image/") for url in body.images):
        raise HTTPException(400, "이미지 형식이 올바르지 않습니다")
    block_ids = sorted({q.block_id for q in body.quotes if q.block_id is not None})
    try:
        return await ask.ask(paper_id, body.question.strip(), [q.text for q in body.quotes], block_ids, body.images)
    except Exception as e:
        raise HTTPException(502, f"답변을 받지 못했습니다: {e}") from e


@app.delete("/api/papers/{paper_id}/messages")
def clear_messages(paper_id: int):
    with db.connect() as con:
        con.execute("DELETE FROM messages WHERE paper_id = ?", (paper_id,))
    return {"ok": True}


@app.get("/api/papers/{paper_id}/export.md")
def export_markdown(paper_id: int):
    """노트와 질문·답변 기록을 Markdown 파일로 내려준다."""
    with db.connect() as con:
        paper = _summary(con, paper_id)
        note = con.execute("SELECT note FROM papers WHERE id = ?", (paper_id,)).fetchone()[0]
    lines = [f"# {paper['title']}", "", f"- 출처: {paper['source']}", "", "## 노트", "", note.strip() or "(없음)"]
    messages = ask.history(paper_id)
    if messages:
        lines += ["", "## 질문과 답변"]
    for m in messages:
        if m["role"] == "user":
            lines += ["", f"### Q. {m['content']}"]
            for quote in m["quotes"]:
                lines += ["", *(f"> {line}" for line in quote.splitlines())]
            if m["images"]:
                lines += ["", f"(PDF 영역 이미지 {len(m['images'])}장 첨부)"]
        else:
            lines += ["", m["content"]]
    filename = quote_url(f"{paper['title']}.md")
    return Response(
        "\n".join(lines) + "\n",
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=\"paper-{paper_id}.md\"; filename*=UTF-8''{filename}"},
    )


@app.delete("/api/papers/{paper_id}")
def delete_paper(paper_id: int):
    library.remove_files(paper_id)
    with db.connect() as con:
        con.execute("DELETE FROM papers WHERE id = ?", (paper_id,))
    return {"ok": True}


class _RevalidatedStatic(StaticFiles):
    """코드를 고친 뒤에도 브라우저가 예전 app.js·style.css를 쓰지 않도록, 매번 서버에 변경 여부를 묻게 한다."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/", _RevalidatedStatic(directory=STATIC_DIR, html=True), name="static")
