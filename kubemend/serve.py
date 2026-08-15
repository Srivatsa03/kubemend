"""A local console over the incident log.

`kubemend log` answers a question you already knew to ask. This is for the other
mode: scanning a week of automated changes to production and stopping at the one
that looks wrong.

Three constraints shaped it.

**It is read-only, and that is structural rather than a policy.** There are no
write routes at all, so nothing reachable from a browser can touch a cluster or
a repository. The only write surface in this project is still a git commit, and
a dashboard that could trigger remediation would quietly make that untrue.

**It binds to localhost.** The journal names workloads, images and namespaces —
an inventory of what is fragile in your cluster. That is not something to serve
on 0.0.0.0 by default, and `--host` exists for people who mean it.

**It is stdlib only.** The package promises no dependencies; installing a web
framework to look at a SQLite file would be a poor reason to break that.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .journal import Journal

__all__ = ["serve", "build_handler"]


def _payload(journal: Journal, path: str, query: dict) -> tuple[int, object]:
    """Route one API request. Pure enough to test without a socket."""
    one = lambda k, default="": (query.get(k) or [default])[0]  # noqa: E731

    if path == "/api/summary":
        stats = journal.stats()
        return 200, {
            "runs": stats.runs,
            "incidents": stats.incidents,
            "committed": stats.committed,
            "reverted": stats.reverted,
            "refused": stats.refused,
            "verified": stats.verified,
            "revert_rate": round(stats.revert_rate, 4),
            "verified_rate": round(stats.verified_rate, 4),
            "repeat_offenders": [
                {"workload": w, "count": n} for w, n in journal.repeat_offenders(8)
            ],
            "namespaces": journal.namespaces(),
            "journal": str(journal.path),
        }

    if path == "/api/incidents":
        try:
            limit = max(1, min(500, int(one("limit", "100"))))
        except ValueError:
            limit = 100
        namespace, workload = one("namespace"), one("workload")
        rows = (journal.history(namespace, workload, limit) if workload
                else journal.recent(limit, namespace))
        return 200, [
            {
                "id": r.id, "at": r.created_at, "target": r.target, "state": r.state,
                "rule": r.rule, "summary": r.summary, "autonomy": r.autonomy,
                "commit": r.commit_sha, "reverted": r.reverted_sha,
            }
            for r in rows
        ]

    if path.startswith("/api/incident/"):
        try:
            incident_id = int(path.rsplit("/", 1)[1])
        except ValueError:
            return 400, {"error": "incident id must be a number"}
        detail = journal.detail(incident_id)
        return (200, detail) if detail else (404, {"error": "no such incident"})

    return 404, {"error": "not found"}


def build_handler(path):
    """Requests are served from a short-lived connection, opened per request.

    Two reasons, one forced and one welcome. SQLite connections are bound to the
    thread that created them, and this server is threaded — sharing one would
    fail on every request but the first, and the journal's read path swallows
    errors by design, so it would fail *silently*.

    The welcome part: a console reading a file that a `remediate` run is still
    appending to should show what was just written, and a per-request connection
    does that for free.
    """

    class Handler(BaseHTTPRequestHandler):
        server_version = "kubemend"
        sys_version = ""

        def do_GET(self) -> None:  # noqa: N802 - required name
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            if url.path.startswith("/api/"):
                journal = Journal(path)
                try:
                    if not journal.available:
                        status, body = 503, {"error": journal.error}
                    else:
                        status, body = _payload(journal, url.path, parse_qs(url.query))
                finally:
                    journal.close()
                return self._send(
                    status, json.dumps(body, indent=1).encode(), "application/json",
                )
            self._send(404, b"not found\n", "text/plain")

        # Every other verb, explicitly. A console over an agent that writes to
        # production should not be one typo away from accepting a POST.
        def do_POST(self):  # noqa: N802
            self._send(405, b'{"error":"read-only"}', "application/json")

        do_PUT = do_DELETE = do_PATCH = do_POST

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the browser navigated away mid-response

        def log_message(self, *args) -> None:
            pass  # a request log per poll is noise, not information

    return Handler


def serve(path, host: str = "127.0.0.1", port: int = 8420) -> None:
    httpd = ThreadingHTTPServer((host, port), build_handler(path))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


PAGE = r"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>kubemend</title>
<style>
  :root {
    --bg: #0b0d10; --panel: #14171c; --line: #23282f; --ink: #e6e9ee;
    --dim: #8b94a3; --faint: #5b6472;
    --ok: #4ade80; --warn: #fbbf24; --bad: #f87171; --info: #60a5fa;
    --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #fbfbfa; --panel: #fff; --line: #e6e6e3; --ink: #1a1d21;
      --dim: #6b7280; --faint: #9aa1ab;
      --ok: #15803d; --warn: #a16207; --bad: #b91c1c; --info: #1d4ed8;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 1080px; margin: 0 auto; padding: 40px 24px 80px; }
  header { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; margin-bottom: 6px; }
  h1 { font-size: 19px; margin: 0; letter-spacing: -.01em; font-weight: 600; }
  .sub { color: var(--dim); font-size: 13px; }
  .path { font-family: var(--mono); font-size: 11.5px; color: var(--faint); }

  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
           gap: 10px; margin: 26px 0 14px; }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 9px;
          padding: 14px 16px; }
  .card .n { font-size: 25px; font-weight: 620; letter-spacing: -.02em;
             font-variant-numeric: tabular-nums; }
  .card .k { color: var(--dim); font-size: 11.5px; text-transform: uppercase;
             letter-spacing: .07em; margin-top: 3px; }
  .card .note { color: var(--faint); font-size: 11.5px; margin-top: 7px; }
  .card.accent { border-color: color-mix(in srgb, var(--info) 40%, var(--line)); }
  .n.ok { color: var(--ok); } .n.warn { color: var(--warn); } .n.bad { color: var(--bad); }

  .bar { display: flex; gap: 8px; align-items: center; margin: 24px 0 10px; flex-wrap: wrap; }
  select, input {
    background: var(--panel); color: var(--ink); border: 1px solid var(--line);
    border-radius: 7px; padding: 6px 9px; font: inherit; font-size: 13px;
  }
  input { width: 190px; }
  .spacer { flex: 1; }
  .count { color: var(--faint); font-size: 12px; }

  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: .07em;
       color: var(--faint); font-weight: 600; padding: 0 10px 8px; }
  td { padding: 11px 10px; border-top: 1px solid var(--line); vertical-align: top; }
  tbody tr { cursor: pointer; }
  tbody tr:hover { background: color-mix(in srgb, var(--ink) 4%, transparent); }
  .when { color: var(--faint); font-family: var(--mono); font-size: 11.5px; white-space: nowrap; }
  .target { font-family: var(--mono); font-size: 12.5px; }
  .why { color: var(--dim); font-size: 12.5px; margin-top: 2px; }

  .tag { display: inline-block; font-size: 11px; font-weight: 600; padding: 2px 8px;
         border-radius: 20px; letter-spacing: .01em; white-space: nowrap;
         border: 1px solid currentColor; }
  .t-verified { color: var(--ok); } .t-committed { color: var(--info); }
  .t-reverted, .t-indeterminate { color: var(--warn); }
  .t-refused, .t-still_failing { color: var(--bad); }
  .t-reported { color: var(--faint); }

  .side { border-top: 1px solid var(--line); margin-top: 34px; padding-top: 20px; }
  .side h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .07em;
             color: var(--faint); margin: 0 0 4px; font-weight: 600; }
  .side p { color: var(--dim); font-size: 12.5px; margin: 0 0 12px; }
  .rep { display: flex; justify-content: space-between; padding: 7px 0;
         border-bottom: 1px solid var(--line); font-family: var(--mono); font-size: 12.5px; }
  .rep b { color: var(--warn); font-variant-numeric: tabular-nums; }

  dialog {
    background: var(--panel); color: var(--ink); border: 1px solid var(--line);
    border-radius: 11px; padding: 0; width: min(680px, 92vw); max-height: 84vh;
  }
  dialog::backdrop { background: rgba(0,0,0,.55); }
  .dh { padding: 18px 22px 14px; border-bottom: 1px solid var(--line);
        display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
  .db { padding: 18px 22px 24px; overflow-y: auto; max-height: 62vh; }
  .db h3 { font-size: 11px; text-transform: uppercase; letter-spacing: .07em;
           color: var(--faint); margin: 20px 0 8px; font-weight: 600; }
  .db h3:first-child { margin-top: 0; }
  .kv { display: grid; grid-template-columns: 128px 1fr; gap: 5px 14px; font-size: 13px; }
  .kv dt { color: var(--dim); } .kv dd { margin: 0; font-family: var(--mono); font-size: 12.5px;
           word-break: break-all; }
  pre { background: var(--bg); border: 1px solid var(--line); border-radius: 7px;
        padding: 11px 13px; overflow-x: auto; font-family: var(--mono);
        font-size: 12px; margin: 0 0 9px; }
  .x { background: none; border: 0; color: var(--dim); font-size: 21px; cursor: pointer;
       line-height: 1; padding: 0 2px; border-radius: 5px; }
  .x:hover { color: var(--ink); }
  /* A dialog autofocuses its first control, so the close button opens wearing a
     focus ring. Keep it for keyboards, drop it for the mouse click that got here. */
  .x:focus { outline: none; }
  .x:focus-visible { outline: 2px solid var(--info); outline-offset: 2px; }
  .empty { color: var(--faint); padding: 40px 4px; font-size: 13px; }
</style>

<div class="wrap">
  <header>
    <h1>kubemend</h1>
    <span class="sub">incident log</span>
    <span class="spacer"></span>
    <span class="path" id="path"></span>
  </header>

  <div class="cards" id="cards"></div>

  <div class="bar">
    <select id="ns"><option value="">all namespaces</option></select>
    <input id="wl" placeholder="workload name" autocomplete="off">
    <span class="spacer"></span>
    <span class="count" id="count"></span>
  </div>

  <table>
    <thead><tr>
      <th style="width:150px">when</th><th style="width:112px">outcome</th>
      <th>workload</th><th style="width:96px">commit</th>
    </tr></thead>
    <tbody id="rows"></tbody>
  </table>

  <div class="side" id="side" hidden>
    <h2>Keeps coming back</h2>
    <p>Remediated more than once. A workload that returns has a bug, not a bad release &mdash;
       and no rollback is going to fix it.</p>
    <div id="reps"></div>
  </div>
</div>

<dialog id="dlg">
  <div class="dh">
    <div>
      <div class="target" id="d-target"></div>
      <div class="why" id="d-when"></div>
    </div>
    <button class="x" onclick="dlg.close()" aria-label="close">&times;</button>
  </div>
  <div class="db" id="d-body"></div>
</dialog>

<script>
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const when = s => (s || "").replace("T", " ").replace(/(\+.*|Z)$/, "");
const get = async u => (await fetch(u)).json();

function card(n, k, note, tone, accent) {
  return `<div class="card${accent ? " accent" : ""}">
    <div class="n${tone ? " " + tone : ""}">${n}</div><div class="k">${k}</div>
    ${note ? `<div class="note">${note}</div>` : ""}</div>`;
}

async function summary() {
  const s = await get("/api/summary");
  $("#path").textContent = s.journal;

  // The revert rate is the number this whole log exists to produce, so it gets
  // the emphasis and the sentence explaining what it means.
  const pct = Math.round(s.revert_rate * 100);
  const tone = s.committed === 0 ? "" : pct === 0 ? "ok" : pct < 25 ? "warn" : "bad";
  $("#cards").innerHTML =
    card(s.incidents, "incidents", `across ${s.runs} run${s.runs === 1 ? "" : "s"}`) +
    card(s.committed, "committed", "fixes written to git") +
    card(s.refused, "refused", "held back by policy") +
    card(s.verified, "verified", "confirmed recovered", s.verified ? "ok" : "") +
    card(s.committed ? pct + "%" : "&mdash;", "revert rate",
         s.committed ? `${s.reverted} of ${s.committed} did not hold` : "nothing written yet",
         tone, true);

  const ns = $("#ns");
  ns.innerHTML = '<option value="">all namespaces</option>' +
    s.namespaces.map(n => `<option>${esc(n)}</option>`).join("");

  const reps = s.repeat_offenders.filter(r => r.count > 1);
  $("#side").hidden = reps.length === 0;
  $("#reps").innerHTML = reps.map(r =>
    `<div class="rep"><span>${esc(r.workload)}</span><b>${r.count}&times;</b></div>`).join("");
}

async function incidents() {
  const q = new URLSearchParams();
  if ($("#ns").value) q.set("namespace", $("#ns").value);
  if ($("#wl").value.trim()) q.set("workload", $("#wl").value.trim());
  const rows = await get("/api/incidents?" + q);

  $("#count").textContent = rows.length
    ? `${rows.length} incident${rows.length === 1 ? "" : "s"}` : "";
  $("#rows").innerHTML = rows.length ? rows.map(r => `
    <tr onclick="open_(${r.id})">
      <td class="when">${esc(when(r.at))}</td>
      <td><span class="tag t-${esc(r.state)}">${esc(r.state)}</span></td>
      <td><div class="target">${esc(r.target)}</div>
          <div class="why">${esc(r.summary)}</div></td>
      <td class="when">${esc((r.commit || "").slice(0, 7))}</td>
    </tr>`).join("")
    : `<tr><td colspan="4" class="empty">Nothing recorded yet. Run
       <code>kubemend remediate --repo &lt;path&gt;</code> and reload.</td></tr>`;
}

async function open_(id) {
  const d = await get("/api/incident/" + id);
  $("#d-target").textContent = d.target;
  $("#d-when").textContent = when(d.created_at) + " · " + d.state;

  const kv = [];
  const row = (k, v) => v && kv.push(`<dt>${k}</dt><dd>${esc(v)}</dd>`);
  row("autonomy", d.autonomy);
  row("commit", d.commit);
  row("branch", d.branch);
  row("pull request", d.pr_url);
  row("verification", d.verify_detail || d.verification);
  row("reverted in", d.reverted);
  if (d.refusals.length) row("refused because", d.refusals.join(", "));

  const findings = d.findings.map(f => `<pre>${esc(f.severity.toUpperCase())}  ${esc(f.rule)}
${esc(f.summary)}
${esc(JSON.stringify(f.evidence, null, 1))}</pre>`).join("");

  // before/after is the whole reversibility argument, so show both sides.
  const actions = d.actions.map(a => `<pre>${esc(a.kind)}${a.container ? "  container=" + esc(a.container) : ""}   ${a.impacted_pods} pod(s) affected
before  ${esc(JSON.stringify(a.before))}
after   ${esc(JSON.stringify(a.after))}
${a.written ? "written  " + esc(a.detail || "yes") : "not written  " + esc(a.skipped_reason || "held")}</pre>`).join("");

  $("#d-body").innerHTML =
    `<h3>Outcome</h3><dl class="kv">${kv.join("")}</dl>` +
    (findings ? `<h3>What it saw</h3>${findings}` : "") +
    (actions ? `<h3>What it proposed</h3>${actions}` : "");
  $("#dlg").showModal();
}

$("#ns").onchange = incidents;
let t; $("#wl").oninput = () => { clearTimeout(t); t = setTimeout(incidents, 180); };
summary(); incidents();
</script>
"""
