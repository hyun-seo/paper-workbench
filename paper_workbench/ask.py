import json

from . import db
from .translate import MODEL, REASONING_EFFORT, TARGET_LANG, get_client

MAX_PAPER_CHARS = 120_000
HISTORY_MESSAGES = 12

SYSTEM_PROMPT = """You help a reader understand the academic paper below. Answer in {lang}.
- Base your answer on the paper. If you go beyond what the paper says, make that clear; \
if the paper does not contain the answer, say so.
- The reader may attach passages they selected or cropped images of the PDF (often formulas). \
Explain those specific parts: what each symbol or term means and what role it plays in the paper.
- Write plain text without Markdown markup (no **bold**, no # headings).
- Write every formula and every symbol with a subscript or superscript as LaTeX, wrapped in $...$ (inline) \
or $$...$$ (its own line), e.g. $d_k$ or $$\\mathrm{{softmax}}(x)$$. Never write math as plain text like sqrt(dk).
- Be concise.

<paper title="{title}">
{text}
</paper>"""


def _paper_text(con, paper_id: int) -> str:
    parts, page = [], 0
    for row in con.execute("SELECT page, original FROM blocks WHERE paper_id = ? ORDER BY id", (paper_id,)):
        if row["page"] != page:
            page = row["page"]
            parts.append(f"[p. {page}]")
        if row["original"]:  # 그림 블록은 글이 없다
            parts.append(row["original"])
    return "\n\n".join(parts)[:MAX_PAPER_CHARS]


def _user_text(question: str, quotes: list[str], originals: list[str], image_count: int) -> str:
    parts = [f"[독자가 선택한 부분 {i}]\n{q}" for i, q in enumerate(quotes, start=1)]
    parts += [f"[선택한 부분이 속한 원문 문단]\n{o}" for o in originals]
    if image_count:
        parts.append(f"[독자가 PDF에서 잘라낸 영역 이미지 {image_count}장 첨부]")
    parts.append(f"[질문]\n{question}" if parts else question)
    return "\n\n".join(parts)


def history(paper_id: int) -> list[dict]:
    with db.connect() as con:
        rows = con.execute(
            "SELECT id, role, content, quotes, images FROM messages WHERE paper_id = ? ORDER BY id", (paper_id,)
        ).fetchall()
    return [{**dict(r), "quotes": json.loads(r["quotes"]), "images": json.loads(r["images"])} for r in rows]


async def ask(
    paper_id: int,
    question: str,
    quotes: list[str],
    block_ids: list[int],
    images: list[str],
) -> dict:
    """논문 내용을 바탕으로 질문에 답하고, 질문과 답을 대화 기록에 남긴다."""
    with db.connect() as con:
        title = con.execute("SELECT title FROM papers WHERE id = ?", (paper_id,)).fetchone()["title"]
        text = _paper_text(con, paper_id)
        marks = ",".join("?" * len(block_ids))
        originals = [
            r["original"]
            for r in con.execute(
                f"SELECT original FROM blocks WHERE paper_id = ? AND id IN ({marks}) ORDER BY id",
                (paper_id, *block_ids),
            )
        ]
    past = history(paper_id)[-HISTORY_MESSAGES:]

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT.format(lang=TARGET_LANG, title=title, text=text)}
    ]
    for m in past:
        content = m["content"]
        if m["role"] == "user":
            content = _user_text(content, m["quotes"], [], len(m["images"]))
        messages.append({"role": m["role"], "content": content})

    user_text = _user_text(question, quotes, originals, len(images))
    if images:
        parts = [{"type": "text", "text": user_text}]
        parts += [{"type": "image_url", "image_url": {"url": url}} for url in images]
        messages.append({"role": "user", "content": parts})
    else:
        messages.append({"role": "user", "content": user_text})

    extra = {"reasoning_effort": REASONING_EFFORT} if REASONING_EFFORT else {}
    resp = await get_client().chat.completions.create(model=MODEL, messages=messages, **extra)
    answer = (resp.choices[0].message.content or "").strip()

    with db.connect() as con:
        con.execute(
            "INSERT INTO messages (paper_id, role, content, quotes, images) VALUES (?, 'user', ?, ?, ?)",
            (paper_id, question, json.dumps(quotes, ensure_ascii=False), json.dumps(images)),
        )
        con.execute("INSERT INTO messages (paper_id, role, content) VALUES (?, 'assistant', ?)", (paper_id, answer))
    return {"answer": answer}
