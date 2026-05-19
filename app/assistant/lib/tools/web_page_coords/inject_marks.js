/**
 * inject_marks.js — Playwright mark injection for web_page_coords
 *
 * Loaded by web_page_coords.py via Path.read_text() and executed inside the
 * browser page via the `browser_evaluate` MCP tool. Two variables MUST be
 * provided in the enclosing scope by the Python caller before this body runs:
 *
 *   q   {string}  — the user question / task description
 *   MAX {number}  — maximum number of marks to inject (default 50)
 *
 * Returns the full marks array: [{id, x, y, label, rect}, ...]
 * window.__emi_marks_map is also written for test/debug inspection.
 *
 * History: previously this file wrapped its body in `async (page) => { ... }`
 * and called `page.evaluate(...)` for the DOM scan. That structure required the
 * playwright-mcp `browser_run_code` tool, which was removed when playwright-mcp
 * was upgraded in 2026. We now run a pure DOM-side body via `browser_evaluate`.
 * The hover-probe phase was dropped — it was best-effort only and required
 * Playwright-side mouse control (page.mouse.move + page.waitForTimeout) which
 * browser_evaluate can't access. Sites that revealed clickables only on hover
 * may yield fewer marks; reach for explicit web_modal_scan or web_click for
 * those cases.
 */

const tokens = (String(q || "").toLowerCase().match(/[a-z0-9]+/g) || []).slice(0, 12);
const WANT = new Set(tokens);

// isVisible for mark injection uses 6px minimum — small buttons (close X,
// radio options, icon buttons) must still be reachable.
function isVisible(el) {
  if (!el) return false;
  const st = window.getComputedStyle(el);
  if (!st) return false;
  if (st.display === "none" || st.visibility === "hidden" || st.opacity === "0") return false;
  if (st.pointerEvents === "none") return false;
  const r = el.getBoundingClientRect();
  if (!r || r.width < 6 || r.height < 6) return false;
  if (r.bottom < 0 || r.right < 0) return false;
  if (r.top > window.innerHeight || r.left > window.innerWidth) return false;
  return true;
}

// If a visible dialog/modal exists, scope marking to it to reduce noise.
function pickModalRoot() {
  try {
    const dialogs = Array.from(document.querySelectorAll("[role='dialog'],[aria-modal='true']"));
    let best = null;
    let bestArea = 0;
    for (const el of dialogs) {
      if (!isVisible(el)) continue;
      const r = el.getBoundingClientRect();
      const area = Math.max(0, (r.width || 0) * (r.height || 0));
      if (area > bestArea) {
        best = el;
        bestArea = area;
      }
    }
    return best;
  } catch (e) {
    return null;
  }
}

const ROOT = pickModalRoot() || document;

function textOf(el) {
  try {
    const aria = (el.getAttribute("aria-label") || "").trim();
    const title = (el.getAttribute("title") || "").trim();
    const placeholder = (el.getAttribute("placeholder") || "").trim();
    let value = "";
    try {
      value = typeof el.value === "string" ? el.value.trim() : "";
    } catch (e) {
      value = "";
    }
    const txt = (el.innerText || el.textContent || "").trim();
    let out = (aria || title || placeholder || value || txt || "").replace(/\s+/g, " ").slice(0, 80);
    try {
      const tag = (el.tagName || "").toLowerCase();
      if (tag === "a") {
        const href = (el.getAttribute("href") || "").trim();
        if (href) out = (out ? out + " " : "") + `href=${href}`;
      }
    } catch (e) {}
    return out.slice(0, 110);
  } catch (e) {
    return "";
  }
}

function roleOf(el) {
  return (el.getAttribute("role") || "").trim();
}

function score(el, label) {
  const l = String(label || "").toLowerCase();
  let s = 0;
  if (/(close|dismiss|cancel|x\b)/.test(l)) s += 120;
  if (/(accept|agree|allow)/.test(l)) s += 70;
  if (/(reject|decline|no thanks)/.test(l)) s += 65;
  if (/(continue|next|confirm|save)/.test(l)) s += 55;
  if (/(address|addresses|cookie|consent)/.test(l)) s += 30;
  if (/(search|find|lookup)/.test(l)) s += 20;
  if (/(saved list|favorite|favourite|heart|save this store|add this store to your saved)/.test(l)) s -= 80;
  // A label containing a question keyword should reliably outrank a generic
  // <button> (+20) or <input> (+25).
  for (const t of WANT) {
    if (t && l.includes(t)) s += 40;
  }
  const tag = (el.tagName || "").toLowerCase();
  if (tag === "button") s += 20;
  if (tag === "a") s += 12;
  if (tag === "input" || tag === "textarea" || tag === "select") s += 25;
  try {
    if (el && el.isContentEditable) s += 25;
  } catch (e) {}
  const role = roleOf(el).toLowerCase();
  if (role === "button" || role === "link") s += 10;
  if (role === "textbox" || role === "combobox") s += 30;
  return s;
}

const selectors = [
  "button",
  "a[href]",
  "input",
  "textarea",
  "[contenteditable='true']",
  "[onclick]",
  "[role='button']",
  "[role='link']",
  "[role='textbox']",
  "[role='combobox']",
  "[aria-haspopup='listbox']",
  "[aria-label]",
  "[data-testid]",
  "[data-test]",
  "[data-qa]",
  // [tabindex='0'] only: [tabindex='-1'] is used for focus management on
  // backdrops, skip-links, and decorative containers — not real click targets.
  "[tabindex='0']",
];

const seen = new Set();
const targetSeen = new Set();
const cands = [];
const textCands = [];

function isInputLike(el) {
  if (!el) return false;
  const tag = (el.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select") return true;
  try {
    if (el.isContentEditable) return true;
  } catch (e) {}
  const role = roleOf(el).toLowerCase();
  if (role === "textbox" || role === "combobox") return true;
  return false;
}

function key10(x, y, w, h) {
  return `${Math.round(x)},${Math.round(y)},${Math.round(w)},${Math.round(h)}`;
}

function addCandidate(el, target) {
  if (!isVisible(target)) return;
  if (targetSeen.has(target)) return;
  targetSeen.add(target);

  const label = textOf(target) || textOf(el);
  const r = target.getBoundingClientRect();
  const l = Math.round(r.left);
  const t = Math.round(r.top);
  const w = Math.round(r.width);
  const h = Math.round(r.height);
  let x = Math.round(r.left + r.width / 2);
  let y = Math.round(r.top + r.height / 2);
  try {
    x = Math.max(l + 2, Math.min(l + Math.max(0, w - 2), x));
    y = Math.max(t + 2, Math.min(t + Math.max(0, h - 2), y));
  } catch (e) {}
  if (x < 0 || y < 0 || x > window.innerWidth || y > window.innerHeight) return;

  let sc = score(target, label);
  try {
    const area = Math.max(0, (r.width || 0) * (r.height || 0));
    const area_bonus = Math.min(70, Math.sqrt(area) / 10);
    sc += area_bonus;
  } catch (e) {}

  const item = { sc, label, x, y, l, t, w, h, tag: (target.tagName || "").toLowerCase(), role: roleOf(target) };
  cands.push(item);
  if (isInputLike(target) || isInputLike(el)) {
    textCands.push(item);
  }
}

for (const sel of selectors) {
  for (const el of Array.from(ROOT.querySelectorAll(sel))) {
    if (!el || seen.has(el)) continue;
    seen.add(el);
    let target = el;
    const inputLike = isInputLike(el);
    if (!inputLike) {
      try {
        const storeA = el.closest && el.closest("a[href*='/store/']");
        if (storeA) target = storeA;
      } catch (e) {}
      try {
        if (target === el) {
          const a = el.closest && el.closest("a[href]");
          if (a) target = a;
        }
      } catch (e) {}
    }
    addCandidate(el, target);
  }
}

// Cursor-pointer heuristic: modern React/Vue apps often use plain <div>/<span>
// for interactive controls (quantity +/-, toggle switches, icon buttons) with
// no semantic markup — only cursor:pointer CSS.  Scan leaf-ish elements that
// weren't caught by the selector pass above.
try {
  const POINTER_TAGS = new Set(["div", "span", "svg", "img", "i", "path", "li"]);
  const MAX_POINTER_AREA = 120 * 120;
  const allEls = ROOT.querySelectorAll("div, span, svg, img, i, li");
  for (const el of Array.from(allEls)) {
    if (!el || seen.has(el)) continue;
    if (!POINTER_TAGS.has((el.tagName || "").toLowerCase())) continue;
    const st = window.getComputedStyle(el);
    if (!st || st.cursor !== "pointer") continue;
    if (!isVisible(el)) continue;
    const r = el.getBoundingClientRect();
    const area = (r.width || 0) * (r.height || 0);
    if (area > MAX_POINTER_AREA || area < 36) continue;
    seen.add(el);
    addCandidate(el, el);
  }
} catch (e) {}

cands.sort((a, b) => b.sc - a.sc);
textCands.sort((a, b) => b.sc - a.sc);

const picked = [];
const used = new Set();

// 1) Always prioritise close/dismiss affordances (modal UX; keeps test determinism).
const closeLike = cands.filter((it) => /(close|dismiss|cancel|\bx\b|×)/.test(String(it && it.label ? it.label : "").toLowerCase()));
closeLike.sort((a, b) => b.sc - a.sc);
const RESERVED_CLOSE = Math.min(3, MAX);
for (const it of closeLike) {
  if (picked.length >= RESERVED_CLOSE) break;
  const k = key10(it.l, it.t, it.w, it.h);
  if (used.has(k)) continue;
  used.add(k);
  picked.push(it);
}

// 2) Reserve slots for textboxes so they cannot be crowded out.
const RESERVED_TEXT = Math.min(8, MAX - picked.length);
for (const it of textCands) {
  if (picked.length >= RESERVED_CLOSE + RESERVED_TEXT) break;
  const k = key10(it.l, it.t, it.w, it.h);
  if (used.has(k)) continue;
  used.add(k);
  picked.push(it);
}

// 3) Fill remaining slots by score.
for (const it of cands) {
  if (picked.length >= MAX) break;
  const k = key10(it.l, it.t, it.w, it.h);
  if (used.has(k)) continue;
  used.add(k);
  picked.push(it);
}

try {
  if (window.__emi_marks_overlay) {
    window.__emi_marks_overlay.remove();
    window.__emi_marks_overlay = null;
  }
} catch (e) {}

const overlay = document.createElement("div");
overlay.id = "__emi_marks_overlay";
overlay.style.position = "fixed";
overlay.style.left = "0";
overlay.style.top = "0";
overlay.style.width = "100vw";
overlay.style.height = "100vh";
overlay.style.zIndex = "2147483647";
overlay.style.pointerEvents = "none";
window.__emi_marks_overlay = overlay;
document.documentElement.appendChild(overlay);

const marks = [];
let id = 1;
for (const it of picked) {
  try {
    const box = document.createElement("div");
    box.style.position = "fixed";
    const inset = 1;
    box.style.left = `${it.l + inset}px`;
    box.style.top = `${it.t + inset}px`;
    box.style.width = `${Math.max(0, it.w - inset * 2)}px`;
    box.style.height = `${Math.max(0, it.h - inset * 2)}px`;
    box.style.border = "1px solid rgba(255,0,0,0.9)";
    box.style.borderRadius = "6px";
    box.style.boxSizing = "border-box";
    box.style.background = "rgba(255,0,0,0.02)";
    overlay.appendChild(box);
  } catch (e) {}

  const d = document.createElement("div");
  d.textContent = String(id);
  d.style.position = "fixed";
  const badgeX = Math.min(Math.max(it.l - 14, 0), window.innerWidth - 20);
  const badgeY = Math.min(Math.max(it.t - 14, 0), window.innerHeight - 20);
  d.style.left = `${badgeX}px`;
  d.style.top = `${badgeY}px`;
  d.style.transform = "translate(0, 0)";
  d.style.background = "rgba(255, 230, 0, 0.97)";
  d.style.color = "#000";
  d.style.border = "2px solid rgba(0,0,0,0.8)";
  d.style.borderRadius = "6px";
  d.style.padding = "1px 5px";
  d.style.font = "bold 14px/1.1 Arial, sans-serif";
  d.style.boxShadow = "0 2px 4px rgba(0,0,0,0.4)";
  overlay.appendChild(d);
  marks.push({
    id,
    x: it.x,
    y: it.y,
    label: it.label,
    rect: { l: it.l, t: it.t, w: it.w, h: it.h },
  });
  id += 1;
}

// Write full map to window for test/debug inspection; Python no longer reads it back.
try {
  window.__emi_marks_map = marks;
} catch (e) {}

return marks;
