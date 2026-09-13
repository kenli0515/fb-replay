/* Spoiler-free replay index: NOW TV highlights + YouTube, rendered from data/matches.json. */

const YT_EMBED = "https://www.youtube-nocookie.com/embed/";

/* NOW TV 播放器固定 660x400，位於 mobileweb 頁面內 (20, 227)；y=641 起就係帶比分嘅列表。 */
const NOW_EMBED = "https://sports.now.com/mobileweb/video#tvVideoId=";
const NOW_CROP = { "crop-x": 20, "crop-y": 227, "frame-w": 700, "frame-h": 800, "crop-pad": 12, "box-w": 660, "box-h": 424 };

const state = { matches: [], query: "", league: "all", mask: true, generatedAt: null };

/* ---------- helpers ---------- */

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2).toLowerCase(), value);
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) if (child) node.append(child);
  return node;
}

function formatDuration(seconds) {
  if (!seconds) return "";
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return `${h ? h + ":" : ""}${h ? pad(m) : m}:${pad(s)} 分鐘`;
}

function formatDate(iso) {
  if (!iso) return "";
  const then = new Date(`${iso}T00:00:00`);
  const days = Math.round((Date.now() - then.getTime()) / 86400000);
  const label = `${then.getMonth() + 1}月${then.getDate()}日`;
  if (days === 0) return `今日 · ${label}`;
  if (days === 1) return `昨日 · ${label}`;
  if (days > 1 && days < 7) return `${days} 日前 · ${label}`;
  return iso;
}

function toast(message) {
  let node = document.querySelector(".toast");
  if (!node) {
    node = el("div", { class: "toast" });
    document.body.append(node);
  }
  node.textContent = message;
  node.classList.add("on");
  clearTimeout(node._timer);
  node._timer = setTimeout(() => node.classList.remove("on"), 1800);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("已複製連結");
  } catch {
    const area = el("textarea", { style: "position:fixed;opacity:0" });
    area.value = text;
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
    toast("已複製連結");
  }
}

/* ---------- playback ---------- */

function mountFrame(card, src, sourceLabel, maskable, crop) {
  const stage = card.querySelector(".stage");
  stage.textContent = "";

  const frame = el("iframe", {
    src,
    allow: "autoplay; encrypted-media; picture-in-picture; fullscreen",
    allowfullscreen: "true",
    referrerpolicy: "origin",
    scrolling: "no",
    loading: "eager",
  });
  const wrap = el("div", { class: crop ? "frame-wrap crop" : "frame-wrap" }, frame);
  if (maskable && state.mask) wrap.append(el("div", { class: "title-mask" }));
  if (crop) {
    for (const [key, value] of Object.entries(NOW_CROP)) wrap.style.setProperty(`--${key}`, `${value}px`);
    wrap.append(el("div", { class: "bar t" }), el("div", { class: "bar b" }));
  }

  const close = el("button", { class: "ghost", type: "button", text: "閂咗播放器" });
  close.addEventListener("click", () => {
    stage.textContent = "";
    stage.classList.remove("on");
    card.classList.remove("playing");
  });
  const open = el("a", {
    class: "ghost btn",
    href: src,
    target: "_blank",
    rel: "noopener noreferrer",
    text: "新標籤開啟",
  });
  const bar = el("div", { class: "framebar" },
    el("span", { class: "src", text: sourceLabel || "" }),
    el("span", { class: "actions" }, open, close),
  );

  const scaler = crop ? el("div", { class: "crop-scaler" }, wrap) : null;
  stage.append(scaler || wrap, bar);
  stage.classList.add("on");
  if (scaler) fitCrop(scaler);
  card.classList.add("playing");
  card.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function fitCrop(scaler) {
  const wrap = scaler.querySelector(".frame-wrap");
  if (!wrap) return;
  const scale = Math.min(1, scaler.clientWidth / NOW_CROP["box-w"]);
  wrap.style.transform = `scale(${scale})`;
  scaler.style.height = `${Math.round(NOW_CROP["box-h"] * scale)}px`;
}

/* ---------- card ---------- */

function plainText(match) {
  const lines = [
    `${match.homeZh || match.home} vs ${match.awayZh || match.away}`,
    `${match.home} vs ${match.away}`,
    [match.date, match.league].filter(Boolean).join(" · "),
  ];
  for (const video of match.youtube || []) {
    lines.push(`YouTube: https://www.youtube.com/watch?v=${video.id}`);
  }
  if (match.nowtvUrl) lines.push(`NOW TV: ${match.nowtvUrl}`);
  return lines.join("\n");
}

function createCard(match) {
  const card = el("article", { class: "card" });
  const primary = (match.youtube || [])[0];

  const homeLabel = match.homeZh || match.home;
  const awayLabel = match.awayZh || match.away;
  const latin = match.homeZh ? `${match.home} vs ${match.away}` : "";
  const title = el("div", { class: "teams", text: `${homeLabel} vs ${awayLabel}` });
  if (latin) title.append(el("span", { class: "latin", text: latin }));

  const metaBits = [formatDate(match.date), match.league];
  if (match.durationSeconds) metaBits.push(formatDuration(match.durationSeconds));
  const meta = el("div", { class: "meta", text: metaBits.filter(Boolean).join(" · ") });

  const actions = el("div", { class: "actions" });
  if (primary) {
    const play = el("button", { class: "primary", type: "button", text: "▶ 睇 YouTube 精華" });
    play.addEventListener("click", () =>
      mountFrame(
        card,
        `${YT_EMBED}${primary.id}?autoplay=1&rel=0&modestbranding=1&playsinline=1`,
        "YouTube 精華",
        true,
      ),
    );
    actions.append(play);
    actions.append(el("a", {
      class: "ghost btn",
      href: `https://www.youtube.com/watch?v=${primary.id}`,
      target: "_blank",
      rel: "noopener noreferrer",
      text: "YouTube 開啟",
    }));
  } else {
    actions.append(el("span", { class: "note warn", text: "YouTube 未搵到可靠精華" }));
  }

  if (match.nowtvUrl) {
    const embed = match.videoId ? `${NOW_EMBED}${match.videoId}` : null;
    if (embed) {
      const playNow = el("button", { class: "btn now", type: "button", text: "▶ 站內睇 NOW TV 精華" });
      playNow.addEventListener("click", () =>
        mountFrame(card, embed, "NOW TV 官方精華（站內播放）", false, true),
      );
      actions.append(playNow);
    }
    actions.append(el("a", {
      class: "ghost btn",
      href: match.nowtvUrl,
      target: "_blank",
      rel: "noopener noreferrer",
      text: "NOW TV 開啟 ↗",
    }));
  }
  const copy = el("button", { class: "ghost", type: "button", text: "複製連結" });
  copy.addEventListener("click", () => copyText(plainText(match)));
  actions.append(copy);

  const notes = [];
  if (primary) {
    notes.push(el("span", { text: [primary.channel, formatDuration(primary.durationSeconds)].filter(Boolean).join(" · ") }));
    if (primary.titleHidden) {
      notes.push(el("span", { class: "warn", text: "（原標題已收起，可能暗示賽果）" }));
    }
  }
  const noteLine = el("div", { class: "note" }, ...notes);

  const nowNote = el("div", { class: "note", text: "NOW TV 站內播放只露出播放器，比分同結果縮圖已經裁走；首次播放會彈出一次同意視窗，按 Consent 之後就唔會再問。" });

  card.append(
    el("header", {}, title, meta),
    actions,
    noteLine,
    ...(match.nowtvUrl ? [nowNote] : []),
    el("div", { class: "stage" }),
  );
  return card;
}

/* ---------- page ---------- */

function visibleMatches() {
  const query = state.query.trim().toLowerCase();
  return state.matches.filter((match) => {
    if (state.league !== "all" && match.league !== state.league) return false;
    if (!query) return true;
    const haystack = [
      match.home, match.away, match.homeZh, match.awayZh, match.league, match.titleZh,
    ].filter(Boolean).join(" ").toLowerCase();
    return haystack.includes(query);
  });
}

function render() {
  const list = document.getElementById("list");
  const status = document.getElementById("status");
  const matches = visibleMatches();
  list.textContent = "";

  if (!matches.length) {
    status.className = "status";
    status.textContent = state.matches.length
      ? "冇符合嘅比賽，試下第二個隊名。"
      : "data/matches.json 未有資料，先執行 scripts/build_data.py。";
    return;
  }
  status.className = "status";
  status.textContent = `${matches.length} 場比賽 · 點「睇 YouTube 精華」即喺本頁播放`;
  for (const match of matches) list.append(createCard(match));
}

function renderLeagueChips() {
  const box = document.getElementById("leagues");
  const leagues = [...new Set(state.matches.map((match) => match.league).filter(Boolean))];
  const options = [["all", "全部"], ...leagues.map((league) => [league, league])];
  box.textContent = "";
  for (const [value, text] of options) {
    const chip = el("button", { class: "chip", type: "button", "aria-pressed": String(state.league === value), text });
    chip.addEventListener("click", () => {
      state.league = value;
      renderLeagueChips();
      render();
    });
    box.append(chip);
  }
}

async function init() {
  const status = document.getElementById("status");
  try {
    const response = await fetch("data/matches.json", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    state.matches = payload.matches || [];
    state.generatedAt = payload.generatedAt || null;
  } catch (error) {
    status.className = "status error";
    status.textContent = `載入失敗：${error.message}。本機開啟要用 http server，例如 python3 -m http.server 8765。`;
    return;
  }
  if (state.generatedAt) {
    const when = new Date(state.generatedAt);
    document.getElementById("stamp").textContent =
      `資料更新：${when.toLocaleString("zh-HK", { hour12: false })}`;
  }
  document.getElementById("search").addEventListener("input", (event) => {
    state.query = event.target.value;
    render();
  });
  document.getElementById("mask").addEventListener("change", (event) => {
    state.mask = event.target.checked;
    document.querySelectorAll(".title-mask").forEach((node) => node.remove());
    if (state.mask) {
      document.querySelectorAll(".frame-wrap iframe").forEach((frame) => {
        frame.parentElement.prepend(el("div", { class: "title-mask" }));
      });
    }
  });
  renderLeagueChips();
  render();
}

window.addEventListener("resize", () => {
  document.querySelectorAll(".crop-scaler").forEach(fitCrop);
});

init();
