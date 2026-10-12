import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or ROOT_DIR / "data")
PDF_DIR = DATA_DIR / "pdfs"
DB_PATH = DATA_DIR / "workbench.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    source      TEXT NOT NULL,
    page_count  INTEGER NOT NULL,
    sha256      TEXT,  -- PDF 내용 해시 (같은 파일을 두 번 가져오지 않기 위함)
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS blocks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id    INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    page        INTEGER NOT NULL,
    x0 REAL NOT NULL, y0 REAL NOT NULL, x1 REAL NOT NULL, y1 REAL NOT NULL,
    -- text | formula | figure (formula·figure는 원본 영역을 이미지로 보여 준다)
    kind        TEXT NOT NULL DEFAULT 'text',
    original    TEXT NOT NULL,
    translated  TEXT,
    -- pending | done | failed | skipped
    status      TEXT NOT NULL DEFAULT 'pending',
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_blocks_paper ON blocks(paper_id, id);
-- 여러 논문을 잇는 개념. name은 표준 영어 용어이고 논문끼리 같은 이름을 공유한다.
CREATE TABLE IF NOT EXISTS concepts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    label       TEXT NOT NULL,
    definition  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_concepts (
    paper_id    INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    concept_id  INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,  -- proposes | uses | compares
    page        INTEGER,
    note        TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (paper_id, concept_id)
);
-- 개념 사이의 관계 (from이 to의 한 종류다 / 구성 요소다 / to에 쓰인다 / to의 대안이다)
CREATE TABLE IF NOT EXISTS concept_links (
    from_id     INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    to_id       INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    type        TEXT NOT NULL,  -- is_a | part_of | used_for | alternative_to
    note        TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (from_id, to_id)
);
-- 서재 전체 종합. 한 줄만 둔다.
CREATE TABLE IF NOT EXISTS synthesis (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    data        TEXT,           -- 종합 결과 (JSON)
    fingerprint TEXT,           -- 어떤 논문 카드들로 만들었는지 (카드가 바뀌면 다시 만든다)
    error       TEXT
);
-- 서재 전체에 한 질문과 답
CREATE TABLE IF NOT EXISTS library_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    role        TEXT NOT NULL,  -- user | assistant
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id    INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,  -- user | assistant
    content     TEXT NOT NULL,
    quotes      TEXT NOT NULL DEFAULT '[]',  -- 질문에 붙인 구절들 (JSON 배열)
    images      TEXT NOT NULL DEFAULT '[]',  -- 질문에 붙인 PDF 영역들 (data URL JSON 배열)
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_messages_paper ON messages(paper_id, id);
"""


def init() -> None:
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as con:
        con.executescript(SCHEMA)
        # 예전 스키마로 만들어진 DB에 나중에 생긴 열을 추가한다.
        for table, column, ddl in [
            ("papers", "sha256", "TEXT"),
            ("papers", "card", "TEXT"),  # 요약 카드 (JSON). 아직 안 만들었으면 NULL
            ("papers", "card_error", "TEXT"),
            ("papers", "extract_version", "INTEGER NOT NULL DEFAULT 1"),
            ("blocks", "kind", "TEXT NOT NULL DEFAULT 'text'"),
            ("concepts", "aliases", "TEXT NOT NULL DEFAULT '[]'"),  # 약어·다른 표기 (JSON 배열)
            ("blocks", "parts", "TEXT"),  # 블록이 차지하는 모든 영역 [[쪽, x0, y0, x1, y1], ...] (JSON)
        ]:
            if column not in {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


@contextmanager
def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def pdf_path(paper_id: int) -> Path:
    return PDF_DIR / f"{paper_id}.pdf"
