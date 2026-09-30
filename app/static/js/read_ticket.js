"use strict";
// A ticket's reading page: the whole message and everything it rides on. Answers stay in the popup.
const BASE = window.SCRIPT_NAME || "";
const $ = (s) => document.querySelector(s);
const el = (tag, cls, txt) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
};
const when = (iso) => (iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "");

function section(title) {
  const s = el("section", "rt-section");
  s.appendChild(el("h2", null, title));
  return s;
}

function kv(pairs) {
  const dl = el("dl", "rt-kv");
  pairs.filter(([, v]) => v && !(Array.isArray(v) && !v.length)).forEach(([k, v]) => {
    dl.appendChild(el("dt", null, k));
    dl.appendChild(el("dd", null, Array.isArray(v) ? v.map((x) => "• " + x).join("\n") : String(v)));
  });
  return dl;
}

function block(title, meta, body) {
  const b = el("div", "rt-block");
  if (title) b.appendChild(el("h3", null, title));
  if (meta) b.appendChild(el("div", "rt-meta", meta));
  if (body instanceof Node) b.appendChild(body);
  else if (body) b.appendChild(el("div", "rt-body", body));
  return b;
}

function render(data) {
  const t = data.ticket;
  const a = $("#rt-article");
  document.title = t.title || "Ticket";
  a.appendChild(el("div", "rt-kicker", `${when(t.created_at)} · ${t.type}`));
  a.appendChild(el("h1", "rt-title", t.title || "Ticket"));
  a.appendChild(el("div", "rt-message", t.message || ""));
  if (t.responded_at) {
    a.appendChild(el("div", "rt-answer",
      `You answered ${when(t.responded_at)}: ${t.user_action || ""}${t.user_text ? " — " + t.user_text : ""}`));
  } else {
    a.appendChild(el("div", "rt-note", (t.choices && t.choices.length ? `Choices: ${t.choices.join(" · ")}. ` : "") +
      "Answer in the ticket popup."));
  }

  if (data.work || data.question) {
    const s = section("Why you are getting this");
    if (data.work) {
      s.appendChild(kv([["the goal", data.work.objective], ["why", data.work.why], ["this step", data.work.task],
        ["done when", data.work.success_criteria]]));
    }
    if (data.question) s.appendChild(kv([["question", data.question.text], ["status", data.question.status]]));
    a.appendChild(s);
  }

  data.concerns.forEach((c) => {
    const s = section("The concern");
    const b = c.brief || {};
    s.appendChild(block(c.title, `${c.status}${c.subject ? " · about " + c.subject : ""}`, kv([
      ["done when", c.done_when], ["you asked", c.owner_words && `"${c.owner_words}"`],
      ["what", b.what], ["why it matters", b.why_it_matters],
      ["known", (b.known || []).map((f) => f.fact)], ["tried", b.tried], ["your wishes", b.owner_wishes],
      ["depends on it", b.depends_on_it], ["open questions", b.open_questions], ["recommendation", b.recommendation],
    ])));
    if (c.earlier && c.earlier.length) {
      const list = el("ul", "rt-list");
      c.earlier.forEach((w) => list.appendChild(el("li", null,
        `${when(w.at)} · ${w.outcome}${w.response && Object.keys(w.response).length ? " · you said: " + JSON.stringify(w.response) : ""}`)));
      s.appendChild(block("Earlier on this", null, list));
    }
    a.appendChild(s);
  });

  if (data.sources.length) {
    const s = section("What came with it");
    data.sources.forEach((src) => {
      if (src.kind === "email") {
        const b = block(src.subject || "(no subject)", `email from ${src.sender} · ${when(src.at)}`, src.body || "");
        if (src.thread && src.thread.length) {
          const d = el("details");
          d.appendChild(el("summary", null, `the rest of the thread (${src.thread.length})`));
          src.thread.forEach((m) => {
            const t = el("div", "rt-thread");
            t.appendChild(el("div", "rt-meta", `${m.sender} · ${when(m.at)} · ${m.subject || ""}`));
            t.appendChild(el("div", "rt-body", m.body || ""));
            d.appendChild(t);
          });
          b.appendChild(d);
        }
        s.appendChild(b);
      } else if (src.kind === "chat") {
        s.appendChild(block(null, `${src.speaker} in ${src.room} · ${when(src.at)}`, src.body || ""));
      } else {
        s.appendChild(block(src.title || src.ref, `${src.kind} · ${when(src.at)}`, src.body || ""));
      }
    });
    a.appendChild(s);
  }

  if (data.entities.length) {
    const s = section("Who and what it names");
    s.appendChild(kv(data.entities.map((e) => [e.label, e.description || "(no description recorded)"])));
    a.appendChild(s);
  }

  if (data.related_work.length) {
    const s = section("Earlier work on this");
    data.related_work.forEach((w) => {
      const outcomes = (w.outcomes || []).map((f) => f.outcome).filter(Boolean);
      s.appendChild(block(w.title, `${w.status} · ${w.created}${w.ended_because ? " · ended: " + w.ended_because : ""}`,
        outcomes.join("\n\n")));
    });
    a.appendChild(s);
  }
}

(async () => {
  const r = await fetch(BASE + "/api/read/" + encodeURIComponent(window.TICKET_ID));
  if (!r.ok) { $("#rt-status").textContent = r.status === 404 ? "This ticket does not exist." : "Could not load the ticket."; return; }
  render(await r.json());
  $("#rt-status").classList.add("hidden");
  $("#rt-article").classList.remove("hidden");
})();
