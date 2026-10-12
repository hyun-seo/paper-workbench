"""서재 전체를 꿰는 종합: 주제 묶음, 논문 사이의 관계, 비교표, 개념 정리, 아이디어.

논문별 요약 카드들을 재료로 모델에 한 번 물어 만든다. 카드가 바뀌면(논문 추가·삭제) 다시 만든다.
"""

import asyncio
import hashlib
import json
import logging
import re

from . import cards, db, translate

log = logging.getLogger(__name__)

RELATIONS = {
    "builds_on": "발전·확장",
    "same_problem": "같은 문제, 다른 접근",
    "shared_method": "같은 방법·도구",
    "complementary": "상호 보완",
    "contrasts": "대립·상충",
}
CONCEPT_LINKS = {"is_a": "의 한 종류", "part_of": "의 구성 요소", "used_for": "에 쓰임", "alternative_to": "의 대안"}
MAX_THEMES = 6

SYSTEM_PROMPT = """You are given structured cards for every paper in a reader's personal library. Write a synthesis \
that helps the reader see how the papers' ideas connect. Reply with only this JSON object, all prose in {lang}:
{{"overview": "...", "themes": [], "relations": [], "comparison": {{"dimensions": [], "rows": []}}, "concepts": [], \
"concept_links": [], "ideas": [], "reading_order": []}}
Refer to a paper in prose as [P<id>], e.g. [P3].
- overview: 4-6 sentences. What the library is about as a whole, its main threads, and where the papers agree or \
pull apart.
- themes: 2-6 threads that group the papers by the idea they pursue (not by venue or year). Each \
{{"name": "short noun phrase", "description": "1-2 sentences", "papers": [{{"id": 3, "role": "one sentence on what \
this paper contributes to the thread"}}]}}. Every paper appears in at least one theme and may appear in several; \
list the theme that fits a paper best before its other themes.
- relations: pairs of papers with a concrete connection. Each {{"a": 3, "b": 4, "type": "...", "note": "one or two \
sentences stating the specific connection"}}. type is one of: builds_on (a extends or relies on b), same_problem \
(same problem, different approach), shared_method (same technique or tool), complementary (they solve different \
parts and could be combined), contrasts (their claims or assumptions conflict). Do not invent connections; \
unrelated pairs are simply left out.
- comparison: a table for reading the papers side by side. "dimensions": 4-6 column names chosen to fit this \
library (for example: problem, key idea, what is learned or optimized, data and evaluation, main result, \
limitation). "rows": one {{"id": 3, "cells": ["..."]}} per paper with one cell per dimension, each at most 25 \
words and concrete.
- concepts: clean up the concept list. One entry for each concept that appears under more than one name or that \
papers write as an abbreviation: {{"canonical": "preferred standard English name (a well-known abbreviation such as \
CLIP is fine if that is how the field writes it)", "label": "name in {lang}", "merge": ["names from the input \
concept list that mean this same concept"], "aliases": ["abbreviations and other spellings used in papers"]}}. \
Merge only true synonyms, never a concept with its broader or narrower relative.
- concept_links: up to 15 clear relations between concepts, using canonical names: {{"from": "...", "to": "...", \
"type": "is_a | part_of | used_for | alternative_to", "note": "short"}}, read as "from is_a to".
- ideas: 3-6 entries {{"title": "...", "description": "2-3 sentences", "papers": [3, 4]}}: open questions, tensions \
between papers, or ways ideas from different papers could be combined. Say so when an entry is speculation.
- reading_order: every paper id once, in a sensible order to read, each {{"id": 3, "why": "one sentence"}}.
Use only what the cards say.""".format(lang=translate.TARGET_LANG)

ASK_PROMPT = """You answer a reader's question about their personal library of academic papers, in {lang}. \
You are given a synthesis of the library and a structured card for each paper (not the full texts).
Reply with only JSON {{"answer": "..."}}.
- Answer from the cards and the synthesis. If they do not contain the answer, say what is missing and which \
paper the reader should open.
- Cite papers as [P<id>] right where you use them, e.g. [P3]. Compare papers explicitly when the question spans several.
- Plain text without Markdown markup. Write math as LaTeX in $...$. Be concise.""".format(lang=translate.TARGET_LANG)

_task: asyncio.Task | None = None


def _library(con) -> list[dict]:
    """요약 카드가 있는 논문들과 그 개념."""
    papers = []
    for p in con.execute("SELECT id, title, card FROM papers WHERE card IS NOT NULL ORDER BY id").fetchall():
        concepts = con.execute(
            "SELECT c.name, c.definition, pc.role, pc.note FROM paper_concepts pc JOIN concepts c"
            " ON c.id = pc.concept_id WHERE pc.paper_id = ? AND pc.role != 'mentions' ORDER BY c.name",
            (p["id"],),
        ).fetchall()
        papers.append({"id": p["id"], "title": p["title"], **json.loads(p["card"]), "concepts": [dict(c) for c in concepts]})
    return papers


def _fingerprint(papers: list[dict]) -> str:
    basis = [(p["id"], p["title"], p["one_line"], p.get("method")) for p in papers]
    return hashlib.sha1(json.dumps(basis, ensure_ascii=False).encode()).hexdigest()


def _clean(reply: dict, papers: list[dict]) -> dict:
    """모델 답에서 없는 논문 id나 모르는 종류를 걸러 화면이 믿고 쓸 수 있는 모양으로 만든다."""
    ids = {p["id"] for p in papers}
    text = lambda v: str(v or "").strip()  # noqa: E731
    themes = []
    for t in (reply.get("themes") or [])[:MAX_THEMES]:
        members = [
            {"id": m["id"], "role": text(m.get("role"))}
            for m in t.get("papers") or []
            if isinstance(m, dict) and m.get("id") in ids
        ]
        if text(t.get("name")) and members:
            themes.append({"name": text(t["name"]), "description": text(t.get("description")), "papers": members})
    relations = [
        {"a": r["a"], "b": r["b"], "type": r["type"], "note": text(r.get("note"))}
        for r in reply.get("relations") or []
        if isinstance(r, dict) and r.get("a") in ids and r.get("b") in ids and r["a"] != r["b"] and r.get("type") in RELATIONS
    ]
    comparison = reply.get("comparison") or {}
    dimensions = [text(d) for d in comparison.get("dimensions") or []]
    rows = [
        {"id": r["id"], "cells": [text(c) for c in r.get("cells") or []][: len(dimensions)]}
        for r in comparison.get("rows") or []
        if isinstance(r, dict) and r.get("id") in ids
    ]
    ideas = [
        {
            "title": text(i.get("title")),
            "description": text(i.get("description")),
            "papers": [x for x in i.get("papers") or [] if x in ids],
        }
        for i in reply.get("ideas") or []
        if isinstance(i, dict) and text(i.get("title"))
    ]
    order = [
        {"id": o["id"], "why": text(o.get("why"))}
        for o in reply.get("reading_order") or []
        if isinstance(o, dict) and o.get("id") in ids
    ]
    return {
        "overview": text(reply.get("overview")),
        "themes": themes,
        "relations": relations,
        "comparison": {"dimensions": dimensions, "rows": rows},
        "ideas": ideas,
        "reading_order": order,
    }


def _apply_concepts(con, reply: dict) -> None:
    """같은 개념의 다른 이름들을 하나로 합치고, 약어·다른 표기와 개념 사이의 관계를 저장한다."""
    by_name = {r["name"].lower(): r["id"] for r in con.execute("SELECT id, name FROM concepts")}
    for item in reply.get("concepts") or []:
        if not isinstance(item, dict) or not str(item.get("canonical") or "").strip():
            continue
        canonical = str(item["canonical"]).strip()
        merged = [str(n).strip() for n in item.get("merge") or [] if str(n).strip().lower() in by_name]
        if not merged and canonical.lower() not in by_name:
            continue  # 서재에 없는 개념은 새로 만들지 않는다
        keep = by_name.get(canonical.lower()) or by_name[merged[0].lower()]
        old_names = {n for n in merged if by_name[n.lower()] != keep}
        for name in old_names:
            old = by_name.pop(name.lower())
            # 같은 논문이 두 이름 모두에 걸려 있으면 모델이 고른 연결(언급이 아닌 것)을 남긴다.
            con.execute(
                "DELETE FROM paper_concepts WHERE concept_id = ? AND role = 'mentions' AND paper_id IN"
                " (SELECT paper_id FROM paper_concepts WHERE concept_id = ?)",
                (keep, old),
            )
            con.execute("UPDATE OR IGNORE paper_concepts SET concept_id = ? WHERE concept_id = ?", (keep, old))
            con.execute("DELETE FROM concepts WHERE id = ?", (old,))
        row = con.execute("SELECT name, aliases FROM concepts WHERE id = ?", (keep,)).fetchone()
        aliases = {*json.loads(row["aliases"]), *old_names, row["name"], *(str(a).strip() for a in item.get("aliases") or [])}
        aliases = sorted(a for a in aliases if a and a.lower() != canonical.lower())
        by_name.pop(row["name"].lower(), None)
        by_name[canonical.lower()] = keep
        con.execute(
            "UPDATE concepts SET name = ?, label = ?, aliases = ? WHERE id = ?",
            (canonical, str(item.get("label") or canonical).strip(), json.dumps(aliases, ensure_ascii=False), keep),
        )
    con.execute("DELETE FROM concept_links")
    for link in reply.get("concept_links") or []:
        if not isinstance(link, dict) or link.get("type") not in CONCEPT_LINKS:
            continue
        a, b = by_name.get(str(link.get("from") or "").lower()), by_name.get(str(link.get("to") or "").lower())
        if a and b and a != b:
            con.execute(
                "INSERT OR IGNORE INTO concept_links (from_id, to_id, type, note) VALUES (?, ?, ?, ?)",
                (a, b, link["type"], str(link.get("note") or "").strip()),
            )


async def _generate(papers: list[dict], fingerprint: str) -> None:
    try:
        reply = await translate.complete_json(SYSTEM_PROMPT, json.dumps({"papers": papers}, ensure_ascii=False))
        data = _clean(reply, papers)
        if not data["overview"] or not data["themes"]:
            raise ValueError("모델이 종합을 돌려주지 않았습니다")
        with db.connect() as con:
            _apply_concepts(con, reply)
            con.execute(
                "INSERT OR REPLACE INTO synthesis (id, data, fingerprint, error) VALUES (1, ?, ?, NULL)",
                (json.dumps(data, ensure_ascii=False), fingerprint),
            )
        await asyncio.to_thread(cards.link_mentions)  # 새로 알게 된 약어로 본문 언급을 다시 찾는다
    except Exception as e:
        log.warning("서재 종합 생성 실패: %s", e)
        with db.connect() as con:
            # 이전 결과는 남기고, 같은 카드 묶음으로 계속 다시 시도하지 않도록 지문을 기록한다.
            old = con.execute("SELECT data FROM synthesis WHERE id = 1").fetchone()
            con.execute(
                "INSERT OR REPLACE INTO synthesis (id, data, fingerprint, error) VALUES (1, ?, ?, ?)",
                (old["data"] if old else None, fingerprint, str(e)[:500]),
            )


def kick() -> None:
    """요약 카드가 모두 준비됐고 지난 종합 이후 논문 구성이 바뀌었으면, 백그라운드에서 종합을 다시 만든다."""
    global _task
    if _task is not None and not _task.done():
        return
    with db.connect() as con:
        waiting = con.execute("SELECT COUNT(*) FROM papers WHERE card IS NULL AND card_error IS NULL").fetchone()[0]
        papers = _library(con)
        row = con.execute("SELECT fingerprint FROM synthesis WHERE id = 1").fetchone()
    if waiting or len(papers) < 2:
        return
    fingerprint = _fingerprint(papers)
    if row is None or row["fingerprint"] != fingerprint:
        _task = asyncio.create_task(_generate(papers, fingerprint))


def regenerate() -> None:
    with db.connect() as con:
        con.execute("UPDATE synthesis SET fingerprint = NULL, error = NULL WHERE id = 1")
    kick()


def state() -> dict:
    """화면에 줄 종합 상태: empty(논문이 2편 미만) | pending | done | failed."""
    with db.connect() as con:
        row = con.execute("SELECT data, error FROM synthesis WHERE id = 1").fetchone()
        papers = [dict(p) for p in con.execute("SELECT id, title FROM papers ORDER BY id")]
        carded = con.execute("SELECT COUNT(*) FROM papers WHERE card IS NOT NULL").fetchone()[0]
        links = [
            dict(r)
            for r in con.execute(
                "SELECT a.name AS source, b.name AS target, l.type, l.note FROM concept_links l"
                " JOIN concepts a ON a.id = l.from_id JOIN concepts b ON b.id = l.to_id"
            )
        ]
    running = _task is not None and not _task.done()
    data = json.loads(row["data"]) if row and row["data"] else None
    if running:
        status = "pending"
    elif row and row["error"]:
        status = "failed"
    elif data:
        status = "done"
    else:
        status = "empty" if carded < 2 else "pending"
    return {
        "status": status,
        "error": row["error"] if row else None,
        "data": data,
        "papers": papers,
        "concept_links": links,
        "relation_types": RELATIONS,
        "concept_link_types": CONCEPT_LINKS,
    }


def markdown_lines() -> list[str]:
    """전체 정리 Markdown의 앞부분에 들어갈 종합."""
    s = state()
    data = s["data"]
    if not data:
        return []
    titles = {p["id"]: p["title"] for p in s["papers"]}
    cite = lambda text: re.sub(r"\[P(\d+)\]", lambda m: f"〈{titles.get(int(m.group(1)), m.group(0))}〉", text)  # noqa: E731
    lines = ["## 종합", "", cite(data["overview"]), "", "### 주제", ""]
    for t in data["themes"]:
        lines += [f"**{t['name']}** {cite(t['description'])}", ""]
        lines += [f"- {titles[m['id']]}: {cite(m['role'])}" for m in t["papers"] if m["id"] in titles]
        lines.append("")
    comparison = data["comparison"]
    if comparison["dimensions"] and comparison["rows"]:
        cell = lambda v: cite(v).replace("|", "\\|").replace("\n", " ")  # noqa: E731
        lines += ["### 비교", "", "| 논문 | " + " | ".join(comparison["dimensions"]) + " |"]
        lines.append("| --- |" + " --- |" * len(comparison["dimensions"]))
        for r in comparison["rows"]:
            if r["id"] in titles:
                lines.append(f"| {cell(titles[r['id']])} | " + " | ".join(cell(c) for c in r["cells"]) + " |")
        lines.append("")
    if data["relations"]:
        lines += ["### 논문 사이의 관계", ""]
        for r in data["relations"]:
            if r["a"] in titles and r["b"] in titles:
                lines.append(f"- {titles[r['a']]} ↔ {titles[r['b']]} [{RELATIONS[r['type']]}]: {cite(r['note'])}")
        lines.append("")
    if data["ideas"]:
        lines += ["### 열린 질문과 아이디어", ""]
        lines += [f"- **{i['title']}** {cite(i['description'])}" for i in data["ideas"]]
        lines.append("")
    if data["reading_order"]:
        lines += ["### 읽는 순서", ""]
        lines += [f"{n}. {titles[o['id']]}: {cite(o['why'])}" for n, o in enumerate(data["reading_order"], 1) if o["id"] in titles]
        lines.append("")
    return lines


def messages() -> list[dict]:
    with db.connect() as con:
        return [dict(r) for r in con.execute("SELECT id, role, content FROM library_messages ORDER BY id")]


async def ask(question: str) -> dict:
    """서재 전체(종합과 논문 카드들)를 바탕으로 질문에 답하고 기록에 남긴다."""
    with db.connect() as con:
        papers = _library(con)
        row = con.execute("SELECT data FROM synthesis WHERE id = 1").fetchone()
        past = con.execute("SELECT role, content FROM library_messages ORDER BY id DESC LIMIT 8").fetchall()[::-1]
    request = {
        "synthesis": json.loads(row["data"]) if row and row["data"] else None,
        "papers": papers,
        "conversation_so_far": [dict(m) for m in past],
        "question": question,
    }
    reply = await translate.complete_json(ASK_PROMPT, json.dumps(request, ensure_ascii=False))
    answer = str(reply.get("answer") or "").strip()
    if not answer:
        raise ValueError("모델이 답을 돌려주지 않았습니다")
    with db.connect() as con:
        con.execute("INSERT INTO library_messages (role, content) VALUES ('user', ?)", (question,))
        con.execute("INSERT INTO library_messages (role, content) VALUES ('assistant', ?)", (answer,))
    return {"answer": answer}
