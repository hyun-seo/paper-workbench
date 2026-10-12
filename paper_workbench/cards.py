"""논문별 요약 카드와 주요 개념. 여러 논문을 잇는 개념 색인과 전체 정리 Markdown의 재료다."""

import asyncio
import json
import logging
import re

from . import db, translate

log = logging.getLogger(__name__)

MAX_PAPER_CHARS = 100_000
# proposes·uses·compares는 모델이 고른 것이고, mentions는 다른 논문의 개념이 본문에 여러 번 나와 자동으로 이은 것이다.
ROLES = {"proposes": "제안", "uses": "사용", "compares": "비교 대상", "mentions": "언급"}
MIN_MENTIONS = 3
SECTIONS = [("problem", "문제"), ("method", "방법"), ("results", "결과"), ("limitations", "한계")]

SYSTEM_PROMPT = """You read an academic paper and write a structured card about it in {lang}.
The user sends JSON {{"title": "...", "existing_concepts": ["..."], "text": "..."}}. Reply with only this JSON object:
{{"one_line": "...", "problem": "...", "method": "...", "results": "...", "limitations": "...",
"concepts": [{{"name": "...", "label": "...", "definition": "...", "role": "uses", "page": 3, "note": "..."}}]}}
- one_line: one sentence saying what the paper does and why it matters.
- problem, method, results, limitations: 2-3 sentences each, specific to this paper (names, numbers). \
If the paper states no limitations, say so.
- concepts: the 5-10 concepts a reader must know to follow this paper: methods, models, problem settings, metrics. \
Skip generic words such as "deep learning" or "experiment".
  - name: the standard English term, singular, e.g. "Self-attention". Prefer the general, widely used term \
over a paper-specific variant. If the same concept is already in existing_concepts, copy that name exactly \
so that papers share it.
  - label: the term in {lang} as used in the field (it may equal name).
  - definition: one or two sentences that hold in general, not only for this paper.
  - role: "proposes" if this paper introduces the concept, "compares" if it is a baseline or alternative the \
paper argues against, otherwise "uses".
  - page: the page number (from the [p. N] markers in the text) where the paper explains it best.
  - note: one sentence on what this paper does with the concept.
- Write everything except name in {lang}. Use only what the paper text says.""".format(lang=translate.TARGET_LANG)

_runner: asyncio.Task | None = None


def _paper_text(con, paper_id: int) -> str:
    """참고문헌 앞까지의 본문. 쪽 번호 표시를 넣어 모델이 개념이 나온 쪽을 짚을 수 있게 한다."""
    parts, page = [], 0
    rows = con.execute(
        "SELECT page, original FROM blocks WHERE paper_id = ? AND kind != 'figure'"
        " AND (status != 'skipped' OR kind = 'formula') ORDER BY id",
        (paper_id,),
    )
    for row in rows:
        if row["page"] != page:
            page = row["page"]
            parts.append(f"[p. {page}]")
        parts.append(row["original"])
    return "\n\n".join(parts)[:MAX_PAPER_CHARS]


async def generate(paper_id: int) -> None:
    """논문 한 편의 요약 카드와 개념을 만들어 저장한다. 실패하면 이유를 card_error에 남긴다."""
    try:
        with db.connect() as con:
            paper = con.execute("SELECT title, page_count FROM papers WHERE id = ?", (paper_id,)).fetchone()
            if paper is None:
                return
            existing = [r["name"] for r in con.execute("SELECT name FROM concepts ORDER BY name")]
            text = _paper_text(con, paper_id)
        request = {"title": paper["title"], "existing_concepts": existing, "text": text}
        reply = await translate.complete_json(SYSTEM_PROMPT, json.dumps(request, ensure_ascii=False))

        card = {key: str(reply.get(key) or "").strip() for key in ["one_line", *(k for k, _ in SECTIONS)]}
        if not card["one_line"]:
            raise ValueError("모델이 요약을 돌려주지 않았습니다")
        with db.connect() as con:
            con.execute(
                "UPDATE papers SET card = ?, card_error = NULL WHERE id = ?",
                (json.dumps(card, ensure_ascii=False), paper_id),
            )
            con.execute("DELETE FROM paper_concepts WHERE paper_id = ?", (paper_id,))
            for c in reply.get("concepts") or []:
                name = str(c.get("name") or "").strip() if isinstance(c, dict) else ""
                if not name:
                    continue
                con.execute(
                    "INSERT OR IGNORE INTO concepts (name, label, definition) VALUES (?, ?, ?)",
                    (name, str(c.get("label") or name).strip(), str(c.get("definition") or "").strip()),
                )
                concept_id = con.execute("SELECT id FROM concepts WHERE name = ?", (name,)).fetchone()["id"]
                page = c.get("page")
                con.execute(
                    "INSERT OR REPLACE INTO paper_concepts (paper_id, concept_id, role, page, note)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        paper_id,
                        concept_id,
                        c.get("role") if c.get("role") in ("proposes", "uses", "compares") else "uses",
                        page if isinstance(page, int) and 1 <= page <= paper["page_count"] else None,
                        str(c.get("note") or "").strip(),
                    ),
                )
    except Exception as e:
        log.warning("논문 %s 요약 카드 생성 실패: %s", paper_id, e)
        with db.connect() as con:
            con.execute("UPDATE papers SET card_error = ? WHERE id = ?", (str(e)[:500], paper_id))


def _name_pattern(name: str) -> re.Pattern:
    """개념 이름이 본문에 나오는 모양: 띄어쓰기·하이픈 차이와 복수형을 허용한다.

    CLIP, mAP처럼 낱말 중간에 대문자가 있는 약어는 대소문자를 가린다 (mAP가 일반 낱말 map과 섞이지 않게).
    """
    words = [re.escape(w) for w in re.split(r"[\s-]+", name) if w]
    flags = 0 if any(w[1:] != w[1:].lower() for w in name.split()) else re.IGNORECASE
    return re.compile(r"(?<![A-Za-z])" + r"[\s-]*".join(words) + r"(?:e?s)?(?![A-Za-z])", flags)


def link_mentions() -> None:
    """다른 논문에서 나온 개념이 본문에 여러 번 등장하면 그 논문에도 '언급'으로 잇는다.

    모델은 논문마다 개념을 몇 개만 고르고 먼저 처리된 논문은 나중 논문의 개념을 모르므로,
    글자 그대로 찾아 이어 주지 않으면 논문 사이의 연결이 거의 생기지 않는다.
    """
    with db.connect() as con:
        concepts = [
            (r["id"], [_name_pattern(name) for name in {r["name"], *json.loads(r["aliases"])} if len(name) > 1])
            for r in con.execute("SELECT id, name, aliases FROM concepts")
        ]
        chosen = {
            (r["paper_id"], r["concept_id"])
            for r in con.execute("SELECT paper_id, concept_id FROM paper_concepts WHERE role != 'mentions'")
        }
        links = []
        for paper in con.execute("SELECT id FROM papers WHERE card IS NOT NULL").fetchall():
            pages: dict[int, str] = {}
            for row in con.execute(
                "SELECT page, original FROM blocks WHERE paper_id = ? AND kind = 'text' AND status != 'skipped'",
                (paper["id"],),
            ):
                pages[row["page"]] = pages.get(row["page"], "") + " " + row["original"]
            for concept_id, patterns in concepts:
                if (paper["id"], concept_id) in chosen:
                    continue
                counts = {page: sum(len(p.findall(text)) for p in patterns) for page, text in pages.items()}
                total = sum(counts.values())
                if total >= MIN_MENTIONS:
                    best = max(counts, key=lambda page: counts[page])
                    links.append((paper["id"], concept_id, best, f"본문에 {total}회 등장"))
        con.execute("DELETE FROM paper_concepts WHERE role = 'mentions'")
        con.executemany(
            "INSERT INTO paper_concepts (paper_id, concept_id, role, page, note) VALUES (?, ?, 'mentions', ?, ?)", links
        )


async def _run_pending() -> None:
    # 한 편씩 차례로 만든다. 뒤 논문이 앞 논문에서 나온 개념 이름을 그대로 이어 쓰게 하기 위함이다.
    while True:
        with db.connect() as con:
            row = con.execute(
                "SELECT id FROM papers WHERE card IS NULL AND card_error IS NULL ORDER BY id LIMIT 1"
            ).fetchone()
        if row is None:
            return
        await generate(row["id"])
        await asyncio.to_thread(link_mentions)


def kick() -> None:
    """카드가 없는 논문이 있으면 백그라운드에서 만들기 시작한다. 이미 돌고 있으면 아무것도 하지 않는다."""
    global _runner
    if _runner is None or _runner.done():
        _runner = asyncio.create_task(_run_pending())


def regenerate(paper_id: int) -> None:
    with db.connect() as con:
        con.execute("UPDATE papers SET card = NULL, card_error = NULL WHERE id = ?", (paper_id,))
    kick()


def paper_card(con, paper_id: int) -> dict:
    """논문의 카드 상태와 내용, 그 논문에 달린 개념들."""
    row = con.execute("SELECT card, card_error FROM papers WHERE id = ?", (paper_id,)).fetchone()
    concepts = con.execute(
        "SELECT c.id, c.name, c.label, c.definition, pc.role, pc.page, pc.note,"
        " (SELECT COUNT(*) FROM paper_concepts x WHERE x.concept_id = c.id) AS papers"
        " FROM paper_concepts pc JOIN concepts c ON c.id = pc.concept_id"
        " WHERE pc.paper_id = ? ORDER BY pc.role = 'mentions', papers DESC, c.name",
        (paper_id,),
    ).fetchall()
    return {
        "status": "done" if row["card"] else "failed" if row["card_error"] else "pending",
        "error": row["card_error"],
        "card": json.loads(row["card"]) if row["card"] else None,
        "concepts": [dict(c) for c in concepts],
    }


def concept_index() -> list[dict]:
    """개념별로 그 개념을 다룬 논문들. 여러 논문에 걸친 개념이 앞에 온다."""
    with db.connect() as con:
        links = con.execute(
            "SELECT c.id, c.name, c.label, c.definition, p.id AS paper_id, p.title, pc.role, pc.page, pc.note"
            " FROM paper_concepts pc JOIN concepts c ON c.id = pc.concept_id JOIN papers p ON p.id = pc.paper_id"
            " ORDER BY c.name, pc.role = 'mentions', p.id"
        ).fetchall()
    concepts: dict[int, dict] = {}
    for r in links:
        concept = concepts.setdefault(
            r["id"], {"id": r["id"], "name": r["name"], "label": r["label"], "definition": r["definition"], "papers": []}
        )
        concept["papers"].append(
            {"id": r["paper_id"], "title": r["title"], "role": r["role"], "page": r["page"], "note": r["note"]}
        )
    return sorted(concepts.values(), key=lambda c: (-len(c["papers"]), c["name"].lower()))


def _concept_title(c: dict) -> str:
    return c["name"] if c["label"].lower() == c["name"].lower() else f"{c['name']} ({c['label']})"


def library_markdown(synthesis_lines: list[str]) -> str:
    """서재 전체를 Markdown 한 파일로 정리한다: 종합, 논문별 요약·개념·노트, 개념 색인."""
    lines = ["# 논문 정리", "", *synthesis_lines]
    with db.connect() as con:
        papers = con.execute("SELECT id, title, source, note FROM papers ORDER BY id").fetchall()
        cards = {p["id"]: paper_card(con, p["id"]) for p in papers}
    lines += ["## 논문", ""]
    for p in papers:
        info = cards[p["id"]]
        lines += [f"### {p['title']}", "", f"- 출처: {p['source']}"]
        if info["card"]:
            card = info["card"]
            lines += [f"- 한 줄 요약: {card['one_line']}", ""]
            lines += [f"**{label}** {card[key]}\n" for key, label in SECTIONS if card.get(key)]
            if info["concepts"]:
                lines += ["**주요 개념**", ""]
                for c in info["concepts"]:
                    where = f", p. {c['page']}" if c["page"] else ""
                    lines.append(f"- {_concept_title(c)} [{ROLES[c['role']]}{where}]: {c['note']}")
                lines.append("")
        else:
            lines += ["", "(요약이 아직 없습니다)", ""]
        if p["note"].strip():
            lines += ["**내 노트**", "", p["note"].strip(), ""]
    lines += ["## 개념 색인", ""]
    for c in concept_index():
        lines += [f"### {_concept_title(c)}", "", c["definition"], ""]
        for p in c["papers"]:
            where = f", p. {p['page']}" if p["page"] else ""
            lines.append(f"- {p['title']} [{ROLES[p['role']]}{where}]: {p['note']}")
        lines.append("")
    return "\n".join(lines)
