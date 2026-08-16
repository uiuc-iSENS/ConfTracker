/* iSENS ConfTracker frontend — renders data.json into the deadline list
   and the "deadline spectrum" strip. No dependencies. */

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

  function dateInfo(raw, tz) {
    if (!raw) return null;
    const t = toEpoch(raw, tz);
    if (!t) return null;
    return { t, raw, past: t <= Date.now() };
  }

  // Upcoming submission rounds for one conference, soonest first. A round is
  // one timeline entry, so its abstract and paper deadlines stay together --
  // they are two dates for the same submission, not two separate events.
  function upcomingRounds(conf) {
    const rounds = [];
    for (const cycle of conf.confs || []) {
      for (const entry of cycle.timeline || []) {
        const abstract = dateInfo(entry.abstract_deadline, cycle.timezone);
        const paper = dateInfo(entry.deadline, cycle.timezone);
        // Keep the round while either date is still ahead; a passed abstract
        // is still worth showing next to a live paper deadline, since most
        // venues will not accept a paper whose abstract was never registered.
        const live = [abstract, paper].filter((d) => d && !d.past);
        if (!live.length) continue;
        rounds.push({
          cycle,
          abstract,
          paper,
          comment: entry.comment || null,
          track: entry.track || null, // null = main paper track
          url: entry.url || null, // a workshop's own CFP, when we found one
          t: Math.min(...live.map((d) => d.t)),
        });
      }
    }
    return rounds.sort((a, b) => a.t - b.t);
  }

  // The next date actually facing you in a round, and which kind it is.
  function soonest(round) {
    const live = [
      ["Paper", round.paper],
      ["Abstract", round.abstract],
    ].filter(([, d]) => d && !d.past);
    if (!live.length) return null;
    live.sort((a, b) => a[1].t - b[1].t);
    return { kind: live[0][0], t: live[0][1].t, raw: live[0][1].raw };
  }

  // Label a deadline by the date the venue states, not the reader's local
  // rendering of it: an AoE deadline converted to local time lands a day
  // later, so "Feb 1" would show as "Feb 2" and contradict every CFP.
  function statedDay(raw) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(raw));
    if (!m) return "";
    return new Date(+m[1], +m[2] - 1, +m[3]).toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
    });
  }

  function urgency(t) {
    const days = (t - Date.now()) / 86400000;
    if (days < 7) return "u-urgent";
    if (days < 30) return "u-warn";
    return "u-ok";
  }

  // ---------- prepare model ----------

  const confs = data.conferences.map((c) => {
    const rounds = upcomingRounds(c);
    // Main paper track leads; workshops/posters/demos become secondary rows.
    const main = rounds.find((r) => !r.track) || rounds[0] || null;
    return {
      ...c,
      tier: (c.isens && c.isens.tier) || 3,
      tags: (c.isens && c.isens.tags) || [],
      main,
      soon: main ? soonest(main) : null,
      // Every other round gets its own sub-row: workshops, posters, demos
      // and extra submission cycles each carry their own deadline, so they
      // are listed rather than summarised.
      extras: rounds.filter((r) => r !== main),
    };
  });

  const upcoming = confs.filter((c) => c.main).sort((a, b) => a.soon.t - b.soon.t);
  const awaiting = confs.filter((c) => !c.main);

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

  // ---------- reminder signup ----------

  // There is no backend to POST a signup to — the site is static files on
  // GitHub Pages — so a subscription is a message whose *title* is the
  // command, opened prefilled here and picked up by the lab machine.
  // Two channels, same command language: a GitHub issue, or an email.
  const reminders =
    data.reminders && (data.reminders.repo || data.reminders.address)
      ? data.reminders
      : null;
  const viaIssues = reminders && reminders.mode === "issues";

  const HOW_TO_EDIT =
    "You can edit the title first: add more venue names, or\n" +
    "  tracks:all   also remind me about workshops, posters and demos\n" +
    "  days:30,7,1  choose your own reminder lead times\n\n" +
    "Later on, 'unsubscribe' stops everything and 'list' shows what you\n" +
    "are signed up for.\n";

  function signupUrl(command) {
    if (viaIssues) {
      // The address goes in the body, where the lab machine reads it; the
      // title is the command itself.
      const body =
        "Email: \n\n" +
        "(put your address on the line above — it is where reminders go)\n\n" +
        HOW_TO_EDIT;
      return (
        "https://github.com/" +
        reminders.repo +
        "/issues/new?labels=" +
        encodeURIComponent(reminders.label || "reminder-signup") +
        "&title=" +
        encodeURIComponent(command) +
        "&body=" +
        encodeURIComponent(body)
      );
    }
    return (
      "mailto:" +
      encodeURIComponent(reminders.address) +
      "?subject=" +
      encodeURIComponent(command) +
      "&body=" +
      encodeURIComponent(
        "Send this message as it is — the subject line is the whole request.\n\n" +
          HOW_TO_EDIT
      )
    );
  }

  function remindButton(c) {
    if (!reminders) return "";
    // The empty span is a full-width flex item: it forces the link onto its
    // own line without making the link itself full width, which would drag
    // its underline across the whole column.
    return `<span class="id-break"></span>
      <a class="remind" href="${signupUrl("subscribe " + venueKey(c.title))}"
        ${viaIssues ? 'target="_blank" rel="noopener"' : ""}
        title="Email reminders before ${attr(c.title)} deadlines">remind me</a>`;
  }

  // What the filter bar is currently showing, as a subscription command.
  // A tier and a tag together are an AND on screen but an OR in a command,
  // so that case is spelled out venue by venue rather than quietly widened.
  function commandForFilters(shown) {
    const tierOnly = state.tier !== "all" && !state.tags.size && !state.q;
    const tagsOnly = state.tier === "all" && state.tags.size && !state.q;
    if (state.tier === "all" && !state.tags.size && !state.q) return "subscribe all";
    if (tierOnly) return `subscribe tier${state.tier}`;
    if (tagsOnly) return "subscribe " + [...state.tags].map((t) => "tag:" + t).join(" ");
    return "subscribe " + shown.map((c) => venueKey(c.title)).join(" ");
  }

  const remBox = document.getElementById("reminders");
  const remBody = document.getElementById("rem-body");
  const remSub = document.getElementById("rem-sub");

  function renderReminders(shown) {
    if (!reminders) return;
    remBox.hidden = false;
    const leads = (reminders.lead_days || [14, 7, 3, 1]).join(", ");
    const who =
      reminders.domains && reminders.domains.length
        ? `Open to ${reminders.domains.join(", ")} addresses.`
        : "";
    remSub.textContent = `mailed ${leads} days before each deadline`;

    const count = shown.length;
    // With nothing on screen there is nothing to subscribe to, so the
    // filter-shaped button falls back to the whole list rather than mailing
    // an empty command.
    const filterCmd = count ? commandForFilters(shown) : "subscribe all";
    const filterLabel =
      filterCmd === "subscribe all"
        ? "everything we track"
        : `what's shown now (${count} venue${count === 1 ? "" : "s"})`;

    const blank = viaIssues ? ' target="_blank" rel="noopener"' : "";
    const lede = viaIssues
      ? `Get an email before the deadlines you care about. The button opens a
         prefilled issue in our signup repo — the title is the request, and you
         add the address the reminders should go to. It is picked up within
         half an hour.`
      : `Get an email before the deadlines you care about. There is no form and
         no account: the button sends a one-line message from your own mailbox,
         which is what tells us the address is yours.`;
    const where = viaIssues
      ? `Or open an issue in
         <a href="https://github.com/${reminders.repo}/issues"${blank}>${reminders.repo}</a>
         yourself. The title is the command:`
      : `Or mail <a href="${signupUrl("help")}">${reminders.address}</a> directly.
         Put the command in the subject line:`;

    remBody.innerHTML = `
      <p class="rem-lede">${lede}</p>
      <p class="rem-actions">
        <a class="rem-btn" href="${signupUrl(filterCmd)}"${blank}>Remind me about ${filterLabel}</a>
        <a class="rem-btn ghost" href="${signupUrl("subscribe all tracks:all")}"${blank}>Everything, workshops and posters included</a>
        <a class="rem-btn ghost" href="${signupUrl("list")}"${blank}>What am I signed up for?</a>
      </p>
      <p class="rem-note">${where}</p>
      <ul class="rem-syntax">
        <li><code>subscribe mobicom sensys</code> one or more venues</li>
        <li><code>subscribe tier1</code> · <code>subscribe tag:sensing</code> by tier or topic</li>
        <li><code>subscribe all tracks:all</code> include workshop, poster and demo calls</li>
        <li><code>subscribe all days:30,7,1</code> your own lead times</li>
        <li><code>pause</code> · <code>resume</code> · <code>unsubscribe</code> · <code>list</code></li>
      </ul>
      <p class="rem-note">${who} Reminders use the dates on this page, which are
        gathered automatically — treat them as a nudge to go and check the
        official CFP, not as the authority.</p>`;
  }

  // ---------- rendering ----------

  function coreBadge(c) {
    const core = c.rank && c.rank.core;
    if (!core || core === "N") return "";
    const cls = core === "A*" ? "badge astar" : "badge";
    return `<span class="${cls}" title="CORE rank">CORE ${core}</span>`;
  }

  // Long extractor comments go to a tooltip, not the label.
  const brief = (s) => (s && s.length <= 48 ? s : null);
  // "UbiComp/ISWC" -> "ubicomp-iswc": a raw title breaks both the id and the
  // spectrum's "#conf-…" anchor as soon as it contains a slash or a space.
  // The same key names a venue in a reminder command, so the two must agree.
  const venueKey = (title) =>
    title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  const slug = (title) => "conf-" + venueKey(title);
  const attr = (s) => String(s || "").replace(/"/g, "&quot;");
  // "2026-09-02 23:59:59" -> "2026-09-02 23:59"; a bare date is left alone.
  const fmtStamp = (raw) => String(raw).replace(/:\d{2}$/, "");

  // One deadline. `primary` is the paper deadline, which gets the weight:
  // it is the date that decides whether you submit at all.
  function dlCell(label, info, tz, primary) {
    const cls = "dl-cell" + (primary ? " primary" : "");
    const stamp = `<div class="dl-date">${fmtStamp(info.raw)} ${tz}</div>`;
    if (info.past) {
      return `<div class="${cls} closed" title="${attr(new Date(info.t).toLocaleString())} local">
          <div class="dl-label">${label}</div>
          <div class="dl-closed">closed</div>
          ${stamp}
        </div>`;
    }
    return `<div class="${cls} ${urgency(info.t)}" title="${attr(new Date(info.t).toLocaleString())} local">
        <div class="dl-label">${label}</div>
        <div class="countdown ${urgency(info.t)}" data-t="${info.t}"></div>
        ${stamp}
      </div>`;
  }

  // Abstract and paper side by side, in time order, paper emphasised. On a
  // workshop/poster/demo round the full-submission deadline is that track's,
  // so it takes the track's name rather than a misleading "Paper".
  function deadlinePair(r) {
    const tz = r.cycle.timezone || "AoE";
    const mainLabel = r.track || "Paper";
    const cells = [];
    if (r.abstract) cells.push(dlCell("Abstract", r.abstract, tz, false));
    if (r.paper) {
      cells.push(dlCell(mainLabel, r.paper, tz, true));
    } else {
      // An announced abstract deadline with no paper date yet is common and
      // worth stating outright, rather than leaving a gap.
      cells.push(`<div class="dl-cell primary pending">
          <div class="dl-label">${mainLabel}</div>
          <div class="dl-pending">not announced</div>
        </div>`);
    }
    const head = brief(r.comment);
    return `${head ? `<div class="dl-kind" title="${attr(r.comment)}">${head}</div>` : ""}
      <div class="dl-pair">${cells.join("")}</div>`;
  }

  // One non-headline round: a named workshop, a poster/demo track, or a
  // later submission cycle. Each keeps its own deadline, which is the point
  // of listing them separately -- two workshops at the same conference
  // routinely close weeks apart.
  function subtrackRow(r) {
    const s = soonest(r);
    if (!s) return "";
    const days = Math.ceil((s.t - Date.now()) / 86400000);
    const name = r.comment || "";
    const shown = name.length > 62 ? name.slice(0, 59).trimEnd() + "…" : name;

    const dates = [];
    if (r.abstract) {
      dates.push(
        `${r.abstract.past ? "abstract closed" : "abstract"} ${statedDay(r.abstract.raw)}`
      );
    }
    if (r.paper) dates.push(statedDay(r.paper.raw));

    // A workshop with its own site gets linked to it: that page is where the
    // deadline came from and where the submission instructions live.
    const label = r.url
      ? `<a href="${attr(r.url)}" rel="noopener">${shown}</a>`
      : shown;

    return `<li class="subtrack ${urgency(s.t)}">
        <span class="st-track">${r.track || "Paper"}</span>
        <span class="st-name" title="${attr(name)}">${label}</span>
        <span class="st-when">${dates.join(" · ")}<span class="st-days">${days}d</span></span>
      </li>`;
  }

  function confRow(c) {
    const li = document.createElement("li");
    li.className = "conf " + (c.soon ? urgency(c.soon.t) : "");
    li.id = slug(c.title);

    const link = c.main && c.main.cycle.link ? c.main.cycle.link : null;
    const acr = link
      ? `<a class="acr" href="${link}" rel="noopener">${c.title}</a>`
      : `<span class="acr">${c.title}</span>`;

    // A recurring-deadline venue has no edition info to show; "TBD · TBD" is
    // noise, so drop the line entirely when neither half is known.
    const cDate = c.main && c.main.cycle.date;
    const cPlace = c.main && c.main.cycle.place;
    const known = (v) => v && v !== "TBD";
    const venue =
      known(cDate) || known(cPlace)
        ? `${cDate || "TBD"} · ${cPlace || "TBD"}`
        : "";

    const when = c.main
      ? deadlinePair(c.main)
      : `<span class="tbd">CFP not announced</span>`;

    const extras = c.extras.length
      ? `<ul class="subtracks">
          <li class="subtracks-head">Also open</li>
          ${c.extras.map(subtrackRow).join("")}
        </ul>`
      : "";

    li.innerHTML = `
      <div class="conf-id">
        ${acr}
        ${coreBadge(c)}
        <span class="tier-dot">tier ${c.tier}</span>
        ${remindButton(c)}
      </div>
      <div class="conf-body">
        <p class="full-name">${c.description || ""}</p>
        ${venue ? `<p class="venue">${venue}</p>` : ""}
      </div>
      <div class="conf-when">${when}</div>
      ${extras}`;
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
    // The signup command follows the filters, so it is rebuilt with them.
    renderReminders(up.concat(wait));
    tick();
  }

  // ---------- spectrum strip ----------

  const MONTH_MS = 30.44 * 86400000;

  function renderSpectrum(list) {
    const axis = document.getElementById("spectrum-axis");
    const note = document.getElementById("spectrum-note");
    axis.textContent = "";

    const now = Date.now();
    const points = list.map((c) => ({ c, s: c.soon })).filter((p) => p.s);
    if (!points.length) {
      note.textContent = "no upcoming deadlines";
      return;
    }

    // Span what the data actually covers, not a fixed year: conferences
    // announce roughly a cycle ahead, so a hard 12-month axis leaves most of
    // the strip empty and squeezes every real deadline into the left edge.
    // Round up to the start of the month after the furthest deadline, with a
    // floor so one imminent deadline cannot zoom the axis down to days.
    const far = Math.max(...points.map((p) => p.s.t));
    const end = new Date(far);
    end.setDate(1);
    end.setHours(0, 0, 0, 0);
    end.setMonth(end.getMonth() + 1);
    const span = Math.min(
      Math.max(end.getTime() - now, 70 * 86400000),
      400 * 86400000
    );

    const months = Math.max(1, Math.round(span / MONTH_MS));
    const through = new Date(now + span).toLocaleString("en-US", {
      month: "short",
      year: "numeric",
    });
    note.textContent = `next ${months} month${months === 1 ? "" : "s"} · through ${through}`;

    // month ticks, as many as the span actually holds
    const cursor = new Date();
    cursor.setDate(1);
    cursor.setHours(0, 0, 0, 0);
    while (true) {
      cursor.setMonth(cursor.getMonth() + 1);
      const x = ((cursor.getTime() - now) / span) * 100;
      if (x > 99) break;
      if (x < 0) continue;
      const tick = document.createElement("div");
      tick.className = "month-tick";
      tick.style.left = x + "%";
      tick.textContent = cursor.toLocaleString("en-US", {
        month: "short",
        ...(cursor.getMonth() === 0 ? { year: "2-digit" } : {}),
      });
      axis.appendChild(tick);
    }

    points.forEach((p, i) => {
      const x = ((p.s.t - now) / span) * 100;
      if (x > 99) return;
      const a = document.createElement("a");
      a.className = `dl-tick ${urgency(p.s.t)} ${i % 2 ? "row-odd" : ""}`;
      a.style.left = Math.max(x, 0.5) + "%";
      a.href = "#" + slug(p.c.title);
      a.title = `${p.c.title} — ${p.s.kind.toLowerCase()} deadline ${statedDay(p.s.raw)}`;
      // Hollow dot = abstract deadline, solid = paper deadline.
      a.innerHTML =
        `<span class="lbl">${p.c.title}</span><span class="stem"></span>` +
        `<span class="dot${p.s.kind === "Abstract" ? " hollow" : ""}"></span>`;
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
