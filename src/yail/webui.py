"""Lightweight admin web UI for the YAIL server.

Stdlib-only (http.server) dashboard on a separate port from the YAIL
protocol socket. Serves a single embedded HTML page and a small JSON API:

    GET  /api/status   server identity, uptime, connection counts
    GET  /api/config   generation/search config (API keys masked)
    POST /api/config   update config live; optionally persist to the env file
    GET  /api/logs     recent log lines from the in-memory ring buffer
    GET  /api/stats    counters, active client sessions, recent image events

Binds to 127.0.0.1 by default: the API can change API keys, so exposing
it beyond localhost (--ui-host) should be a deliberate choice.
"""
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from yail import __version__
from yail.config import ImageGenConfig, ServerConfig, valid_search_backends
from yail.stats import LOG_BUFFER, STATS

logger = logging.getLogger(__name__)

DEFAULT_UI_PORT = 5557


def mask_key(key: str | None) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:3]}...{key[-4:]}"


class WebUIContext:
    """Everything the request handler needs, shared across requests."""

    def __init__(self, gen_config: ImageGenConfig, server_config: ServerConfig,
                 filenames: list[str], env_path: str = ".env"):
        self.gen_config = gen_config
        self.server_config = server_config
        self.filenames = filenames
        self.env_path = env_path


def _persist_env(env_path: str, values: dict[str, str]) -> None:
    """Write key=value pairs to the env file, creating it if needed."""
    from dotenv import set_key
    if not os.path.exists(env_path):
        with open(env_path, "w", encoding="utf-8"):
            pass
    for key, value in values.items():
        set_key(env_path, key, value)
    logger.info(f"Persisted {', '.join(values)} to {env_path}")


class AdminRequestHandler(BaseHTTPRequestHandler):
    context: WebUIContext  # set on the server class by start_webui

    # The dashboard polls every couple of seconds; default http.server
    # logging would flood the same log buffer the dashboard displays.
    def log_message(self, format: str, *args) -> None:
        pass

    # ----- response helpers ----------------------------------------------

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ----- GET -------------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"

        if route == "/":
            self.send_html(DASHBOARD_HTML)
        elif route == "/api/status":
            self.send_json(self.api_status())
        elif route == "/api/config":
            self.send_json(self.api_config())
        elif route == "/api/logs":
            params = parse_qs(parsed.query)
            n = min(int(params.get("n", ["200"])[0]), 500)
            self.send_json({"logs": LOG_BUFFER.tail(n)})
        elif route == "/api/stats":
            self.send_json(STATS.snapshot())
        else:
            self.send_json({"error": "not found"}, status=404)

    def api_status(self) -> dict:
        ctx = self.context
        snap = STATS.snapshot()
        try:
            from importlib.metadata import version
            ddgs_version = version("ddgs")
        except Exception:
            ddgs_version = "unknown"
        return {
            "version": __version__,
            "yail_host": ctx.server_config.host,
            "yail_port": ctx.server_config.port,
            "uptime_seconds": snap["uptime_seconds"],
            "active_connections": snap["active_connections"],
            "total_connections": snap["total_connections"],
            "local_files": len(ctx.filenames),
            "files_enabled": ctx.server_config.files_enabled,
            "streaming_enabled": ctx.server_config.streaming_enabled,
            "camera_enabled": ctx.server_config.enable_camera,
            "search_backend": (f"ddgs {ddgs_version} "
                               f"[{','.join(ctx.server_config.search_backends)}]"),
            "openrouter_key_set": bool(ctx.gen_config.api_key),
            "gen_model": ctx.gen_config.model,
        }

    def api_config(self) -> dict:
        gen = self.context.gen_config
        server = self.context.server_config
        return {
            "files_enabled": server.files_enabled,
            "files_path": server.paths[0] if server.paths else "",
            "files_count": len(self.context.filenames),
            "streaming_enabled": server.streaming_enabled,
            "stream_max_retries": server.stream_max_retries,
            "stream_retry_wait": server.stream_retry_wait,
            "download_timeout": server.download_timeout,
            "search_backends": list(server.search_backends),
            "search_max_results": server.search_max_results,
            "model": gen.model,
            "system_prompt": gen.system_prompt,
            "openrouter_api_key": mask_key(gen.api_key),
            "env_file": os.path.abspath(self.context.env_path),
            "valid": {
                "search_backends": valid_search_backends(),
            },
        }

    # ----- POST ------------------------------------------------------------

    def do_POST(self) -> None:
        if urlparse(self.path).path.rstrip("/") != "/api/config":
            self.send_json({"error": "not found"}, status=404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self.send_json({"error": "invalid JSON body"}, status=400)
            return
        self.send_json(self.apply_config(payload))

    def apply_config(self, payload: dict) -> dict:
        gen = self.context.gen_config
        applied: list[str] = []
        errors: list[str] = []
        to_persist: dict[str, str] = {}

        def setting(name: str, setter, env_var: str | None = None) -> None:
            value = payload.get(name)
            if value is None or value == "":
                return
            if setter(value) is False:
                errors.append(f"invalid {name}: {value!r}")
                return
            applied.append(name)
            if env_var:
                to_persist[env_var] = str(value)

        setting("model", gen.set_model, None)
        if "model" in applied:
            # Persist the resolved OpenRouter id, not the raw input.
            to_persist["GEN_MODEL"] = gen.model
        setting("system_prompt", gen.set_system_prompt, "OPENAI_SYSTEM_PROMPT")

        openrouter_key = payload.get("openrouter_api_key")
        if openrouter_key:
            gen.set_api_key(openrouter_key)
            applied.append("openrouter_api_key")
            to_persist["OPENROUTER_API_KEY"] = openrouter_key

        server = self.context.server_config
        if "files_path" in payload:
            path = str(payload["files_path"]).strip()
            current = server.paths[0] if server.paths else ""
            if path != current:
                if path and not (os.path.isabs(path) and os.path.isdir(path)):
                    errors.append(f"files_path must be an existing absolute "
                                  f"folder path: {path!r}")
                else:
                    from yail.files import collect_files
                    new_files = (collect_files([path], server.extensions)
                                 if path else [])
                    # Mutate in place: the server and active sessions share
                    # this list object.
                    self.context.filenames[:] = new_files
                    server.paths = [path] if path else []
                    if not path:
                        server.files_enabled = False
                    applied.append("files_path")
                    to_persist["FILES_PATH"] = path

        if "files_enabled" in payload:
            enabled = bool(payload["files_enabled"])
            if enabled and not server.paths:
                errors.append("cannot enable file serving without a folder path")
            elif enabled != server.files_enabled:
                server.files_enabled = enabled
                logger.info(f"Local file serving {'enabled' if enabled else 'disabled'} "
                            f"via web UI")
                applied.append("files_enabled")
            # Always persist the flag alongside other persisted file settings
            # so the choice survives restarts.
            to_persist["FILES_ENABLED"] = ("true" if server.files_enabled else "false")

        def num_setting(name: str, attr: str, cast, lo, hi, env_var: str) -> None:
            if name not in payload:
                return
            try:
                value = cast(payload[name])
            except (TypeError, ValueError):
                errors.append(f"invalid {name}: {payload[name]!r}")
                return
            if not lo <= value <= hi:
                errors.append(f"{name} must be between {lo} and {hi}")
                return
            if getattr(server, attr) != value:
                setattr(server, attr, value)
                logger.info(f"{name} set to {value} via web UI")
                applied.append(name)
            to_persist[env_var] = str(value)

        num_setting("stream_max_retries", "stream_max_retries", int, 1, 100,
                    "STREAM_MAX_RETRIES")
        num_setting("stream_retry_wait", "stream_retry_wait", float, 0, 60,
                    "STREAM_RETRY_WAIT")
        num_setting("download_timeout", "download_timeout", float, 1, 120,
                    "STREAM_DOWNLOAD_TIMEOUT")
        num_setting("search_max_results", "search_max_results", int, 1, 2000,
                    "SEARCH_MAX_RESULTS")

        if "streaming_enabled" in payload:
            enabled = bool(payload["streaming_enabled"])
            if enabled != server.streaming_enabled:
                server.streaming_enabled = enabled
                logger.info(f"Streaming {'enabled' if enabled else 'disabled'} via web UI")
                applied.append("streaming_enabled")
            to_persist["STREAM_ENABLED"] = "true" if enabled else "false"

        if "search_backends" in payload:
            raw = payload["search_backends"]
            backends = ([str(b).strip().lower() for b in raw if str(b).strip()]
                        if isinstance(raw, list) else [])
            valid = valid_search_backends()
            unknown = [b for b in backends if b not in valid]
            if not backends:
                errors.append("search_backends must be a non-empty list")
            elif unknown:
                errors.append(f"unknown search backends: {', '.join(unknown)} "
                              f"(valid: {', '.join(valid)})")
            else:
                if "auto" in backends:
                    backends = ["auto"]
                if backends != server.search_backends:
                    server.search_backends = backends
                    logger.info(f"Search backends set to {backends} via web UI")
                    applied.append("search_backends")
                to_persist["SEARCH_BACKENDS"] = ",".join(backends)

        persisted = False
        if payload.get("persist") and to_persist:
            try:
                _persist_env(self.context.env_path, to_persist)
                persisted = True
            except Exception as e:
                errors.append(f"could not write env file: {e}")

        return {"applied": applied, "errors": errors, "persisted": persisted,
                "config": self.api_config()}


def start_webui(host: str, port: int, context: WebUIContext) -> ThreadingHTTPServer:
    """Start the admin UI in a daemon thread and return the http server."""
    handler = type("BoundAdminRequestHandler", (AdminRequestHandler,),
                   {"context": context})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever,
                              name="yail-webui", daemon=True)
    thread.start()
    logger.info(f"Admin web UI listening on http://{host}:{port}/")
    return httpd


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>YAIL Server</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { --bg:#10141c; --panel:#1a2130; --line:#2b3852; --text:#d6e2f0;
          --dim:#7d8da6; --accent:#5fd3f0; --ok:#69d58c; --err:#ef6a6a; --warn:#e8c468; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:14px/1.45 "SF Mono", Menlo, Consolas, monospace; }
  header { padding:14px 22px; border-bottom:1px solid var(--line);
           display:flex; align-items:baseline; gap:14px; }
  header h1 { margin:0; font-size:17px; color:var(--accent); letter-spacing:2px; }
  header .sub { color:var(--dim); font-size:12px; }
  main { padding:18px 22px; max-width:1180px; margin:0 auto; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
           gap:10px; margin-bottom:18px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:8px;
          padding:10px 14px; }
  .card .k { color:var(--dim); font-size:11px; text-transform:uppercase; letter-spacing:1px; }
  .card .v { font-size:20px; margin-top:2px; }
  .panes { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
  @media (max-width:900px){ .panes { grid-template-columns:1fr; } }
  section { background:var(--panel); border:1px solid var(--line); border-radius:8px;
            padding:14px 16px; margin-bottom:14px; }
  section h2 { margin:0 0 10px; font-size:13px; color:var(--accent);
               text-transform:uppercase; letter-spacing:1px; }
  table { width:100%; border-collapse:collapse; font-size:12.5px; }
  th { text-align:left; color:var(--dim); font-weight:normal; padding:3px 8px 3px 0; }
  td { padding:3px 8px 3px 0; border-top:1px solid var(--line); }
  /* Fixed-height scroll pane sized to ~10 table rows; newest rows on top. */
  .scrollpane { max-height:265px; overflow-y:auto; }
  .scrollpane thead th { position:sticky; top:0; background:var(--panel); }
  label { display:block; color:var(--dim); font-size:11.5px; margin:8px 0 2px; }
  input, select, textarea { width:100%; background:var(--bg); color:var(--text);
      border:1px solid var(--line); border-radius:5px; padding:6px 8px;
      font:inherit; font-size:12.5px; }
  textarea { resize:vertical; min-height:48px; }
  button { background:var(--accent); color:#08222b; border:0; border-radius:5px;
           padding:8px 16px; font:inherit; font-weight:bold; cursor:pointer; margin-top:12px; }
  button:hover { filter:brightness(1.12); }
  .row { display:grid; grid-template-columns:1fr 1fr; gap:0 12px; }
  .row3 { display:grid; grid-template-columns:1fr 1fr 1fr; gap:0 12px; }
  .chip { display:inline-flex; align-items:center; gap:7px; background:var(--bg);
          border:1px solid var(--line); border-radius:12px; padding:2px 10px;
          margin:3px 5px 0 0; font-size:12.5px; }
  .chip span { cursor:pointer; color:var(--err); font-weight:bold; }
  .addrow { display:flex; gap:8px; margin-top:6px; }
  .addrow select { flex:1; }
  .addrow button { margin-top:0; padding:5px 12px; }
  fieldset { border:1px solid var(--line); border-radius:6px; margin:14px 0 0;
             padding:2px 12px 12px; }
  legend { color:var(--dim); font-size:11px; text-transform:uppercase;
           letter-spacing:1px; padding:0 6px; }
  /* ~40 visible log lines (11.5px * 1.5 line-height * 40), newest at the top. */
  #logs { background:var(--bg); border:1px solid var(--line); border-radius:5px;
          padding:8px 10px; height:690px; overflow-y:auto; font-size:11.5px;
          line-height:1.5; white-space:pre-wrap; word-break:break-all; }
  .lv-ERROR,.lv-CRITICAL { color:var(--err); }
  .lv-WARNING { color:var(--warn); }
  .lv-INFO { color:var(--text); }
  .lv-DEBUG { color:var(--dim); }
  .ok { color:var(--ok); } .bad { color:var(--err); } .dim { color:var(--dim); }
  #saveMsg { margin-left:10px; font-size:12px; }
  .persist { display:flex; align-items:center; gap:6px; margin-top:10px; color:var(--dim); }
  .persist input { width:auto; }
</style>
</head>
<body>
<header>
  <h1>YAIL SERVER</h1>
  <span class="sub" id="ident">connecting…</span>
</header>
<main>
  <div class="cards" id="cards"></div>
  <div class="panes">
    <div>
      <section>
        <h2>Generation Config</h2>
        <form id="cfgForm" onsubmit="return saveConfig(event)">
          <label>Model (OpenRouter id, e.g. google/gemini-2.5-flash-image)</label>
          <input name="model" id="cfg_model">
          <label>System prompt</label>
          <textarea name="system_prompt" id="cfg_prompt"></textarea>
          <label>OpenRouter API key <span class="dim" id="cur_orkey"></span></label>
          <input name="openrouter_api_key" id="cfg_orkey" placeholder="leave blank to keep">

          <fieldset>
            <legend>Local files</legend>
            <label>Folder <span class="dim" id="cur_files"></span></label>
            <input id="cfg_fpath" placeholder="absolute folder path (blank = no local files)">
            <div class="persist">
              <input type="checkbox" id="cfg_fenabled">
              <span>enable local file serving (the <b>files</b> command)</span>
            </div>
          </fieldset>
          <fieldset>
            <legend>Streaming</legend>
            <div class="persist" style="margin-top:6px">
              <input type="checkbox" id="cfg_stream">
              <span>enable streaming / slideshow (the <b>next</b> command)</span>
            </div>
            <div class="row3">
              <div><label>Max retries</label><input id="cfg_retries" type="number" min="1" max="100"></div>
              <div><label>Retry wait (s)</label><input id="cfg_rwait" type="number" min="0" max="60" step="0.1"></div>
              <div><label>Download timeout (s)</label><input id="cfg_dlto" type="number" min="1" max="120" step="0.5"></div>
            </div>
          </fieldset>
          <fieldset>
            <legend>Image search servers</legend>
            <div id="backendChips"></div>
            <div class="addrow">
              <select id="backendSel"></select>
              <button type="button" onclick="addBackend()">Add</button>
            </div>
            <label>Max search results</label>
            <input id="cfg_smax" type="number" min="1" max="2000">
          </fieldset>
          <div class="persist">
            <input type="checkbox" id="cfg_persist" checked>
            <span>persist to <span id="envPath">.env</span></span>
          </div>
          <button type="submit">Apply</button><span id="saveMsg"></span>
        </form>
      </section>
      <section>
        <h2>Clients</h2>
        <table><thead><tr>
          <th>id</th><th>address</th><th>mode</th><th>gfx</th><th>last command</th>
          <th>imgs</th><th>idle</th>
        </tr></thead><tbody id="clients"></tbody></table>
      </section>
    </div>
    <div>
      <section>
        <h2>Recent Images</h2>
        <div class="scrollpane">
          <table><thead><tr>
            <th>time</th><th>client</th><th>source</th><th>target</th><th>ok</th><th>ms</th>
          </tr></thead><tbody id="images"></tbody></table>
        </div>
      </section>
    </div>
  </div>
  <section>
    <h2>Logs <span class="dim" style="text-transform:none">(newest first)</span></h2>
    <div id="logs"></div>
  </section>
</main>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmtAge = s => s < 60 ? s + "s" : s < 3600 ? (s/60|0) + "m" : (s/3600|0) + "h" + ((s%3600)/60|0) + "m";
const fmtTime = ts => new Date(ts*1000).toLocaleTimeString();
let cfgLoaded = false;

async function getJSON(url) { const r = await fetch(url); return r.json(); }

function card(k, v, cls="") {
  return `<div class="card"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`;
}

async function refreshStatus() {
  const s = await getJSON("/api/status");
  $("ident").textContent =
      `v${s.version} • yail tcp ${s.yail_host}:${s.yail_port} • ${s.search_backend}`;
  $("cards").innerHTML =
      card("Uptime", fmtAge(s.uptime_seconds)) +
      card("Active clients", s.active_connections) +
      card("Total conns", s.total_connections) +
      card("Local files", s.files_enabled ? s.local_files : "off",
           s.files_enabled ? "" : "dim") +
      card("OpenRouter key", s.openrouter_key_set ? "set" : "missing",
           s.openrouter_key_set ? "ok" : "bad");
}

async function refreshStats() {
  const s = await getJSON("/api/stats");
  const now = Date.now()/1000;
  $("clients").innerHTML = s.sessions.map(c => `<tr>
      <td>${c.id}</td><td>${esc(c.address)}</td><td>${esc(c.mode ?? "-")}</td>
      <td>${c.gfx_mode ?? "-"}</td><td>${esc(c.last_command ?? "-")}</td>
      <td>${c.images_sent}</td><td>${fmtAge(Math.max(0, now - c.last_activity|0))}</td>
    </tr>`).join("") || `<tr><td colspan="7" class="dim">no connected clients</td></tr>`;
  $("images").innerHTML = s.recent_images.slice().reverse().map(i => `<tr>
      <td>${fmtTime(i.ts)}</td><td>${i.client}</td><td>${esc(i.source)}</td>
      <td>${esc(i.target)}</td>
      <td class="${i.ok ? "ok" : "bad"}">${i.ok ? "ok" : "FAIL"}</td>
      <td>${i.duration_ms}</td>
    </tr>`).join("") || `<tr><td colspan="6" class="dim">no images served yet</td></tr>`;
}

async function refreshLogs() {
  const data = await getJSON("/api/logs?n=500");
  // Newest first: latest line stays pinned at the top, older lines push down.
  $("logs").innerHTML = data.logs.slice().reverse().map(l =>
      `<div class="lv-${l.level}">${fmtTime(l.ts)} ${l.level.padEnd(7)} ${esc(l.message)}</div>`
    ).join("");
}

async function loadConfig() {
  const c = await getJSON("/api/config");
  $("cfg_model").value = c.model;
  $("cfg_prompt").value = c.system_prompt;
  $("cur_orkey").textContent = c.openrouter_api_key ? `(${c.openrouter_api_key})` : "(not set)";
  $("cfg_fpath").value = c.files_path;
  $("cfg_fenabled").checked = c.files_enabled;
  $("cur_files").textContent = `(${c.files_count} files indexed)`;
  $("cfg_stream").checked = c.streaming_enabled;
  $("cfg_retries").value = c.stream_max_retries;
  $("cfg_rwait").value = c.stream_retry_wait;
  $("cfg_dlto").value = c.download_timeout;
  $("cfg_smax").value = c.search_max_results;
  searchBackends = c.search_backends.slice();
  validBackends = c.valid.search_backends;
  renderBackends();
  $("envPath").textContent = c.env_file;
  cfgLoaded = true;
}

let searchBackends = [], validBackends = [];

function renderBackends() {
  $("backendChips").innerHTML = searchBackends.map((b, i) =>
      `<span class="chip">${esc(b)}<span onclick="removeBackend(${i})" title="remove">&times;</span></span>`
    ).join("") || `<span class="dim">none — searches will fail</span>`;
  const options = validBackends.filter(b => !searchBackends.includes(b));
  $("backendSel").innerHTML = options.map(b => `<option>${esc(b)}</option>`).join("");
}

function addBackend() {
  const sel = $("backendSel").value;
  if (sel && !searchBackends.includes(sel)) {
    // "auto" means all engines, so it replaces any specific selection.
    searchBackends = sel === "auto" ? ["auto"] : [...searchBackends.filter(b => b !== "auto"), sel];
    renderBackends();
  }
}

function removeBackend(i) {
  searchBackends.splice(i, 1);
  renderBackends();
}

async function saveConfig(ev) {
  ev.preventDefault();
  const body = {
    model: $("cfg_model").value,
    system_prompt: $("cfg_prompt").value,
    openrouter_api_key: $("cfg_orkey").value,
    files_path: $("cfg_fpath").value, files_enabled: $("cfg_fenabled").checked,
    streaming_enabled: $("cfg_stream").checked,
    stream_max_retries: $("cfg_retries").value,
    stream_retry_wait: $("cfg_rwait").value,
    download_timeout: $("cfg_dlto").value,
    search_backends: searchBackends,
    search_max_results: $("cfg_smax").value,
    persist: $("cfg_persist").checked,
  };
  const r = await fetch("/api/config",
      {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
  const res = await r.json();
  const msg = $("saveMsg");
  if (res.errors && res.errors.length) {
    msg.textContent = res.errors.join("; "); msg.className = "bad";
  } else {
    msg.textContent = `applied: ${res.applied.join(", ") || "nothing"}` +
                      (res.persisted ? " (saved)" : "");
    msg.className = "ok";
  }
  $("cfg_orkey").value = "";
  loadConfig(); refreshStatus();
  return false;
}

function tick() { refreshStatus(); refreshStats(); refreshLogs(); if (!cfgLoaded) loadConfig(); }
tick();
setInterval(tick, 2500);
</script>
</body>
</html>
"""
