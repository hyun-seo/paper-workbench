import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

from . import db, extract

# 이 폴더에 PDF를 넣으면 서재에 자동으로 추가된다.
PAPERS_DIR = Path(os.environ.get("PAPERS_DIR") or db.ROOT_DIR / "inputs")
REMOVED_DIR = PAPERS_DIR / "_removed"  # 서재에서 삭제한 논문의 PDF가 옮겨지는 곳
MAX_PDF_BYTES = 50 * 1024 * 1024

errors: dict[str, str] = {}  # 파일명 → 가져오지 못한 이유
_handled: dict[str, float] = {}  # 이미 처리한 파일명 → 수정 시각


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _insert_blocks(con, paper_id: int, blocks: list[extract.Block]) -> None:
    con.executemany(
        "INSERT INTO blocks (paper_id, page, x0, y0, x1, y1, kind, original, status, parts)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (paper_id, b.page, *b.bbox, b.kind, b.text, "pending" if b.translatable else "skipped", json.dumps(b.parts))
            for b in blocks
        ],
    )


def _extract(data: bytes) -> tuple[str, int, list[extract.Block]]:
    if len(data) > MAX_PDF_BYTES:
        raise ValueError("PDF가 너무 큽니다 (최대 50MB)")
    if not data.startswith(b"%PDF"):
        raise ValueError("PDF 파일이 아닙니다")
    try:
        title, page_count, blocks = extract.extract(data)
    except Exception as e:
        raise ValueError(f"PDF를 읽지 못했습니다: {e}") from e
    if not any(b.kind == "text" for b in blocks):
        raise ValueError("텍스트를 추출하지 못했습니다 (스캔본 PDF는 아직 지원하지 않습니다)")
    return title, page_count, blocks


def import_pdf(data: bytes, source: str) -> tuple[int, bool]:
    """PDF를 서재에 넣고 (논문 id, 새로 추가됐는지)를 돌려준다. 같은 내용의 PDF가 있으면 그 논문을 돌려준다."""
    digest = _sha256(data)
    with db.connect() as con:
        existing = con.execute("SELECT id FROM papers WHERE sha256 = ?", (digest,)).fetchone()
    if existing:
        return existing["id"], False
    title, page_count, blocks = _extract(data)
    with db.connect() as con:
        paper_id = con.execute(
            "INSERT INTO papers (title, source, page_count, sha256, extract_version) VALUES (?, ?, ?, ?, ?)",
            (title, source, page_count, digest, extract.VERSION),
        ).lastrowid
        _insert_blocks(con, paper_id, blocks)
    db.pdf_path(paper_id).write_bytes(data)
    return paper_id, True


def reprocess(paper_id: int) -> None:
    """저장된 PDF에서 문단·수식·그림을 다시 뽑는다.

    원문이 그대로인 문단은 기존 번역을 이어 쓰고, 새로 생기거나 달라진 문단만 번역 대기로 둔다.
    노트·질문 기록·요약은 남는다.
    """
    title, page_count, blocks = _extract(db.pdf_path(paper_id).read_bytes())
    with db.connect() as con:
        done = con.execute(
            "SELECT original, translated FROM blocks WHERE paper_id = ? AND status = 'done'", (paper_id,)
        ).fetchall()
        con.execute("DELETE FROM blocks WHERE paper_id = ?", (paper_id,))
        con.execute(
            "UPDATE papers SET title = ?, page_count = ?, extract_version = ? WHERE id = ?",
            (title, page_count, extract.VERSION, paper_id),
        )
        _insert_blocks(con, paper_id, blocks)
        con.executemany(
            "UPDATE blocks SET translated = ?, status = 'done'"
            " WHERE paper_id = ? AND original = ? AND status = 'pending'",
            [(row["translated"], paper_id, row["original"]) for row in done],
        )


def upgrade_extractions() -> None:
    """예전 추출 규칙으로 뽑은 논문을 현재 규칙으로 다시 뽑는다."""
    with db.connect() as con:
        old = [r["id"] for r in con.execute("SELECT id FROM papers WHERE extract_version < ?", (extract.VERSION,))]
    for paper_id in old:
        try:
            reprocess(paper_id)
        except Exception as e:  # PDF 사본이 없는 등. 다음 실행 때 다시 시도한다
            errors[f"논문 {paper_id}"] = f"다시 추출하지 못했습니다: {e}"


def scan() -> list[int]:
    """논문 폴더를 훑어 새 PDF를 가져오고, 새로 추가된 논문 id들을 돌려준다."""
    PAPERS_DIR.mkdir(parents=True, exist_ok=True)
    added = []
    for path in sorted(PAPERS_DIR.glob("*.pdf")):
        mtime = path.stat().st_mtime
        if _handled.get(path.name) == mtime or time.time() - mtime < 2:  # 복사 중일 수 있는 파일은 다음에
            continue
        _handled[path.name] = mtime
        try:
            paper_id, created = import_pdf(path.read_bytes(), path.name)
            errors.pop(path.name, None)
            if created:
                added.append(paper_id)
        except Exception as e:
            errors[path.name] = str(e)
    for name in [n for n in errors if not (PAPERS_DIR / n).exists()]:
        del errors[name]
    return added


def save_download(data: bytes, url: str) -> Path:
    """링크로 받은 PDF를 논문 폴더에 파일로 남긴다."""
    PAPERS_DIR.mkdir(parents=True, exist_ok=True)
    arxiv = re.search(r"arxiv\.org/(?:abs|pdf)/([^?#]+?)(?:\.pdf)?$", url)
    stem = f"arxiv-{arxiv.group(1)}" if arxiv else Path(url.split("?")[0].rstrip("/")).stem
    stem = re.sub(r"[^\w.-]+", "_", stem).strip("._") or "paper"
    path = PAPERS_DIR / f"{stem}.pdf"
    if path.exists() and path.read_bytes() != data:
        path = PAPERS_DIR / f"{stem}-{_sha256(data)[:8]}.pdf"
    path.write_bytes(data)
    _handled[path.name] = path.stat().st_mtime  # 폴더 감시가 같은 파일을 다시 가져오지 않게 한다
    return path


def remove_files(paper_id: int) -> None:
    """삭제한 논문의 PDF를 폴더에서 _removed로 옮겨, 다음 훑기에서 다시 추가되지 않게 한다."""
    with db.connect() as con:
        row = con.execute("SELECT sha256 FROM papers WHERE id = ?", (paper_id,)).fetchone()
    if row and row["sha256"] and PAPERS_DIR.exists():
        for path in PAPERS_DIR.glob("*.pdf"):
            if _sha256(path.read_bytes()) == row["sha256"]:
                REMOVED_DIR.mkdir(exist_ok=True)
                shutil.move(path, REMOVED_DIR / path.name)
                _handled.pop(path.name, None)
    db.pdf_path(paper_id).unlink(missing_ok=True)


def backfill_hashes() -> None:
    """해시 없이 저장된 예전 논문에 해시를 채운다."""
    with db.connect() as con:
        for row in con.execute("SELECT id FROM papers WHERE sha256 IS NULL").fetchall():
            path = db.pdf_path(row["id"])
            if path.exists():
                con.execute("UPDATE papers SET sha256 = ? WHERE id = ?", (_sha256(path.read_bytes()), row["id"]))
