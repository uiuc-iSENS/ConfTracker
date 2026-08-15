/* iSENS ConfTracker frontend — renders data.json into the deadline list
   and the 12-month "deadline spectrum" strip. No dependencies. */

(async function () {
  const data = await fetch("data.json?_=" + Date.now()).then((r) => r.json());

  // ---------- deadline parsing ----------

  function tzOffsetHours(tz) {
    if (!tz || /^AoE$/i.test(tz)) return -12; // Anywhere on Earth
    const m = /^UTC([+-]\d{1,2})(?::(\d{2}))?$/i.exec(tz.trim());
    if (!m) return -12;
    const sign = m[1].startsWith("-") ? -1 : 1;
    return Number(m[1]) + sign * (m[2] ? Number(m[2]) / 60 : 0);
  }

  function toEpoch(str, tz) {
    const m = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?/.exec(
      String(str).trim()
    );
    if (!m) return null;
    const [, y, mo, d, h = "23", mi = "59", s = "59"] = m;
    const utc = Date.UTC(+y, +mo - 1, +d, +h, +mi, +s);
    return utc - tzOffsetHours(tz) * 3600 * 1000; // wall clock in UTC±o → epoch
  }

  // All upcoming deadline events for one conference, soonest first.
  function upcomingEvents(conf) {
    const now = Date.now();
    const events = [];
    for (const cycle of conf.confs || []) {
      for (const entry of cycle.timeline || []) {
        const candidates = [
          ["Abstract", entry.abstract_deadline],
          ["Paper", entry.deadline],
        ];
        for (const [kind, value] of candidates) {
          if (!value) continue;
          const t = toEpoch(value, cycle.timezone);
          if (t && t > now) {
            events.push({
              t,
              kind,
              value,
              cycle,
              comment: entry.comment || null,
              track: entry.track || null, // null = main paper track
            });
          }
        }
      }
    }
    return events.sort((a, b) => a.t - b.t);
  }

  function urgency(t) {
    const days = (t - Date.now()) / 86400000;
    if (days < 7) return "u-urgent";
    if (days < 30) return "u-warn";
    return "u-ok";
  }

  // ---------- prepare model ----------

  const confs = data.conferences.map((c) => {
    const events = upcomingEvents(c);
    // Main paper track leads; workshops/posters/demos become secondary rows.
    const next = events.find((e) => !e.track) || events[0] || null;
    return {
      ...c,
      tier: (c.isens && c.isens.tier) || 3,
      tags: (c.isens && c.isens.tags) || [],
      next,
      extras: events.filter((e) => e !== next).slice(0, 4),
    };
  });

  const upcoming = confs.filter((c) => c.next).sort((a, b) => a.next.t - b.next.t);
  const awaiting = confs.filter((c) => !c.next);

  // ---------- header ----------

  const updatedEl = document.getElementById("updated");
  if (data.generated_at) {
    const d = new Date(data.generated_at);
    updatedEl.textContent = "data refreshed " + d.toLocaleString();
  }

  // ---------- filters ----------

  const state = { tier: "all", tags: new Set(), q: "" };

  const allTags = [...new Set(confs.flatMap((c) => c.tags))].sort();
  const chipBox = document.getElementById("tag-chips");
  for (const tag of allTags) {
    const b = document.createElement("button");
    b.className = "chip";
    b.textContent = tag;
    b.addEventListener("click", () => {
      state.tags.has(tag) ? state.tags.delete(tag) : state.tags.add(tag);
      b.classList.toggle("active");
      render();
    });
    chipBox.appendChild(b);
  }

  document.getElementById("tier-tabs").addEventListener("click", (e) => {
    const btn = e.target.closest(".tab");
    if (!btn) return;
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    btn.classList.add("active");
    state.tier = btn.dataset.tier;
    render();
  });

  document.getElementById("search").addEventListener("input", (e) => {
    state.q = e.target.value.trim().toLowerCase();
    render();
  });

  function visible(c) {
    if (state.tier !== "all" && String(c.tier) !== state.tier) return false;
    if (state.tags.size && ![...state.tags].some((t) => c.tags.includes(t))) return false;
    if (state.q) {
      const hay = (c.title + " " + (c.description || "")).toLowerCase();
      if (!hay.includes(state.q)) return false;
    }
    return true;
  }

  // ---------- rendering ----------

  function coreBadge(c) {
    const core = c.rank && c.rank.core;
    if (!core || core === "N") return "";
    const cls = core === "A*" ? "badge astar" : "badge";
    return `<span class="${cls}" title="CORE rank">CORE ${core}</span>`;
  }

  function confRow(c) {
    const li = document.createElement("li");
    li.className = "conf " + (c.next ? urgency(c.next.t) : "");
    li.id = "conf-" + c.title.toLowerCase();

    const link = c.next && c.next.cycle.link ? c.next.cycle.link : null;
    const acr = link
      ? `<a class="acr" href="${link}" rel="noopener">${c.title}</a>`
      : `<span class="acr">${c.title}</span>`;

    const venue = c.next
      ? `${c.next.cycle.date || "TBD"} · ${c.next.cycle.place || "TBD"}`
      : "";

    // Long extractor comments go to a tooltip, not the label.
    const brief = (s) => (s && s.length <= 48 ? s : null);

    const kindLabel = c.next
      ? [c.next.track, c.next.kind + " deadline", brief(c.next.comment)]
          .filter(Boolean)
          .join(" · ")
      : "";

    const when = c.next
      ? `<div class="dl-kind" title="${(c.next.comment || "").replace(/"/g, "&quot;")}">${kindLabel}</div>
         <div class="countdown ${urgency(c.next.t)}" data-t="${c.next.t}"></div>
         <p class="dl-date">${c.next.value} ${c.next.cycle.timezone || "AoE"}<br>
           ${new Date(c.next.t).toLocaleString()} local</p>`
      : `<span class="tbd">CFP not announced</span>`;

    const extras = c.extras.length
      ? `<p class="tracks">${c.extras
          .map((e) => {
            const label = [e.track || e.kind, brief(e.comment)]
              .filter(Boolean)
              .join(" · ");
            const d = new Date(e.t).toLocaleDateString(undefined, {
              month: "short",
              day: "numeric",
            });
            const days = Math.ceil((e.t - Date.now()) / 86400000);
            const tip = (e.comment || "").replace(/"/g, "&quot;");
            return `<span class="track ${urgency(e.t)}" title="${tip}">${label} · ${d} (${days}d)</span>`;
          })
          .join("")}</p>`
      : "";

    li.innerHTML = `
      <div class="conf-id">
        ${acr}
        ${coreBadge(c)}
        <span class="tier-dot">tier ${c.tier}</span>
      </div>
      <div class="conf-body">
        <p class="full-name">${c.description || ""}</p>
        ${venue ? `<p class="venue">${venue}</p>` : ""}
        ${extras}
      </div>
      <div class="conf-when">${when}</div>`;
    return li;
  }

  const upcomingBox = document.getElementById("upcoming");
  const awaitingBox = document.getElementById("awaiting");
  const awaitingLabel = document.getElementById("awaiting-label");

  function render() {
    upcomingBox.textContent = "";
    awaitingBox.textContent = "";

    const up = upcoming.filter(visible);
    const wait = awaiting.filter(visible);

    if (!up.length) {
      upcomingBox.innerHTML = `<li class="empty">No upcoming deadlines match the current filters.</li>`;
    }
    up.forEach((c) => upcomingBox.appendChild(confRow(c)));
    wait.forEach((c) => awaitingBox.appendChild(confRow(c)));
    awaitingLabel.hidden = !wait.length;
    renderSpectrum(up);
    tick();
  }

  // ---------- spectrum strip ----------

  function renderSpectrum(list) {
    const axis = document.getElementById("spectrum-axis");
    axis.textContent = "";
    const now = Date.now();
    const span = 365 * 86400000;

    // month ticks
    const cursor = new Date();
    cursor.setDate(1);
    for (let i = 1; i <= 12; i++) {
      cursor.setMonth(cursor.getMonth() + 1);
      const x = ((cursor.getTime() - now) / span) * 100;
      if (x < 0 || x > 99) continue;
      const tick = document.createElement("div");
      tick.className = "month-tick";
      tick.style.left = x + "%";
      tick.textContent = cursor.toLocaleString("en-US", { month: "short" });
      axis.appendChild(tick);
    }

    list.forEach((c, i) => {
      const x = ((c.next.t - now) / span) * 100;
      if (x > 99) return;
      const a = document.createElement("a");
      a.className = `dl-tick ${urgency(c.next.t)} ${i % 2 ? "row-odd" : ""}`;
      a.style.left = Math.max(x, 0.5) + "%";
      a.href = "#conf-" + c.title.toLowerCase();
      a.innerHTML = `<span class="lbl">${c.title}</span><span class="stem"></span><span class="dot"></span>`;
      axis.appendChild(a);
    });
  }

  // ---------- live countdown ----------

  function fmt(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    if (d > 0) return `${d}d ${String(h).padStart(2, "0")}h ${String(m).padStart(2, "0")}m`;
    return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  }

  function tick() {
    const now = Date.now();
    document.querySelectorAll(".countdown").forEach((el) => {
      el.textContent = fmt(Number(el.dataset.t) - now);
    });
  }

  setInterval(tick, 1000);
  render();
})().catch((err) => {
  document.getElementById("upcoming").innerHTML =
    `<li class="empty">Could not load data.json — run the scraper pipeline first.</li>`;
  console.error(err);
});
