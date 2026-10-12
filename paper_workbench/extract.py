import re
from collections import Counter
from dataclasses import dataclass, field

import pymupdf

VERSION = 3  # 추출 규칙이 바뀌면 올린다. 예전 버전으로 뽑은 논문은 서버가 뜰 때 다시 뽑는다.
MIN_CHARS = 20
REFERENCES_HEADING = re.compile(r"^\s*(\d+\.?\s*)?(references|bibliography)\s*$", re.I)
# 수식에 쓰이는 글꼴 (Computer Modern 수학 글꼴, AMS 기호 등)
MATH_FONT = re.compile(r"CMMI|CMSY|CMEX|MSBM|MSAM|Math|Symbol|StandardSymL|rsfs|eufm", re.I)
# 본문이 다른 글꼴이면 Computer Modern Roman도 수식(함수 이름·숫자)에만 쓰인다.
MATH_ROMAN_FONT = re.compile(r"CMR\d", re.I)
FORMULA_RATIO = 0.6  # 수식 글꼴 비율이 이보다 높으면 별행 수식으로 본다
EQUATION_NUMBER = re.compile(r"\(\d+[a-z]?\)[.,]?$")
MATH_SIGN = re.compile(r"[=∑∏∫∈≤≥≈∗]|arg ?m(?:ax|in)")
# 큰 괄호·행렬 괄호처럼 확장 글꼴에서만 나오는 글리프. 텍스트로는 제어 문자나 사용자 정의 영역 문자로 뽑힌다.
BIG_DELIMITER = re.compile(r"[\x00-\x08\uf8e6-\uf8ff]")
LIST_ITEM = re.compile(r"(?:[•·▪◦–-]|\(?\d+[.)]|\(?[a-z][.)])\s")
CAPTION = re.compile(r"(?:^|\s)(Figure|Fig\.|Table)\s*\d+\s*[:.]")
SENTENCE_END = re.compile(r"[.!?:;。][\"')\]”’]?$")


@dataclass
class Block:
    page: int
    bbox: tuple[float, float, float, float]  # 페이지 크기 대비 0~1 비율
    text: str
    kind: str  # text | formula | figure (formula·figure는 원본 영역을 이미지로 보여 준다)
    translatable: bool
    # 블록이 차지하는 모든 영역 [(쪽, x0, y0, x1, y1)]. 단·쪽을 넘어 이어진 문단은 여러 개다. 첫 항목이 page·bbox와 같다.
    parts: list[tuple[int, float, float, float, float]] = field(default_factory=list)


@dataclass
class _Group:
    """한 문단(또는 수식)으로 묶인 PDF 텍스트 블록들"""

    page: int
    rect: pymupdf.Rect
    lines: list = field(default_factory=list)  # (rect, text, 글자 수, 수식 글자 수)
    more: list = field(default_factory=list)  # 단·쪽을 넘어 이어진 나머지 영역들 (쪽, rect)
    merged: bool = False
    text: str = ""
    kind: str = "text"

    def build(self) -> None:
        lines = self.lines
        if self.merged:
            # 조각난 블록을 합쳤으면 줄을 위→아래, 왼→오른 순서로 다시 세운다.
            rows: list[list] = []
            for line in sorted(lines, key=lambda ln: (ln[0].y0 + ln[0].y1) / 2):
                center = (line[0].y0 + line[0].y1) / 2
                if rows and center - rows[-1][0] < line[0].height * 0.5:
                    rows[-1][1].append(line)
                else:
                    rows.append([center, [line]])
            lines = [ln for _c, row in rows for ln in sorted(row, key=lambda ln: ln[0].x0)]
        self.text = _clean("\n".join(ln[1] for ln in lines))
        ratio = sum(ln[3] for ln in lines) / (sum(ln[2] for ln in lines) or 1)
        # 별행 수식: 수식 글꼴이 대부분이거나, 짧은 블록이 식 번호 "(1)"로 끝나면서 수식 기호가 있거나,
        # 큰 괄호 글리프가 들어 있는 경우. (변수를 본문 이탤릭체로 조판한 논문은 글꼴 비율만으로는 못 잡는다.)
        # 영어 낱말이 많으면 수식 기호가 섞인 문장이지 별행 수식이 아니다.
        short = len(self.text) < 200 and len(re.findall(r"[A-Za-z]{4,}", self.text)) <= 8
        numbered = short and EQUATION_NUMBER.search(self.text) and (ratio >= 0.15 or MATH_SIGN.search(self.text))
        delimited = short and BIG_DELIMITER.search(self.text)
        self.kind = "formula" if ratio >= FORMULA_RATIO or numbered or delimited else "text"


def _clean(text: str) -> str:
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)  # 줄 끝 하이픈 연결
    return re.sub(r"\s+", " ", text).strip()


def _figure_regions(page: pymupdf.Page, layout: dict) -> list[pymupdf.Rect]:
    """그림·표처럼 텍스트로 옮기면 깨지는 영역. 번역 패널에서는 원본을 잘라 이미지로 보여 준다."""
    rects = [pymupdf.Rect(b["bbox"]) for b in layout["blocks"] if b["type"] == 1]
    try:
        tables = page.find_tables()
        rects += [pymupdf.Rect(t.bbox) for t in (tables.tables if tables else [])]
    except Exception:
        pass
    drawings = page.get_drawings()
    # 가로줄만 있는 표(booktabs): 같은 폭의 긴 가로줄이 3개 이상이면 맨 위 줄부터 맨 아래 줄까지를 표로 본다.
    rules: dict[tuple[int, int], list[pymupdf.Rect]] = {}
    for d in drawings:
        r = d["rect"]
        if r.width > page.rect.width * 0.3 and r.height < 3:
            rules.setdefault((round(r.x0 / 4), round(r.x1 / 4)), []).append(r)
    for group in rules.values():
        if len(group) >= 3:
            rects.append(pymupdf.Rect(group[0].x0, min(r.y0 for r in group), group[0].x1, max(r.y1 for r in group)))
    try:
        rects += [r for r in page.cluster_drawings(drawings=drawings) if r.width > 80 and r.height > 60]
    except Exception:
        pass

    page_area = page.rect.width * page.rect.height
    rects = [r & page.rect for r in rects]
    rects = [r for r in rects if r.width > 40 and r.height > 20 and r.width * r.height < page_area * 0.85]
    merged: list[pymupdf.Rect] = []
    for r in rects:
        r = pymupdf.Rect(r)
        while True:
            hit = next((m for m in merged if m.intersects(r)), None)
            if hit is None:
                break
            merged.remove(hit)
            r |= hit
        merged.append(r)
    return sorted(merged, key=lambda r: r.y0)


def _same_band(a: pymupdf.Rect, b: pymupdf.Rect) -> bool:
    """두 블록이 같은 줄 높이에 붙어 있는가 (문장 속 수식 때문에 한 줄이 여러 블록으로 쪼개진 경우)."""
    overlap = min(a.y1, b.y1) - max(a.y0, b.y0)
    gap = max(b.x0 - a.x1, a.x0 - b.x1)
    return overlap >= min(a.height, b.height) * 0.5 and gap < 12


def _merge_near(rects: list[pymupdf.Rect], pad: float) -> list[pymupdf.Rect]:
    """pad 이내로 붙어 있는 사각형들을 하나로 합친다."""
    merged: list[pymupdf.Rect] = []
    for r in rects:
        r = pymupdf.Rect(r)
        while True:
            hit = next((m for m in merged if (m + (-pad, -pad, pad, pad)).intersects(r)), None)
            if hit is None:
                break
            merged.remove(hit)
            r |= hit
        merged.append(r)
    return merged


def _caption_figures(
    regions: list[pymupdf.Rect], groups: list[_Group], page_width: float
) -> tuple[list[pymupdf.Rect], list[_Group]]:
    """하위 그림 여러 장으로 된 그림을 캡션 기준으로 하나로 묶는다.

    캡션("Figure 3:", "Table 1:") 바로 위나 아래의 영역에서 시작해 가까운 영역을 계속 붙이고,
    캡션 폭까지 넓혀 "(a) Input" 같은 하위 라벨과 글자로 그린 도식도 한 장에 담는다.
    """
    free = _merge_near(regions, 14)  # 한 줄로 늘어선 하위 그림들을 먼저 띠로 묶는다 (단 사이 간격보다는 좁게)

    def is_label(g: _Group) -> bool:  # 그림 안의 라벨: 짧거나, 길어도 폭이 좁다 (본문 문단은 단 폭을 채운다)
        return g.kind == "text" and (len(g.text) < 120 or g.rect.width < page_width * 0.3)
    figures: list[pymupdf.Rect] = []
    captions = [g for g in groups if g.kind == "text" and CAPTION.search(g.text[:80])]
    for cap in captions:
        c = cap.rect

        def same_column(r: pymupdf.Rect) -> bool:  # 영역의 가운데가 캡션 폭 안에 있어야 같은 단의 그림이다
            return c.x0 - 20 <= (r.x0 + r.x1) / 2 <= c.x1 + 20

        def gap(r: pymupdf.Rect) -> float:  # 캡션과 영역 사이의 세로 거리. 다른 단이면 무한대
            if not same_column(r):
                return float("inf")
            return c.y0 - r.y1 if r.y1 <= c.y0 + 5 else r.y0 - c.y1 if r.y0 >= c.y1 - 5 else float("inf")

        def prose_between(a: pymupdf.Rect, b: pymupdf.Rect) -> bool:
            """두 영역 사이(세로)에 본문 문단이 끼어 있는가. 짧은 하위 라벨만 있으면 같은 그림으로 본다."""
            top, bottom = (a, b) if a.y0 <= b.y0 else (b, a)
            band = pymupdf.Rect(min(a.x0, b.x0), top.y1, max(a.x1, b.x1), bottom.y0)
            return any(
                g is not cap and g.kind == "text" and not is_label(g) and g.rect.intersects(band) for g in groups
            )

        near = [r for r in free if -5 <= gap(r) < 150 and not prose_between(r, c)]
        if not near:
            continue
        seed = min(near, key=gap)
        above = seed.y1 <= c.y0 + 5
        union = pymupdf.Rect(seed)
        free.remove(seed)
        while True:  # 같은 쪽(캡션 위 또는 아래)에 있는 가까운 영역을 계속 붙인다. 격자형 그림은 행 사이에 라벨이 있다
            more = [
                r
                for r in free
                if same_column(r)
                and (union + (-30, -70, 30, 70)).intersects(r)
                and (r.y1 <= c.y0 + 5 if above else r.y0 >= c.y1 - 5)
                and not prose_between(r, union)
            ]
            if not more:
                break
            for r in more:
                union |= r
                free.remove(r)
        union.x0, union.x1 = min(union.x0, c.x0), max(union.x1, c.x1)
        # 하위 라벨까지 담되 캡션 글자는 걸치지 않게, 이미지를 자를 때 더하는 여백(3pt)만큼 캡션에서 띄운다.
        # (사이에 본문이 없다는 것은 위에서 확인했으므로, 캡션까지의 빈 곳에는 하위 라벨만 있다.)
        if above:
            union.y1 = c.y0 - 3
        else:
            union.y0 = c.y1 + 3
        figures.append(union)

    # 캡션이 없는 영역(부록의 이미지 격자 등)도 가까운 것끼리 한 장으로 묶고, 서로 겹치는 그림은 합친다.
    figures = _merge_near(figures + _merge_near(free, 14), 0)

    def inside_figure(g: _Group) -> bool:
        center = pymupdf.Point((g.rect.x0 + g.rect.x1) / 2, (g.rect.y0 + g.rect.y1) / 2)
        return g not in captions and is_label(g) and any(center in f for f in figures)

    return sorted(figures, key=lambda r: r.y0), [g for g in groups if not inside_figure(g)]


def _page_groups(page_no: int, layout: dict, figures: list[pymupdf.Rect], is_math) -> list[_Group]:
    groups: list[_Group] = []
    for block in layout["blocks"]:
        if block["type"] != 0:
            continue
        rect = pymupdf.Rect(block["bbox"])
        center = pymupdf.Point((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
        if any(center in f for f in figures):  # 그림·표 안의 글자는 이미지에 포함된다
            continue
        lines = []
        for line in block["lines"]:
            if abs(line["dir"][0] - 1) > 0.01:  # arXiv 세로 스탬프 같은 회전된 글자는 제외
                continue
            text = "".join(span["text"] for span in line["spans"])
            chars = sum(len(span["text"].strip()) for span in line["spans"])
            math = sum(len(span["text"].strip()) for span in line["spans"] if is_math(span["font"]))
            if chars:
                lines.append((pymupdf.Rect(line["bbox"]), text, chars, math))
        if not lines:
            continue
        if groups and _same_band(groups[-1].rect, rect):
            groups[-1].rect |= rect
            groups[-1].lines += lines
            groups[-1].merged = True
        else:
            groups.append(_Group(page_no, rect, lines))

    out: list[_Group] = []
    for g in groups:
        g.build()
        prev = out[-1] if out else None
        # 위아래·좌우로 붙은 수식 조각은 하나의 수식으로 합친다.
        if prev and prev.kind == g.kind == "formula" and (prev.rect + (-8, -8, 8, 8)).intersects(g.rect):
            prev.rect |= g.rect
            prev.text = f"{prev.text} {g.text}"
        elif g.kind == "formula" and len(g.text) >= 3:
            out.append(g)
        elif g.kind == "text" and (len(g.text) >= MIN_CHARS or re.search(r"[A-Za-z]{3,}", g.text)):
            out.append(g)  # 쪽번호·수식 조각 같은 짧은 블록은 버린다. 제목류는 살린다.

    # 수식은 좌변이나 식 번호가 다른 블록으로 떨어져 있기 쉬워, 같은 단의 본문 폭만큼 좌우로 넓혀서 자른다.
    # 단 폭은 수식과 가로로 겹치는 본문 문단 중 가장 좁은 것으로 본다 (두 단에 걸친 캡션을 피하기 위함).
    paragraphs = [t.rect for t in out if t.kind == "text" and len(t.text) > 150 and not CAPTION.search(t.text[:80])]
    formulas = [g for g in out if g.kind == "formula"]
    for g in formulas:
        center = (g.rect.x0 + g.rect.x1) / 2
        same = [c for c in paragraphs if c.x0 <= center <= c.x1]
        if same:
            column = min(same, key=lambda c: c.width)
            g.rect.x0, g.rect.x1 = min(g.rect.x0, column.x0), max(g.rect.x1, column.x1)

    def formula_scrap(t: _Group) -> bool:  # 넓힌 수식 영역 안에 든 짧은 글 조각 ("φ∗ = argmax" 등)은 이미지에 포함된다
        center = pymupdf.Point((t.rect.x0 + t.rect.x1) / 2, (t.rect.y0 + t.rect.y1) / 2)
        return t.kind == "text" and len(t.text) < 40 and any(center in (f.rect + (0, -3, 0, 3)) for f in formulas)

    return [g for g in out if not formula_scrap(g)]


def _join_paragraphs(groups: list[_Group]) -> list[_Group]:
    """문장 중간에서 끊긴 블록을 앞 문단에 잇는다 (줄바꿈·단 넘김·쪽 넘김으로 갈라진 문단)."""
    out: list[_Group] = []
    for g in groups:
        prev = out[-1] if out else None
        unfinished = (
            prev is not None
            and prev.kind == g.kind == "text"
            and not SENTENCE_END.search(prev.text)
            and not REFERENCES_HEADING.match(prev.text)
        )
        if not unfinished:
            out.append(g)
            continue
        last_page, last_rect = prev.more[-1] if prev.more else (prev.page, prev.rect)
        # 같은 단에서 이어지려면 가로로 겹치고 앞 블록보다 위에서 시작하지 않아야 한다 (위에서 시작하면 단이 바뀐 것이다).
        same_column = (
            g.page == last_page
            and g.rect.x0 < last_rect.x1
            and last_rect.x0 < g.rect.x1
            and g.rect.y0 >= last_rect.y0 - 2
        )
        gap = g.rect.y0 - last_rect.y1
        # 같은 단에서 맞닿거나 겹친 블록은 문장 속 수식 때문에 쪼개진 한 문단이다 (뒷조각이 숫자·기호로 시작하기도 한다).
        touching = same_column and gap < 4 and not LIST_ITEM.match(g.text)
        if not (touching or re.match(r"[a-z]", g.text)) or (same_column and gap >= 14):
            out.append(g)  # 새 문단이거나, 같은 단에서 멀리 떨어져 있다
            continue
        if same_column and not prev.more:
            prev.rect |= g.rect
        elif same_column:
            prev.more[-1] = (last_page, last_rect | g.rect)
        else:
            prev.more.append((g.page, pymupdf.Rect(g.rect)))  # 단·쪽이 바뀌었다: 이어진 영역으로 따로 둔다
        prev.more += g.more
        hyphen = prev.text.endswith("-")
        prev.text = prev.text[:-1] + g.text if hyphen else f"{prev.text} {g.text}"
    return out


def _insert_figures(groups: list[_Group], figures: list[tuple[int, pymupdf.Rect]]) -> list[_Group]:
    """그림을 같은 쪽에서 바로 아래에 오는 블록(보통 캡션) 앞에 끼워 넣는다."""
    for page_no, rect in figures:
        fig = _Group(page_no, rect, kind="figure")
        below = [
            i
            for i, g in enumerate(groups)
            if g.page == page_no and g.kind != "figure" and g.rect.y0 >= rect.y1 - 5 and g.rect.x0 < rect.x1
        ]
        if below:
            groups.insert(min(below, key=lambda i: groups[i].rect.y0), fig)
        else:
            after = [i for i, g in enumerate(groups) if g.page <= page_no]
            groups.insert(after[-1] + 1 if after else 0, fig)
    return groups


def extract(pdf_bytes: bytes) -> tuple[str, int, list[Block]]:
    """PDF에서 (제목, 페이지 수, 문단·수식·그림 블록)을 뽑는다."""
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        groups: list[_Group] = []
        figures: list[tuple[int, pymupdf.Rect]] = []
        sizes = {}
        layouts = [page.get_text("dict") for page in doc]
        fonts = Counter()
        for layout in layouts:
            for block in layout["blocks"]:
                for line in block.get("lines", []):
                    for span in line["spans"]:
                        fonts[span["font"]] += len(span["text"])
        body_font = fonts.most_common(1)[0][0] if fonts else ""
        roman_is_math = not MATH_ROMAN_FONT.search(body_font)

        def is_math(font: str) -> bool:
            return bool(MATH_FONT.search(font) or (roman_is_math and MATH_ROMAN_FONT.search(font)))

        for page_no, (page, layout) in enumerate(zip(doc, layouts), start=1):
            sizes[page_no] = (page.rect.width, page.rect.height)
            raw_regions = _figure_regions(page, layout)
            regions, page_groups = _caption_figures(
                raw_regions, _page_groups(page_no, layout, raw_regions, is_math), page.rect.width
            )
            figures += [(page_no, r) for r in regions]
            groups += page_groups
        groups = _insert_figures(_join_paragraphs(groups), figures)

        blocks: list[Block] = []
        in_references = False
        for g in groups:
            if g.kind == "text" and REFERENCES_HEADING.match(g.text):
                in_references = True
            parts = []
            for page_no, r in [(g.page, g.rect), *g.more]:
                w, h = sizes[page_no]
                parts.append((page_no, r.x0 / w, r.y0 / h, r.x1 / w, r.y1 / h))
            blocks.append(
                Block(
                    page=g.page,
                    bbox=parts[0][1:],
                    text=g.text,
                    kind=g.kind,
                    translatable=g.kind == "text" and not in_references,
                    parts=parts,
                )
            )
        title = (doc.metadata or {}).get("title", "").strip() or _guess_title(doc[0])
        return title or "제목 없음", doc.page_count, blocks


def _guess_title(page: pymupdf.Page) -> str:
    """메타데이터에 제목이 없으면 첫 쪽에서 글자가 가장 큰 줄들을 제목으로 본다."""
    spans = [
        span
        for block in page.get_text("dict")["blocks"]
        for line in block.get("lines", [])
        if abs(line["dir"][0] - 1) < 0.01  # arXiv 세로 스탬프 같은 회전된 글자는 제외
        for span in line["spans"]
        if len(span["text"].strip()) > 1
    ]
    if not spans:
        return ""
    biggest = max(span["size"] for span in spans)
    return _clean(" ".join(s["text"] for s in spans if s["size"] >= biggest - 0.5))[:200]
