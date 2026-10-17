/* xdl local UI behavior — vanilla JS + EventSource SSE (no framework). */
"use strict";

const $ = (sel) => document.querySelector(sel);

let busy = false;
let currentJobId = null;
let source = null;

function msg(text, cls) {
  const box = $("#msg");
  box.className = "msg " + cls;
  box.textContent = text;
  box.classList.remove("hidden");
}

function parseUrls() {
  return $("#urls").value.split("\n").map((s) => s.trim()).filter(Boolean);
}

async function postJSON(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || resp.statusText || String(resp.status));
  }
  return data;
}

// ---------------------------------------------------------------- options row

$("#concurrency").addEventListener("input", (e) => {
  $("#concurrency-out").value = e.target.value;
});

// ---------------------------------------------------------------- Flow A

$("#download").addEventListener("click", async () => {
  if (busy) { msg("a batch is already running", "err"); return; }
  const urls = parseUrls();
  if (!urls.length) { msg("paste at least one URL", "err"); return; }
  busy = true;
  try {
    const data = await postJSON("/jobs", {
      urls: $("#urls").value,
      options: {
        quality: $("#quality").value === "best" ? "best" : $("#quality").value,
        output: $("#output").value.trim() || null,
        concurrency: Number($("#concurrency").value),
      },
    });
    msg(`batch #${data.job_id} started (${data.accepted} items)`, "info");
    watchJob(String(data.job_id));
  } catch (err) {
    msg(String(err.message || err), "err");
  } finally {
    busy = false;
  }
});

// ---------------------------------------------------------------- Flow B (analyze)

$("#analyze").addEventListener("click", async () => {
  const urls = parseUrls();
  if (!urls.length) { msg("paste at least one URL", "err"); return; }
  try {
    const data = await postJSON("/analyze", { urls: $("#urls").value });
    renderCards(data.items || []);
  } catch (err) {
    msg(String(err.message || err), "err");
  }
});

function renderCards(items) {
  const host = $("#cards");
  host.classList.remove("hidden");
  host.textContent = "";
  for (const item of items) {
    const card = document.createElement("div");
    card.className = "card";
    const img = document.createElement("img");
    img.src = item.poster || "/static/no-thumb.svg";
    img.alt = "";
    const meta = document.createElement("div");
    meta.className = "meta";
    const handle = document.createElement("div");
    handle.className = "handle";
    handle.textContent = "@" + (item.author || "?") + " · " + item.tweet_id;
    const snippet = document.createElement("div");
    snippet.className = "snippet";
    snippet.textContent = item.text || "";
    const pills = document.createElement("div");
    pills.className = "pills";
    const pill = document.createElement("button");
    pill.className = "pill";
    pill.textContent = "download";
    pill.addEventListener("click", async () => {
      try {
        const data = await postJSON("/jobs", { urls: [item.input] });
        msg(`batch #${data.job_id} started`, "info");
        watchJob(String(data.job_id));
      } catch (err) { msg(String(err.message || err), "err"); }
    });
    pills.appendChild(pill);
    meta.append(handle, snippet, pills);
    card.append(img, meta);
    host.appendChild(card);
  }
}

// ---------------------------------------------------------------- SSE job view

function watchJob(jobId) {
  currentJobId = jobId;
  $("#batch").classList.remove("hidden");
  $("#batch-title").textContent = "Batch #" + jobId;
  $("#batch-counts").textContent = "running…";
  $("#batch-items").textContent = "";
  $("#retry").classList.add("hidden");
  if (source) source.close();
  source = new EventSource("/jobs/" + jobId + "/events");
  source.onmessage = (e) => { try { applyEvent(JSON.parse(e.data)); } catch (err) { /* ignore partial */ } };
  source.onerror = async () => {
    source.close();
    try {
      const detail = await (await fetch("/jobs/" + jobId)).json();
      renderCounts(detail);
    } catch (err) { /* page gone? */ }
  };
  refreshHistory();
}

function itemRow(key) {
  let row = document.getElementById(key);
  if (row) {
    return {
      row,
      name: row.querySelector(".name"),
      author: row.querySelector(".author"),
      chip: row.querySelector(".chip"),
      bar: row.querySelector(".progress > div"),
      btn: row.querySelector(".reveal"),
    };
  }
  row = document.createElement("div");
  row.id = key;
  row.className = "item";
  row.innerHTML =
    '<span class="name"></span>' +
    '<span class="author"></span>' +
    '<span class="chip">queued</span>' +
    '<span class="progress"><div></div></span>' +
    '<button class="reveal hidden">reveal</button>';
  row.querySelector(".reveal").addEventListener("click", async (e) => {
    const tweetId = row.dataset.tweet;
    try { await postJSON("/items/" + tweetId + "/reveal", {}); }
    catch (err) { msg(String(err.message || err), "err"); }
  });
  $("#batch-items").appendChild(row);
  return {
    row,
    name: row.querySelector(".name"),
    author: row.querySelector(".author"),
    chip: row.querySelector(".chip"),
    bar: row.querySelector(".progress > div"),
    btn: row.querySelector(".reveal"),
  };
}

let itemRowCache = null;

function applyEvent(ev) {
  switch (ev.kind) {
    case "batch_start": {
      break;
    }
    case "resolve_start":
    case "resolve_done": {
      itemRowCache = itemRow(keyFor(ev));
      if (ev.kind === "resolve_done") itemRowCache.chip.textContent = "resolved";
      break;
    }
    case "download_start": {
      const row = itemRow(keyFor(ev));
      itemRowCache = row;
      row.row.dataset.tweet = ev.tweet_id;
      row.chip.textContent = "downloading 0%";
      if (ev.path) row.name.textContent = ev.path;
      break;
    }
    case "download_progress": {
      const row = itemRow(keyFor(ev));
      const pct = ev.total ? Math.round((100 * ev.offset) / ev.total) : null;
      if (pct !== null) {
        row.bar.style.width = pct + "%";
        row.chip.textContent = "downloading " + pct + "%";
      } else {
        row.chip.textContent = "downloading " + humanBytes(ev.offset);
      }
      break;
    }
    case "download_done": {
      const row = itemRow(keyFor(ev));
      if (ev.status === "ok") {
        row.bar.style.width = "100%";
        row.chip.textContent = "done " + (ev.detail || "");
        row.chip.className = "chip chip-ok";
        row.row.dataset.tweet = ev.tweet_id;
        row.btn.classList.remove("hidden");
      } else {
        row.chip.textContent = ev.status === "skipped" ? "skipped" : "failed: " + (ev.detail || "");
        row.chip.className = "chip " + (ev.status === "skipped" ? "" : "chip-failed");
      }
      break;
    }
    case "item_done": {
      renderCountsFromEvent(ev);
      if (ev.status === "failed") $("#retry").classList.remove("hidden");
      break;
    }
    case "batch_done": {
      const counts = ev.counters || {};
      $("#batch-counts").textContent =
        `ok ${counts.ok || 0} · skip ${counts.skipped || 0} · fail ${counts.failed || 0}`;
      if (source) { source.close(); source = null; }
      refreshHistory();
      break;
    }
    default: break;
  }
}

function keyFor(ev) {
  return "i-" + (ev.tweet_id || "?") + "-" + (ev.media_index || 0);
}

function humanBytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0, v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return Math.round(v) + " " + units[i];
}

function renderCounts(detail) {
  const c = detail.counts || {};
  $("#batch-counts").textContent =
    `ok ${c.ok || 0} · skip ${c.skipped || 0} · fail ${c.failed || 0}`;
}

function renderCountsFromEvent(ev) {
  // cheap incremental: chip statuses already shown per row; the batch_done
  // event is authoritative — item_done only refreshes the title state chip.
  $("#running-chip").textContent = "batch running…";
}

// ---------------------------------------------------------------- history

async function refreshHistory() {
  try {
    const data = await (await fetch("/jobs")).json();
    const rows = $("#history-rows");
    rows.textContent = "";
    for (const job of data.jobs || []) {
      const tr = document.createElement("tr");
      tr.dataset.job = job.job_id;
      tr.className = "history-row";
      const when = new Date(Number(job.started_at) * 1000 || Date.now());
      tr.innerHTML =
        `<td>#${job.job_id}</td>` +
        `<td>${when.toLocaleTimeString()}</td>` +
        `<td>${job.counts
            ? `${job.counts.ok || 0} ok · ${job.counts.skipped || 0} skip · ${job.counts.failed || 0} fail`
            : `${job.total ?? job.inputs ?? "?"} item(s) · ${job.origin || "cli"}`}</td>` +
        `<td><span class="chip chip-${job.status}">${job.status}</span></td>`;
      tr.addEventListener("click", () => showDetail(String(job.job_id)));
      rows.appendChild(tr);
    }
  } catch (err) { /* ignore */ }
}

$("#retry").addEventListener("click", async () => {
  if (!currentJobId) return;
  try {
    const data = await postJSON("/jobs/" + currentJobId + "/retry", {});
    if (data.job_id) { msg(`retry batch #${data.job_id} started`, "info"); watchJob(String(data.job_id)); }
    else msg(data.detail || "nothing to retry", "info");
  } catch (err) { msg(String(err.message || err), "err"); }
});

async function showDetail(jobId) {
  const detail = await (await fetch("/jobs/" + jobId)).json();
  $("#detail").classList.remove("hidden");
  $("#detail-title").textContent = "Batch #" + jobId;
  const rows = $("#detail-rows");
  rows.textContent = "";
  for (const item of detail.items || []) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${item.tweet_id || ""}</td>` +
      `<td>${item.author || ""}</td>` +
      `<td><span class="chip chip-${item.status}">${item.status}</span></td>` +
      `<td>${(item.files || [])[0] || item.detail || item.error || ""}</td>` +
      `<td>—</td>`;
    tr.querySelector(".chip").className = "chip chip-" + item.status;
    rows.appendChild(tr);
  }
}

refreshHistory();
