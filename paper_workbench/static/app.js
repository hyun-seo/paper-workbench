import * as pdfjs from "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.7.76/pdf.min.mjs";

pdfjs.GlobalWorkerOptions.workerSrc =
  "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.7.76/pdf.worker.min.mjs";

const $ = (id) => document.getElementById(id);
const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
};

async function api(path, options) {
  const res = await fetch(`/api${path}`, options);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `요청 실패 (${res.status})`);
  }
  return res.json();
}

function progressText(p) {
  const total = p.done + p.failed + p.pending;
  if (p.pending && p.running) return `번역 중 ${p.done}/${total}`;
  if (p.failed || p.pending) return `번역 ${p.done}/${total} · 미완료 ${p.failed + p.pending}`;
  return "번역 완료";
}

// 화면 전환 때마다 올려서, 이전 화면의 폴링·렌더 작업이 스스로 멈추게 한다.
let viewToken = 0;

/* ---------- 서재 ---------- */

const ROLES = { proposes: "제안", uses: "사용", compares: "비교 대상", mentions: "언급" };

// 서재·개념·리더 중 하나만 보이게 한다.
function showView(view) {
  for (const id of ["library", "overview", "graph", "concepts", "reader"]) $(id).hidden = id !== view;
  for (const link of document.querySelectorAll(".nav a[data-view]")) link.classList.toggle("on", link.dataset.view === view);
  if (view !== "reader") document.title = "Paper Workbench";
}

async function showLibrary() {
  const token = ++viewToken;
  showView("library");

  const refresh = async () => {
    const [papers, inbox] = await Promise.all([api("/papers"), api("/inbox")]);
    if (token !== viewToken) return;
    $("inbox-dir").textContent = inbox.dir;
    $("inbox-errors").replaceChildren(
      ...Object.entries(inbox.errors).map(([name, reason]) => el("li", null, `${name}: ${reason}`)),
    );
    const list = $("paper-list");
    list.replaceChildren();
    if (!papers.length) list.append(el("li", "empty", "아직 추가한 논문이 없습니다."));
    for (const p of papers) {
      const li = el("li");
      const link = el("a");
      link.href = `#/paper/${p.id}`;
      link.append(el("div", "title", p.title), el("div", "status", `${p.page_count}쪽 · ${progressText(p.progress)}`));
      if (p.one_line) link.append(el("div", "one-line", p.one_line));
      const redo = el("button", null, "다시 추출");
      redo.title = "PDF에서 문단·수식·그림을 다시 뽑습니다. 원문이 그대로인 문단의 번역은 유지되고, 달라진 문단만 다시 번역합니다.";
      redo.disabled = p.progress.running;
      redo.onclick = async () => {
        if (!confirm(`"${p.title}"을(를) PDF에서 다시 추출할까요? 달라진 문단만 다시 번역합니다.`)) return;
        await api(`/papers/${p.id}/reprocess`, { method: "POST" }).catch((err) => alert(err.message));
        refresh();
      };
      const del = el("button", null, "삭제");
      del.onclick = async () => {
        if (!confirm(`"${p.title}" 논문과 노트를 삭제할까요? PDF 파일은 폴더의 _removed로 옮겨집니다.`)) return;
        await api(`/papers/${p.id}`, { method: "DELETE" });
        refresh();
      };
      li.append(link, redo, del);
      list.append(li);
    }
  };
  // 폴더에 새로 들어온 PDF와 번역 진행을 반영하도록 주기적으로 다시 읽는다.
  const tick = async () => {
    if (token !== viewToken) return;
    await refresh().catch(() => {});
    setTimeout(tick, 3000);
  };
  tick();
}

$("add-form").onsubmit = async (e) => {
  e.preventDefault();
  const url = $("add-url").value.trim();
  const status = $("add-status");
  if (!url) return;
  status.className = "status";
  status.textContent = "PDF를 받아 문단을 추출하는 중…";
  try {
    const { id } = await api("/papers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    e.target.reset();
    status.textContent = "";
    location.hash = `#/paper/${id}`;
  } catch (err) {
    status.className = "status error";
    status.textContent = err.message;
  }
};

/* ---------- 리더 ---------- */

let activePane = null; // 사용자가 지금 스크롤하고 있는 패널 (동기화 되먹임 방지)
for (const id of ["pdf-pane", "tr-pane"]) {
  for (const type of ["pointerenter", "wheel", "touchstart"]) {
    $(id).addEventListener(type, () => (activePane = id), { passive: true });
  }
}

// 패널 위쪽 1/3 지점에 걸친 페이지 번호
function currentPage(pane, selector) {
  const line = pane.getBoundingClientRect().top + pane.clientHeight / 3;
  let page = 1;
  for (const node of pane.querySelectorAll(selector)) {
    if (node.getBoundingClientRect().top > line) break;
    page = Number(node.dataset.page);
  }
  return page;
}

function scrollPaneTo(pane, node) {
  if (!node) return;
  pane.scrollTop += node.getBoundingClientRect().top - pane.getBoundingClientRect().top - 8;
}

function setupSync() {
  const pdfPane = $("pdf-pane");
  const trPane = $("tr-pane");
  const last = { "pdf-pane": 0, "tr-pane": 0 };
  const follow = (srcId, srcSel, dst, dstSel) => () => {
    if (!$("sync").checked || activePane !== srcId) return;
    const page = currentPage($(srcId), srcSel);
    if (page === last[srcId]) return;
    last[srcId] = page;
    scrollPaneTo(dst, dst.querySelector(`${dstSel}[data-page="${page}"]`));
  };
  pdfPane.onscroll = follow("pdf-pane", ".pdf-page", trPane, ".page-mark");
  trPane.onscroll = follow("tr-pane", ".page-mark", pdfPane, ".pdf-page");
}

async function renderPdf(paperId, token) {
  const pane = $("pdf-pane");
  pane.replaceChildren();
  const doc = await pdfjs.getDocument(`/api/papers/${paperId}/pdf`).promise;
  if (token !== viewToken) return;

  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        observer.unobserve(entry.target);
        drawPage(doc, entry.target);
      }
    },
    { root: pane, rootMargin: "800px 0px" },
  );

  for (let n = 1; n <= doc.numPages; n++) {
    const page = await doc.getPage(n);
    if (token !== viewToken) return;
    const { width, height } = page.getViewport({ scale: 1 });
    const holder = el("div", "pdf-page");
    holder.dataset.page = n;
    holder.dataset.width = width;
    holder.style.aspectRatio = `${width} / ${height}`;
    holder.style.maxWidth = `${Math.round(width * 1.6)}px`;
    pane.append(holder);
    observer.observe(holder);
  }
  fitTextLayers();
}

// 텍스트 레이어는 --scale-factor에 맞춰 글자 위치·크기를 잡으므로, 페이지 폭이 바뀌면 다시 맞춘다.
function fitTextLayers() {
  for (const holder of $("pdf-pane").querySelectorAll(".pdf-page")) {
    holder.style.setProperty("--scale-factor", holder.clientWidth / Number(holder.dataset.width));
  }
}
new ResizeObserver(fitTextLayers).observe($("pdf-pane"));

async function drawPage(doc, holder) {
  const page = await doc.getPage(Number(holder.dataset.page));
  const base = page.getViewport({ scale: 1 });
  const scale = Math.max(1.5, (holder.clientWidth * devicePixelRatio) / base.width);
  const viewport = page.getViewport({ scale });
  const canvas = el("canvas");
  canvas.width = viewport.width;
  canvas.height = viewport.height;
  holder.prepend(canvas);
  await page.render({ canvasContext: canvas.getContext("2d"), viewport }).promise;

  // 원문을 드래그로 선택할 수 있게 투명한 텍스트 레이어를 캔버스 위에 얹는다.
  const layer = el("div", "textLayer");
  holder.append(layer);
  await new pdfjs.TextLayer({
    textContentSource: page.streamTextContent(),
    container: layer,
    viewport: page.getViewport({ scale: holder.clientWidth / base.width }),
  }).render();
}

// 열려 있는 논문의 문단들과 번역 패널의 해당 노드
let reader = { blocks: [], nodes: new Map() };

// 원본 페이지 위의 화면 좌표가 속한 블록. 영역이 겹치면(넓혀 잡은 그림·수식과 그 옆 문단 등) 가장 작은 영역의 블록을 고른다.
function blockAt(holder, clientX, clientY) {
  const rect = holder.getBoundingClientRect();
  const x = (clientX - rect.left) / rect.width;
  const y = (clientY - rect.top) / rect.height;
  const page = Number(holder.dataset.page);
  const pad = 0.004;
  let best = null;
  let bestArea = Infinity;
  for (const block of reader.blocks) {
    for (const [p, x0, y0, x1, y1] of block.parts) {
      const area = (x1 - x0) * (y1 - y0);
      if (p === page && x >= x0 - pad && x <= x1 + pad && y >= y0 - pad && y <= y1 + pad && area < bestArea) {
        best = block;
        bestArea = area;
      }
    }
  }
  return best;
}

// 원문을 누르면 그 문단의 번역으로 이동한다. 드래그 선택 중이거나 영역 선택 모드면 건너뛴다.
$("pdf-pane").addEventListener("click", (e) => {
  const holder = e.target.closest(".pdf-page");
  if (!holder || capturing || getSelection().toString().trim()) return;
  const block = blockAt(holder, e.clientX, e.clientY);
  const node = block && reader.nodes.get(block.id);
  if (!node) return;
  highlight(block, node, false);
  node.scrollIntoView({ block: "center", behavior: "smooth" });
});

function highlight(block, node, scrollPdf = true) {
  document.querySelector(".block.active")?.classList.remove("active");
  for (const old of document.querySelectorAll(".pdf-hl")) old.remove();
  node.classList.add("active");
  // 단·쪽을 넘어 이어진 문단은 영역이 여러 개다. 모두 칠하고 첫 영역으로 이동한다.
  let first = null;
  for (const [page, x0, y0, x1, y1] of block.parts) {
    const holder = $("pdf-pane").querySelector(`.pdf-page[data-page="${page}"]`);
    if (!holder) continue;
    const hl = el("div", "pdf-hl");
    hl.style.left = `${x0 * 100}%`;
    hl.style.top = `${y0 * 100}%`;
    hl.style.width = `${(x1 - x0) * 100}%`;
    hl.style.height = `${(y1 - y0) * 100}%`;
    holder.append(hl);
    first ??= hl;
  }
  activePane = null; // 이 스크롤이 반대쪽 패널을 다시 끌고 가지 않게 한다
  if (scrollPdf) first?.scrollIntoView({ block: "center", behavior: "smooth" });
}

// 번역문을 문장 단위로 나눈다. "et al." 같은 약어 뒤에서는 끊지 않는다.
const ABBREVIATION = /(?:\b(?:et al|Fig|Figs|Eq|Eqs|Sec|Tab|Ref|vs|cf|No|etc|e\.g|i\.e)|\b[A-Z])\.$/;
function sentenceLines(text) {
  const lines = [];
  for (const part of text.split(/(?<=[.!?。][)\]"'”’]?)\s+/)) {
    const last = lines.length - 1;
    if (last >= 0 && ABBREVIATION.test(lines[last])) lines[last] += ` ${part}`;
    else lines.push(part);
  }
  return lines;
}

// $...$ 수식을 렌더링한다 (KaTeX를 못 불러오면 원문 그대로 둔다).
function renderMath(node) {
  window.renderMathInElement?.(node, {
    delimiters: [
      { left: "$$", right: "$$", display: true },
      { left: "\\[", right: "\\]", display: true },
      { left: "$", right: "$", display: false },
      { left: "\\(", right: "\\)", display: false },
    ],
    throwOnError: false,
  });
}

function fillBlock(node, block) {
  node.className = `block ${block.status}`;
  node.title = block.status === "failed" ? block.error || "" : block.status === "done" ? block.original : "";
  if (block.status === "done") {
    // 문장마다 한 줄씩, 사이를 띄워서 보여 준다 (긴 문장이 접힌 줄과 구분되게).
    node.replaceChildren(...sentenceLines(block.translated).map((line) => el("span", "sentence", line)));
    renderMath(node);
  } else {
    node.textContent = block.original;
  }
}

function renderBlocks(paperId, blocks) {
  const pane = $("tr-pane");
  pane.replaceChildren();
  const nodes = new Map();
  let page = 0;
  for (const block of blocks) {
    if (block.page !== page) {
      page = block.page;
      const mark = el("div", "page-mark", `p. ${page}`);
      mark.dataset.page = page;
      pane.append(mark);
    }
    const node = el(block.kind === "text" ? "p" : "div");
    node.dataset.id = block.id;
    if (block.kind === "text") {
      fillBlock(node, block);
    } else {
      // 수식·그림·표는 번역하지 않고 원본 영역을 잘라 보여 준다.
      node.className = `block ${block.kind}`;
      const img = el("img");
      img.loading = "lazy";
      img.alt = block.original || "그림";
      img.src = `/api/papers/${paperId}/blocks/${block.id}/image`;
      node.append(img);
    }
    node.onclick = () => highlight(block, node);
    nodes.set(block.id, node);
    pane.append(node);
  }
  reader = { blocks, nodes };
  return nodes;
}

function setupNote(paperId, note) {
  const area = $("note");
  const status = $("note-status");
  area.value = note;
  status.textContent = "";
  let timer;
  area.oninput = () => {
    status.className = "status";
    status.textContent = "저장 중…";
    clearTimeout(timer);
    timer = setTimeout(async () => {
      try {
        await api(`/papers/${paperId}/note`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ note: area.value }),
        });
        status.textContent = "저장됨";
      } catch (err) {
        status.className = "status error";
        status.textContent = `저장 실패: ${err.message}`;
      }
    }, 600);
  };
}

// 원본과 번역 패널을 모두 해당 쪽으로 옮긴다.
function jumpToPage(page) {
  activePane = null;
  scrollPaneTo($("pdf-pane"), $("pdf-pane").querySelector(`.pdf-page[data-page="${page}"]`));
  scrollPaneTo($("tr-pane"), $("tr-pane").querySelector(`.page-mark[data-page="${page}"]`));
}

function conceptTitle(c) {
  return c.label.toLowerCase() === c.name.toLowerCase() ? c.name : `${c.name} (${c.label})`;
}

// 요약 탭: 논문 카드와 주요 개념. 아직 만드는 중이면 끝날 때까지 다시 읽는다.
function setupSummary(paperId, summary, token) {
  const box = $("tab-summary");
  const redo = (label) => {
    const button = el("button", "redo", label);
    button.onclick = async () => {
      await api(`/papers/${paperId}/card`, { method: "POST" });
      setupSummary(paperId, { status: "pending" }, token);
    };
    return button;
  };
  box.className = "summary";
  box.replaceChildren();
  if (summary.status === "pending") {
    box.append(el("p", "status", "요약과 주요 개념을 만드는 중…"));
    setTimeout(async () => {
      if (token !== viewToken) return;
      const fresh = await api(`/papers/${paperId}/card`).catch(() => summary);
      if (token === viewToken) setupSummary(paperId, fresh, token);
    }, 4000);
    return;
  }
  if (summary.status === "failed") {
    box.append(el("p", "status error", `요약을 만들지 못했습니다: ${summary.error}`), redo("다시 만들기"));
    return;
  }
  box.append(el("p", "one-line", summary.card.one_line));
  for (const [key, label] of [["problem", "문제"], ["method", "방법"], ["results", "결과"], ["limitations", "한계"]]) {
    if (summary.card[key]) box.append(el("h3", null, label), el("p", null, summary.card[key]));
  }
  if (summary.concepts.length) {
    const list = el("ul");
    for (const c of summary.concepts) {
      const item = el("li");
      const name = el("a", null, conceptTitle(c));
      name.title = c.definition;
      if (c.page) name.onclick = () => jumpToPage(c.page);
      item.append(name, el("span", "role", ` [${ROLES[c.role]}${c.page ? `, p. ${c.page}` : ""}] `), c.note);
      if (c.papers > 1) {
        const others = el("a", "role", ` · 논문 ${c.papers}편`);
        others.href = "#/concepts";
        item.append(others);
      }
      list.append(item);
    }
    box.append(el("h3", null, "주요 개념"), list);
  }
  box.append(redo("요약 다시 만들기"));
}

async function showConcepts() {
  const token = ++viewToken;
  showView("concepts");
  const concepts = await api("/concepts");
  if (token !== viewToken) return;
  const shared = concepts.filter((c) => c.papers.length > 1).length;
  $("concepts-status").textContent = concepts.length
    ? `개념 ${concepts.length}개 · 여러 논문에 걸친 개념 ${shared}개`
    : "아직 개념이 없습니다. 논문의 요약이 만들어지면 여기에 모입니다.";
  const list = $("concept-list");
  list.replaceChildren();
  for (const c of concepts) {
    const card = el("div", "concept");
    const title = el("h2", null, conceptTitle(c));
    if (c.papers.length > 1) title.append(el("span", "count", `논문 ${c.papers.length}편`));
    const papers = el("ul");
    for (const p of c.papers) {
      const item = el("li");
      const link = el("a", null, p.title);
      link.href = `#/paper/${p.id}${p.page ? `/p${p.page}` : ""}`;
      item.append(link, el("span", "role", ` [${ROLES[p.role]}${p.page ? `, p. ${p.page}` : ""}] `), p.note);
      papers.append(item);
    }
    card.append(title, el("p", null, c.definition), papers);
    list.append(card);
  }
}

/* ---------- 종합: 서재 전체를 꿰어 보기 ---------- */

const shortTitle = (title, n = 34) => (title.length > n ? `${title.slice(0, n - 1)}…` : title);

// 논문마다 가장 잘 맞는 주제(처음 등장하는 주제)의 번호. 주제 색은 이 번호로 고정된다.
function primaryThemes(data) {
  const theme = new Map();
  (data?.themes || []).forEach((t, i) => t.papers.forEach((m) => theme.has(m.id) || theme.set(m.id, i)));
  return theme;
}

// 글 속의 [P3] 표시를 그 논문으로 가는 링크로 바꿔 node에 채운다.
function fillCited(node, text, titles) {
  node.replaceChildren();
  for (const part of text.split(/(\[P\d+\])/)) {
    const m = part.match(/^\[P(\d+)\]$/);
    const id = m && Number(m[1]);
    if (m && titles.has(id)) {
      const link = el("a", "ref", `〈${shortTitle(titles.get(id), 26)}〉`);
      link.href = `#/paper/${id}`;
      link.title = titles.get(id);
      node.append(link);
    } else {
      node.append(part);
    }
  }
  return node;
}

async function showOverview() {
  const token = ++viewToken;
  showView("overview");
  const body = $("overview-body");
  const status = $("overview-status");

  const render = async () => {
    const s = await api("/synthesis");
    if (token !== viewToken) return;
    const titles = new Map(s.papers.map((p) => [p.id, p.title]));
    const paperLink = (id, n) => {
      const link = el("a", null, shortTitle(titles.get(id) || `논문 ${id}`, n));
      link.href = `#/paper/${id}`;
      link.title = titles.get(id) || "";
      return link;
    };
    const cited = (tag, className, text) => fillCited(el(tag, className), text, titles);
    const redo = el("button", null, "다시 종합");
    redo.onclick = async () => {
      await api("/synthesis", { method: "POST" });
      render();
    };

    status.className = "status";
    if (s.status === "empty") status.textContent = "요약이 만들어진 논문이 2편 이상이면 서재 전체 종합이 여기에 만들어집니다.";
    else if (s.status === "pending") status.textContent = "서재 전체를 종합하는 중… (1~2분 걸립니다)";
    else if (s.status === "failed") {
      status.className = "status error";
      status.textContent = `종합을 만들지 못했습니다: ${s.error} `;
    } else status.textContent = `논문 ${s.papers.length}편을 종합했습니다. 논문이 추가·삭제되면 자동으로 다시 만듭니다. `;
    if (s.status === "done" || s.status === "failed") status.append(redo);
    if (s.status === "pending") setTimeout(() => token === viewToken && render(), 5000);

    const data = s.data;
    // 질문 입력 중이면 화면을 다시 그리지 않는다.
    if (!data || document.activeElement?.id === "library-input") return;
    const theme = primaryThemes(data);
    const swatch = (i) => {
      const mark = el("span", "swatch");
      if (i != null) mark.style.background = `var(--theme-${i + 1})`;
      return mark;
    };
    body.replaceChildren(cited("p", "lead", data.overview));

    // 서재 전체에 질문
    const log = el("div");
    log.id = "library-log";
    const form = el("form", "ask-library");
    const input = el("textarea");
    input.id = "library-input";
    input.rows = 2;
    input.placeholder = "서재 전체에 질문하세요. 예: 이 논문들이 '선호'를 다루는 방식은 어떻게 다른가? (Enter 전송)";
    const send = el("button", null, "질문");
    send.type = "submit";
    form.append(input, send);
    const addAnswer = (m, className = "") => {
      const node = el("div", `msg ${m.role} ${className}`);
      fillCited(node, m.content, titles);
      if (m.role === "assistant" && !className) renderMath(node);
      log.append(node);
      return node;
    };
    const clear = el("button", null, "질문 기록 지우기");
    clear.id = "library-clear";
    clear.hidden = true;
    clear.onclick = async () => {
      if (!confirm("서재 질문 기록을 모두 지울까요?")) return;
      await api("/library/messages", { method: "DELETE" });
      log.replaceChildren();
      clear.hidden = true;
    };
    api("/library/messages").then((messages) => {
      messages.forEach((m) => addAnswer(m));
      clear.hidden = !messages.length;
    });
    form.onsubmit = async (e) => {
      e.preventDefault();
      const question = input.value.trim();
      if (!question || send.disabled) return;
      addAnswer({ role: "user", content: question });
      const pending = addAnswer({ role: "assistant", content: "답변 작성 중… (논문 요약들을 읽고 답합니다)" }, "pending");
      input.value = "";
      send.disabled = true;
      try {
        const { answer } = await api("/library/ask", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ question }),
        });
        pending.remove();
        addAnswer({ role: "assistant", content: answer });
        clear.hidden = false;
      } catch (err) {
        pending.className = "msg assistant error";
        pending.textContent = err.message;
      } finally {
        send.disabled = false;
      }
    };
    input.onkeydown = (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        form.requestSubmit();
      }
    };
    body.append(form, log, clear);

    // 주제
    const themes = el("div", "themes");
    data.themes.forEach((t, i) => {
      const card = el("div", "theme");
      const title = el("h3");
      title.append(swatch(i), t.name);
      const list = el("ul");
      for (const m of t.papers) {
        const item = el("li");
        item.append(paperLink(m.id, 40), " ", cited("span", null, m.role));
        list.append(item);
      }
      card.append(title, cited("p", null, t.description), list);
      themes.append(card);
    });
    body.append(el("h2", null, "주제"), themes);

    // 비교표
    const { dimensions, rows } = data.comparison;
    if (dimensions.length && rows.length) {
      const table = el("table", "compare");
      const head = el("tr");
      head.append(el("th", null, "논문"), ...dimensions.map((d) => el("th", null, d)));
      const thead = el("thead");
      thead.append(head);
      const tbody = el("tbody");
      for (const r of rows) {
        const tr = el("tr");
        const name = el("td");
        name.append(swatch(theme.get(r.id)), paperLink(r.id, 48));
        tr.append(name, ...dimensions.map((_d, i) => cited("td", null, r.cells[i] || "")));
        tbody.append(tr);
      }
      table.append(thead, tbody);
      const wrap = el("div", "table-wrap");
      wrap.append(table);
      body.append(el("h2", null, "나란히 비교"), wrap);
    }

    // 논문 사이의 관계
    if (data.relations.length) {
      const relations = el("div", "relations");
      for (const r of data.relations) {
        const card = el("div", "relation");
        const pair = el("div", "pair");
        pair.append(paperLink(r.a, 30), r.type === "builds_on" ? " → " : " ↔ ", paperLink(r.b, 30));
        card.append(pair, el("span", "kind", s.relation_types[r.type]), cited("p", null, r.note));
        relations.append(card);
      }
      const title = el("h2", null, "논문 사이의 관계 ");
      const map = el("a", "status", "지도로 보기");
      map.href = "#/graph";
      title.append(map);
      body.append(title, relations);
    }

    // 열린 질문과 아이디어
    if (data.ideas.length) {
      const ideas = el("div", "ideas");
      for (const idea of data.ideas) {
        const card = el("div", "idea");
        const who = el("div", "who");
        idea.papers.forEach((id, i) => who.append(i ? " · " : "", paperLink(id, 28)));
        card.append(el("h3", null, idea.title), cited("p", null, idea.description), who);
        ideas.append(card);
      }
      body.append(el("h2", null, "열린 질문과 아이디어"), ideas);
    }

    // 읽는 순서
    if (data.reading_order.length) {
      const order = el("ol", "order");
      for (const o of data.reading_order) {
        const item = el("li");
        item.append(swatch(theme.get(o.id)), paperLink(o.id, 60), " ", cited("span", "status", o.why));
        order.append(item);
      }
      body.append(el("h2", null, "읽는 순서"), order);
    }
  };
  body.replaceChildren();
  render();
}

/* ---------- 지도 ---------- */

// 논문(네모, 주제 색)과 개념(회색 동그라미)의 지도. 굵은 선은 종합에서 나온 논문 사이의 관계다.
async function showGraph() {
  const token = ++viewToken;
  showView("graph");
  const [papers, concepts, synthesis] = await Promise.all([api("/papers"), api("/concepts"), api("/synthesis")]);
  if (token !== viewToken) return;
  const shared = concepts.filter((c) => c.papers.length > 1).length;
  $("graph-shared").checked = shared > 0 && concepts.length > 30;
  $("graph-concepts").onchange = $("graph-shared").onchange = () => drawGraph(papers, concepts, synthesis, token);
  const relations = synthesis.data?.relations.length || 0;
  $("graph-status").textContent =
    `논문 ${papers.length}편 · 논문 사이의 관계 ${relations}개 · 개념 ${concepts.length}개 (여러 논문에 걸친 개념 ${shared}개)` +
    (synthesis.data ? "" : " · 종합이 만들어지면 주제 색과 논문 사이의 관계가 표시됩니다");

  // 범례: 주제 색(논문), 모양, 선 종류
  const legend = $("graph-legend");
  legend.replaceChildren();
  const item = (mark, text) => {
    const span = el("span", "legend");
    span.append(mark, text);
    legend.append(span);
  };
  (synthesis.data?.themes || []).forEach((t, i) => {
    const mark = el("i", "mark");
    mark.style.background = `var(--theme-${i + 1})`;
    item(mark, t.name);
    // 논문은 가장 잘 맞는 주제의 색으로 칠해지지만 여러 주제에 속할 수 있다. 주제를 누르면 그 주제의 논문이 모두 강조된다.
    const key = legend.lastElementChild;
    key.classList.add("theme-key");
    key.title = "누르면 이 주제에 속한 논문만 강조합니다";
    key.onclick = () => {
      const on = !key.classList.contains("on");
      for (const other of legend.querySelectorAll(".theme-key")) other.classList.remove("on");
      key.classList.toggle("on", on);
      graphSelectTheme?.(on ? i : null);
    };
  });
  item(el("i", "mark round"), "개념");
  item(el("i", "line"), "논문 사이의 관계");
  item(el("i", "line thin"), "논문–개념");
  item(el("i", "line dashed"), "언급 · 개념 사이");
  drawGraph(papers, concepts, synthesis, token);
}

let graphSelectTheme = null; // 현재 그려진 지도에서 주제 하나를 강조하는 함수

function drawGraph(papers, concepts, synthesis, token) {
  const svg = d3.select("#graph-svg");
  svg.selectAll("*").remove();
  const info = $("graph-info");
  const tip = $("graph-tip");
  const data = synthesis.data;
  const theme = primaryThemes(data);
  const titles = new Map(papers.map((p) => [p.id, p.title]));
  const showConcepts = $("graph-concepts").checked;
  $("graph-shared").disabled = !showConcepts;
  const shown = !showConcepts ? [] : $("graph-shared").checked ? concepts.filter((c) => c.papers.length > 1) : concepts;

  const nodes = [
    ...papers.map((p) => ({ key: `p${p.id}`, kind: "paper", paper: p, label: shortTitle(p.title, 30), r: 11 })),
    ...shown.map((c) => ({ key: `c${c.id}`, kind: "concept", concept: c, label: c.name, r: 3 + 2 * c.papers.length })),
  ];
  const byKey = new Map(nodes.map((n) => [n.key, n]));
  const conceptKey = new Map(shown.map((c) => [c.name.toLowerCase(), `c${c.id}`]));
  const links = [
    // 논문 사이의 관계 (종합). 같은 두 논문 사이의 관계가 여럿이면 선 하나에 모은다.
    ...[...d3.group((data?.relations || []).filter((r) => byKey.has(`p${r.a}`) && byKey.has(`p${r.b}`)),
      (r) => [r.a, r.b].sort((x, y) => x - y).join("-")).values()]
      .map((group) => ({ source: `p${group[0].a}`, target: `p${group[0].b}`, kind: "relation", relations: group })),
    // 논문–개념
    ...shown.flatMap((c) =>
      c.papers.filter((p) => byKey.has(`p${p.id}`)).map((p) => ({ source: `p${p.id}`, target: `c${c.id}`, kind: p.role })),
    ),
    // 개념 사이의 관계
    ...synthesis.concept_links
      .filter((l) => conceptKey.has(l.source.toLowerCase()) && conceptKey.has(l.target.toLowerCase()))
      .map((l) => ({ source: conceptKey.get(l.source.toLowerCase()), target: conceptKey.get(l.target.toLowerCase()), kind: "concept-link", conceptLink: l })),
  ];
  const neighbors = new Map(nodes.map((n) => [n.key, new Set([n.key])]));
  for (const l of links) {
    neighbors.get(l.source).add(l.target);
    neighbors.get(l.target).add(l.source);
  }

  const { width, height } = $("graph-svg").getBoundingClientRect();
  const view = svg.append("g");
  svg.call(d3.zoom().scaleExtent([0.3, 4]).on("zoom", (e) => view.attr("transform", e.transform)));

  const link = view.append("g").selectAll("line").data(links).join("line").attr("class", (l) => `link ${l.kind}`);
  // 굵은 관계선은 누르기 쉽게 보이지 않는 넓은 선을 겹쳐 둔다.
  const hit = view.append("g").selectAll("line").data(links.filter((l) => l.kind === "relation")).join("line").attr("class", "link-hit");
  const node = view.append("g").selectAll("g").data(nodes).join("g").attr("class", (n) => `node ${n.kind}`);
  node.filter((n) => n.kind === "paper").append("rect").attr("class", "shape")
    .attr("x", -11).attr("y", -11).attr("width", 22).attr("height", 22).attr("rx", 5)
    .style("fill", (n) => (theme.has(n.paper.id) ? `var(--theme-${theme.get(n.paper.id) + 1})` : null));
  node.filter((n) => n.kind === "concept").append("circle").attr("class", "shape").attr("r", (n) => n.r);
  node.append("text").attr("x", (n) => n.r + 5).attr("y", 4).text((n) => n.label);

  const simulation = d3.forceSimulation(nodes)
    .force("link", d3.forceLink(links).id((n) => n.key)
      .distance((l) => (l.kind === "relation" ? 220 : l.kind === "mentions" ? 130 : 95))
      .strength((l) => (l.kind === "relation" ? 0.25 : l.kind === "concept-link" ? 0.05 : 0.5)))
    .force("charge", d3.forceManyBody().strength((n) => (n.kind === "paper" ? -900 : -260)))
    .force("x", d3.forceX(width / 2).strength(0.05))
    .force("y", d3.forceY(height / 2).strength(0.08))
    .force("collide", d3.forceCollide((n) => n.r + (n.kind === "paper" ? 46 : 22)))
    .on("tick", () => {
      if (token !== viewToken) return simulation.stop();
      for (const sel of [link, hit]) {
        sel.attr("x1", (l) => l.source.x).attr("y1", (l) => l.source.y).attr("x2", (l) => l.target.x).attr("y2", (l) => l.target.y);
      }
      node.attr("transform", (n) => `translate(${n.x},${n.y})`);
    });

  node.call(d3.drag()
    .on("start", (e, n) => { if (!e.active) simulation.alphaTarget(0.3).restart(); n.fx = n.x; n.fy = n.y; })
    .on("drag", (e, n) => { n.fx = e.x; n.fy = e.y; })
    .on("end", (e, n) => { if (!e.active) simulation.alphaTarget(0); n.fx = n.fy = null; }));

  // 호버: 한 줄 설명
  const showTip = (e, text) => {
    tip.hidden = false;
    tip.textContent = text;
    tip.style.left = `${Math.min(e.clientX + 14, innerWidth - 340)}px`;
    tip.style.top = `${e.clientY + 14}px`;
  };
  const relationLabel = (group) =>
    `${shortTitle(titles.get(group[0].a), 24)} ↔ ${shortTitle(titles.get(group[0].b), 24)} · ` +
    group.map((r) => synthesis.relation_types[r.type]).join(", ");
  node
    .on("pointermove", (e, n) =>
      showTip(e, n.kind === "paper" ? n.paper.one_line || n.paper.title : `${conceptTitle(n.concept)}: ${n.concept.definition}`))
    .on("pointerleave", () => (tip.hidden = true));
  hit.on("pointermove", (e, l) => showTip(e, relationLabel(l.relations))).on("pointerleave", () => (tip.hidden = true));

  const paperLink = (id, page) => {
    const a = el("a", null, titles.get(id) || `논문 ${id}`);
    a.href = `#/paper/${id}${page ? `/p${page}` : ""}`;
    return a;
  };
  const relationItem = (r, self) => {
    const item = el("li");
    const other = self == null ? null : r.a === self ? r.b : r.a;
    if (other == null) item.append(paperLink(r.a), r.type === "builds_on" ? " → " : " ↔ ", paperLink(r.b));
    else item.append(paperLink(other));
    item.append(el("span", "role", ` [${synthesis.relation_types[r.type]}] `), fillCited(el("span"), r.note, titles));
    return item;
  };
  const showInfo = (n, relations) => {
    info.replaceChildren();
    if (relations) {
      const list = el("ul");
      for (const r of relations) list.append(relationItem(r));
      info.append(el("h2", null, "논문 사이의 관계"), list);
      return;
    }
    if (!n) {
      info.append(el("p", null, "네모는 논문(색은 주제), 동그라미는 개념입니다. 점이나 굵은 선을 누르면 설명이 여기에 나옵니다. 끌어서 옮기고, 휠로 확대·축소합니다."));
      const table = el("a", null, "글과 표로 보기 (종합)");
      table.href = "#/overview";
      info.append(table);
      return;
    }
    if (n.kind === "concept") {
      const c = n.concept;
      const list = el("ul");
      for (const p of c.papers) {
        const item = el("li");
        item.append(paperLink(p.id, p.page), el("span", "role", ` [${ROLES[p.role]}${p.page ? `, p. ${p.page}` : ""}] `), p.note);
        list.append(item);
      }
      info.append(el("h2", null, conceptTitle(c)), el("p", null, c.definition), list);
      const related = synthesis.concept_links.filter((l) => [l.source, l.target].some((x) => x.toLowerCase() === c.name.toLowerCase()));
      if (related.length) {
        const rel = el("ul");
        for (const l of related) rel.append(el("li", null, `${l.source} → ${l.target}${synthesis.concept_link_types[l.type]}${l.note ? `: ${l.note}` : ""}`));
        info.append(el("h3", null, "개념 사이의 관계"), rel);
      }
      return;
    }
    const p = n.paper;
    const title = el("h2");
    title.append(paperLink(p.id));
    info.append(title, el("p", null, p.one_line || "요약이 아직 없습니다."));
    const myThemes = (data?.themes || []).map((t, i) => [t, i]).filter(([t]) => t.papers.some((m) => m.id === p.id));
    if (myThemes.length) {
      const list = el("ul");
      for (const [t, i] of myThemes) {
        const item = el("li");
        const mark = el("span", "swatch");
        mark.style.background = `var(--theme-${i + 1})`;
        item.append(mark, `${t.name}: `, fillCited(el("span"), t.papers.find((m) => m.id === p.id).role, titles));
        list.append(item);
      }
      info.append(el("h3", null, "주제"), list);
    }
    const mine = (data?.relations || []).filter((r) => r.a === p.id || r.b === p.id);
    if (mine.length) {
      const list = el("ul");
      for (const r of mine) list.append(relationItem(r, p.id));
      info.append(el("h3", null, "다른 논문과의 관계"), list);
    }
    const own = concepts.filter((c) => c.papers.some((x) => x.id === p.id));
    if (own.length) {
      const list = el("ul");
      for (const c of own) {
        const role = c.papers.find((x) => x.id === p.id).role;
        const item = el("li");
        const name = el("a", null, conceptTitle(c));
        name.title = c.definition;
        name.onclick = () => byKey.has(`c${c.id}`) && select(byKey.get(`c${c.id}`));
        item.append(name, el("span", "role", ` [${ROLES[role]}] `), c.papers.length > 1 ? `논문 ${c.papers.length}편` : "");
        list.append(item);
      }
      info.append(el("h3", null, "개념"), list);
    }
  };
  const select = (n, relations) => {
    const near = n ? neighbors.get(n.key) : relations ? new Set([`p${relations[0].a}`, `p${relations[0].b}`]) : null;
    node.classed("dim", (m) => near && !near.has(m.key)).classed("selected", (m) => n && m.key === n.key);
    link
      .classed("dim", (l) => near && (relations ? l.relations !== relations : l.source.key !== n.key && l.target.key !== n.key))
      .classed("selected", (l) => relations && l.relations === relations);
    showInfo(n, relations);
  };
  node.on("click", (e, n) => { e.stopPropagation(); select(n); });
  hit.on("click", (e, l) => { e.stopPropagation(); select(null, l.relations); });
  svg.on("click", () => select(null));
  select(null);

  // 범례에서 주제를 고르면 그 주제에 속한 논문과 그 사이의 관계만 남기고 흐리게 한다.
  graphSelectTheme = (index) => {
    if (index == null) return select(null);
    const t = data.themes[index];
    const members = new Set(t.papers.map((m) => `p${m.id}`));
    node.classed("dim", (m) => !members.has(m.key)).classed("selected", false);
    link.classed("dim", (l) => !(members.has(l.source.key) && members.has(l.target.key))).classed("selected", false);
    info.replaceChildren();
    const list = el("ul");
    for (const m of t.papers) {
      const item = el("li");
      item.append(paperLink(m.id), " ", fillCited(el("span"), m.role, titles));
      list.append(item);
    }
    info.append(el("h2", null, t.name), fillCited(el("p"), t.description, titles), list);
  };
}

async function showReader(paperId, page) {
  const token = ++viewToken;
  let paper;
  try {
    paper = await api(`/papers/${paperId}`);
  } catch {
    location.hash = "#/";
    return;
  }
  if (token !== viewToken) return;

  showView("reader");
  $("paper-title").textContent = paper.title;
  document.title = `${paper.title} · Paper Workbench`;
  $("pdf-pane").scrollTop = $("tr-pane").scrollTop = 0;

  const nodes = renderBlocks(paperId, paper.blocks);
  setupNote(paperId, paper.note);
  setupChat(paperId);
  setupSummary(paperId, paper.summary, token);
  $("export").href = `/api/papers/${paperId}/export.md`;
  setupSync();
  renderPdf(paperId, token).then(() => page && jumpToPage(page)).catch((err) => {
    $("pdf-pane").replaceChildren(el("p", "status error", `PDF를 표시하지 못했습니다: ${err.message}`));
  });

  const showProgress = (p) => {
    $("progress").textContent = progressText(p);
    $("retry").hidden = p.running || !(p.failed || p.pending);
  };
  const poll = async () => {
    if (token !== viewToken) return;
    const fresh = await api(`/papers/${paperId}`);
    if (token !== viewToken) return;
    for (const block of fresh.blocks) {
      const node = nodes.get(block.id);
      if (node && block.kind === "text" && !node.classList.contains(block.status)) {
        const active = node.classList.contains("active");
        fillBlock(node, block);
        node.classList.toggle("active", active);
      }
    }
    showProgress(fresh.progress);
    if (fresh.progress.running) setTimeout(poll, 2000);
  };
  showProgress(paper.progress);
  if (paper.progress.running) setTimeout(poll, 2000);

  $("retry").onclick = async () => {
    $("retry").hidden = true;
    await api(`/papers/${paperId}/translate`, { method: "POST" });
    poll();
  };
}

/* ---------- 사이드 패널 (노트 / 질문) ---------- */

function openSide(tab) {
  if (tab) localStorage.setItem("sideTab", tab);
  const current = localStorage.getItem("sideTab") || "note";
  $("side-pane").hidden = false;
  localStorage.setItem("sideOpen", "1");
  for (const btn of document.querySelectorAll(".tabs button")) btn.classList.toggle("on", btn.dataset.tab === current);
  $("tab-summary").hidden = current !== "summary";
  $("tab-note").hidden = current !== "note";
  $("tab-chat").hidden = current !== "chat";
  $("note-status").hidden = current !== "note";
}
$("toggle-side").onclick = () => {
  if ($("side-pane").hidden) return openSide();
  $("side-pane").hidden = true;
  localStorage.removeItem("sideOpen");
};
for (const btn of document.querySelectorAll(".tabs button")) btn.onclick = () => openSide(btn.dataset.tab);
if (localStorage.getItem("sideOpen")) openSide();

/* ---------- 질문 ---------- */

let attachments = []; // 다음 질문에 붙일 것들: { quote, blockId } 또는 { image }

function renderAttachments() {
  const box = $("chat-attach");
  box.replaceChildren();
  box.hidden = !attachments.length;
  for (const item of attachments) {
    const chip = el("div", "attach-item");
    if (item.image) {
      const img = el("img");
      img.src = item.image;
      chip.append(img);
    } else {
      chip.append(el("div", "quote", item.quote));
    }
    const remove = el("button", null, "×");
    remove.title = "첨부 취소";
    remove.onclick = () => {
      attachments = attachments.filter((a) => a !== item);
      renderAttachments();
    };
    chip.append(remove);
    box.append(chip);
  }
  box.scrollTop = box.scrollHeight;
}

function addAttachment(item) {
  attachments.push(item);
  renderAttachments();
  openSide("chat");
  $("chat-input").focus();
}

function clearAttachments() {
  attachments = [];
  renderAttachments();
}

function addMessage(m, className = "") {
  const log = $("chat-log");
  log.querySelector(".empty")?.remove();
  const node = el("div", `msg ${m.role} ${className}`);
  for (const src of m.images || []) {
    const img = el("img");
    img.src = src;
    node.append(img);
  }
  for (const quote of m.quotes || []) node.append(el("div", "quote", quote));
  const body = el("div", null, m.content);
  node.append(body);
  if (m.role === "assistant" && !className) renderMath(body);
  log.append(node);
  log.scrollTop = log.scrollHeight;
  return node;
}

async function setupChat(paperId) {
  const token = viewToken;
  const log = $("chat-log");
  const input = $("chat-input");
  const showEmpty = () =>
    log.replaceChildren(
      el("div", "empty", "논문 내용을 물어보세요. 번역문을 드래그하거나, 상단의 '영역 선택'으로 원본의 수식·그림을 잘라 붙여서 질문할 수도 있습니다."),
    );
  clearAttachments();
  input.value = "";
  showEmpty();
  const messages = await api(`/papers/${paperId}/messages`);
  if (token !== viewToken) return;
  messages.forEach((m) => addMessage(m));

  $("chat-form").onsubmit = async (e) => {
    e.preventDefault();
    const question = input.value.trim() || (attachments.length ? "이 부분을 설명해 주세요." : "");
    if (!question || $("chat-send").disabled) return;
    const quotes = attachments.filter((a) => a.quote);
    const images = attachments.filter((a) => a.image).map((a) => a.image);
    addMessage({ role: "user", content: question, quotes: quotes.map((q) => q.quote), images });
    const pending = addMessage({ role: "assistant", content: "답변 작성 중…" }, "pending");
    input.value = "";
    clearAttachments();
    $("chat-send").disabled = true;
    try {
      const { answer } = await api(`/papers/${paperId}/ask`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question,
          quotes: quotes.map((q) => ({ text: q.quote, block_id: q.blockId })),
          images,
        }),
      });
      if (token !== viewToken) return;
      pending.remove();
      addMessage({ role: "assistant", content: answer });
    } catch (err) {
      pending.className = "msg assistant error";
      pending.textContent = err.message;
    } finally {
      $("chat-send").disabled = false;
    }
  };
  input.onkeydown = (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      $("chat-form").requestSubmit();
    }
  };
  $("chat-clear").onclick = async () => {
    if (!confirm("이 논문의 질문·답변 기록을 모두 지울까요?")) return;
    await api(`/papers/${paperId}/messages`, { method: "DELETE" });
    showEmpty();
  };
}

// 번역문에서 텍스트를 드래그하면 '이 부분 질문하기' 버튼을 띄운다.
document.addEventListener("mouseup", (e) => {
  const button = $("ask-selection");
  if (e.target === button) return;
  setTimeout(() => {
    const selection = getSelection();
    const text = selection.toString().trim();
    const inPdf = $("pdf-pane").contains(selection.anchorNode);
    if (!text || !(inPdf || $("tr-pane").contains(selection.anchorNode))) {
      button.hidden = true;
      return;
    }
    const range = selection.getRangeAt(0);
    const rect = range.getBoundingClientRect();
    button.hidden = false;
    button.style.left = `${Math.max(8, Math.min(rect.left, innerWidth - 160))}px`;
    button.style.top = `${Math.min(rect.bottom + 6, innerHeight - 40)}px`;
    button.onclick = () => {
      const anchor = selection.anchorNode;
      const element = anchor.nodeType === Node.TEXT_NODE ? anchor.parentElement : anchor;
      if (inPdf) {
        // 원문 선택: 선택이 시작된 줄의 위치로 문단을 찾아, 그 문단 원문도 함께 보낸다.
        const first = range.getClientRects()[0] || rect;
        const holder = element.closest(".pdf-page");
        const block = holder && blockAt(holder, first.left + first.width / 2, first.top + first.height / 2);
        addAttachment({ quote: text.replace(/\s+/g, " "), blockId: block?.id });
      } else {
        const block = element.closest(".block");
        addAttachment({ quote: text, blockId: block ? Number(block.dataset.id) : undefined });
      }
      button.hidden = true;
      selection.removeAllRanges();
    };
  });
});
$("ask-selection").onmousedown = (e) => e.preventDefault(); // 버튼을 눌러도 선택이 풀리지 않게

// 원본 PDF에서 영역을 드래그해 이미지로 첨부한다 (수식·그림용).
let capturing = false;
function setCapture(on) {
  capturing = on;
  $("pdf-pane").classList.toggle("capturing", on);
  $("capture").classList.toggle("on", on);
}
$("capture").onclick = () => setCapture(!capturing);
$("pdf-pane").addEventListener("pointerdown", (e) => {
  const holder = capturing && e.target.closest(".pdf-page");
  const canvas = holder && holder.querySelector("canvas");
  if (!canvas) return;
  e.preventDefault();
  const page = holder.getBoundingClientRect();
  const box = el("div", "capture-box");
  holder.append(box);
  const area = (ev) => {
    const x = Math.max(page.left, Math.min(ev.clientX, page.right));
    const y = Math.max(page.top, Math.min(ev.clientY, page.bottom));
    return {
      left: Math.min(x, e.clientX) - page.left,
      top: Math.min(y, e.clientY) - page.top,
      width: Math.abs(x - e.clientX),
      height: Math.abs(y - e.clientY),
    };
  };
  const move = (ev) => {
    for (const [key, value] of Object.entries(area(ev))) box.style[key] = `${value}px`;
  };
  const up = (ev) => {
    window.removeEventListener("pointermove", move);
    box.remove();
    setCapture(false);
    const a = area(ev);
    if (a.width < 8 || a.height < 8) return;
    const ratio = canvas.width / page.width;
    const scale = Math.min(1, 1400 / (a.width * ratio));
    const out = el("canvas");
    out.width = a.width * ratio * scale;
    out.height = a.height * ratio * scale;
    out.getContext("2d").drawImage(
      canvas, a.left * ratio, a.top * ratio, a.width * ratio, a.height * ratio, 0, 0, out.width, out.height,
    );
    addAttachment({ image: out.toDataURL("image/png") });
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up, { once: true });
});

/* ---------- 라우팅 ---------- */

function route() {
  if (location.hash === "#/concepts") return showConcepts();
  if (location.hash === "#/graph") return showGraph();
  if (location.hash === "#/overview") return showOverview();
  const m = location.hash.match(/^#\/paper\/(\d+)(?:\/p(\d+))?$/);
  if (m) showReader(Number(m[1]), m[2] ? Number(m[2]) : undefined);
  else showLibrary();
}
window.addEventListener("hashchange", route);
route();
