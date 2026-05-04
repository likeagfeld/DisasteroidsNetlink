#!/usr/bin/env python3
"""
Saturn NetLink Unified Admin Portal

Single HTTP portal that aggregates admin dashboards for every game server
running on this host. Nginx fronts the single /admin/ URL and this service
proxies to each game server's local admin JSON API.

Games are hardcoded below as (slug, label, backing port). Each backing game
server is expected to listen on 127.0.0.1:<port> and expose:
  GET  /api/state     -> {uptime, total_joins, players[...], game{...}}
  GET  /api/history   -> {entries: [...]}
  POST /api/kick      {uuid}
  POST /api/end_game
  POST /api/restart

This aggregator itself listens on 127.0.0.1:ADMIN_PORT (loopback only).
Nginx terminates TLS, enforces Basic-Auth, and forwards with the header
X-Admin-Auth: nginx-verified, which each backing service trusts.
"""

import argparse
import json
import logging
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib import request as urlrequest
from urllib.error import URLError, HTTPError
from urllib.parse import urlparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("saturn_admin")


GAMES = [
    # (slug, display label, backing admin port)
    ("coup",         "Coup",            9090),
    ("flicky",       "Flicky's Flock",  9091),
    ("disasteroids", "Disasteroids",    9092),
    ("utenyaa",      "Utenyaa",         9093),
    ("mmm",          "Micro Motor Mayhem", 9094),
]

UPSTREAM_TIMEOUT = 3.0

# Pretty labels for common game-dict keys. Any unknown key is rendered as-is
# with underscores replaced by spaces. Ordering is preserved when the key is
# present; unknown keys are appended alphabetically.
CARD_ORDER = [
    "active", "phase", "game_type",
    "human_count", "bot_count", "players_in_game",
    "wave", "asteroids_left", "ships_alive",
    "active_pipes", "pipe_speed",
    "room_state", "turn", "deck_remaining",
    "track_id", "current_lap", "lap_count", "racers_finished",
    "total_connects_today",
]
CARD_LABELS = {
    "active":            "Game",
    "phase":             "Phase",
    "game_type":         "Mode",
    "human_count":       "Players",
    "bot_count":         "Bots",
    "players_in_game":   "In Game",
    "wave":              "Wave",
    "asteroids_left":    "Asteroids",
    "ships_alive":       "Ships",
    "active_pipes":      "Pipes",
    "pipe_speed":        "Speed",
    "room_state":        "Room",
    "turn":              "Turn",
    "deck_remaining":    "Deck",
    "track_id":              "Track",
    "current_lap":           "Lap",
    "lap_count":             "Laps",
    "racers_finished":       "Finished",
    "total_connects_today":  "Joins Today",
}


def _port_for(slug: str) -> int:
    for s, _, p in GAMES:
        if s == slug:
            return p
    return 0


def _upstream_get(port: int, path: str) -> tuple:
    url = "http://127.0.0.1:%d%s" % (port, path)
    req = urlrequest.Request(url, headers={"X-Admin-Auth": "nginx-verified"})
    try:
        with urlrequest.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
            return resp.status, resp.read()
    except HTTPError as e:
        return e.code, e.read() if hasattr(e, "read") else b""
    except (URLError, TimeoutError, OSError) as e:
        log.debug("Upstream GET failed (%s): %s", url, e)
        return 0, b""


def _upstream_post(port: int, path: str, body: bytes) -> tuple:
    url = "http://127.0.0.1:%d%s" % (port, path)
    req = urlrequest.Request(
        url, data=body, method="POST",
        headers={
            "X-Admin-Auth": "nginx-verified",
            "Content-Type": "application/json",
        })
    try:
        with urlrequest.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
            return resp.status, resp.read()
    except HTTPError as e:
        return e.code, e.read() if hasattr(e, "read") else b""
    except (URLError, TimeoutError, OSError) as e:
        log.debug("Upstream POST failed (%s): %s", url, e)
        return 0, b""


def _render_admin_html() -> bytes:
    # JS constants injected from GAMES so the browser knows about each tab.
    games_js = json.dumps([
        {"slug": slug, "label": label} for slug, label, _ in GAMES
    ])
    card_order_js = json.dumps(CARD_ORDER)
    card_labels_js = json.dumps(CARD_LABELS)

    tab_buttons = []
    tab_panels = []
    for idx, (slug, label, _port) in enumerate(GAMES):
        cls_tab = "tab active" if idx == 0 else "tab"
        cls_pane = "tab-content active" if idx == 0 else "tab-content"
        tab_buttons.append(
            '<button class="%s" data-slug="%s" onclick="showTab(\'%s\')">%s</button>'
            % (cls_tab, slug, slug, label))

        # MMM-specific control panel and live join-history table.
        mmm_panel = ""
        mmm_history_panel = ""
        if slug == "mmm":
            track_options = "".join(
                "<option value=\"%d\">%d</option>" % (i, i)
                for i in range(1, 16))
            lap_options = "".join(
                "<option value=\"%d\">%d</option>" % (i, i)
                for i in range(1, 10))
            respawn_options = "".join(
                "<option value=\"%d\">%ds</option>" % (i, i)
                for i in (0, 4, 6, 8, 10, 15, 20, 30))
            mmm_panel = f"""
  <div class="panel mmm-panel">
    <h3>MMM Race Controls</h3>
    <div class="mmm-row">
      <span class="mmm-label">Bots:</span>
      <button class="btn" onclick="mmmBot('{slug}','remove')">-</button>
      <span class="mmm-bot-count" style="min-width:24px;text-align:center">0</span>
      <button class="btn" onclick="mmmBot('{slug}','add')">+</button>
    </div>
    <div class="mmm-row">
      <label class="mmm-label"><input type="checkbox" class="mmm-rand-track"> Random track</label>
      <span class="mmm-label">Forced track:</span>
      <select class="mmm-forced-track">{track_options}</select>
    </div>
    <div class="mmm-row">
      <span class="mmm-label">Lap count:</span>
      <select class="mmm-lap-count">{lap_options}</select>
      <span class="mmm-label">Powerup respawn:</span>
      <select class="mmm-pup-respawn">{respawn_options}</select>
    </div>
    <div class="controls" style="margin-top:10px">
      <button class="btn" onclick="mmmApplyTune('{slug}')">Apply</button>
      <button class="btn btn-warn" onclick="mmmForceEnd('{slug}')">Force end race</button>
    </div>
  </div>"""
            mmm_history_panel = f"""
  <div class="panel">
    <h3>Join History (Last 20)</h3>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Time</th><th>Event</th><th>Name</th><th>IP</th><th>Reason</th></tr></thead>
        <tbody class="mmm-jh-table"></tbody>
      </table>
    </div>
  </div>"""

        # Utenyaa-specific stage toggles. Each checkbox POSTs to
        # /api/utenyaa/tune?key=stage_enabled_<name>&value=<true|false>
        # which the userver.py admin endpoint translates into a tune
        # dict update. _pick_stage honors the mask on every match start.
        # CROSS unchecked by default — its road/grass intersection
        # geometry has tank-collision dead spots; will be re-enabled
        # once geometry is patched.
        utenyaa_stage_panel = ""
        utenyaa_history_panel = ""
        if slug == "utenyaa":
            utenyaa_stage_panel = f"""
  <div class="panel utenyaa-stage-panel">
    <h3>Stage Pool <span class="muted" style="font-weight:normal;font-size:13px">(checked stages eligible for next match)</span></h3>
    <div class="tuning-knobs">
      <label><input type="checkbox" class="utenyaa-stage" data-stage-key="stage_enabled_island"> Island</label>
      <label><input type="checkbox" class="utenyaa-stage" data-stage-key="stage_enabled_cross"> Cross <span class="muted">(known issue: tank gets stuck at road/grass intersections)</span></label>
      <label><input type="checkbox" class="utenyaa-stage" data-stage-key="stage_enabled_valley"> Valley</label>
      <label><input type="checkbox" class="utenyaa-stage" data-stage-key="stage_enabled_railway"> Railway</label>
    </div>
    <div class="controls" style="margin-top:10px">
      <button class="btn" onclick="utenyaaApplyStages('{slug}')">Apply</button>
      <span class="utenyaa-stage-status muted" style="margin-left:10px"></span>
    </div>
  </div>"""
            utenyaa_history_panel = f"""
  <div class="panel">
    <h3>Join History (Last 200)</h3>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Time</th><th>Event</th><th>Name</th><th>IP</th><th>Reason</th></tr></thead>
        <tbody class="utenyaa-jh-table"></tbody>
      </table>
    </div>
  </div>"""

        # Disasteroids-specific join-history table. Mirrors the Utenyaa
        # panel and consumes /api/disasteroids/join_history (epoch `t`
        # rendered client-side via toLocaleString).
        disasteroids_history_panel = ""
        if slug == "disasteroids":
            disasteroids_history_panel = f"""
  <div class="panel">
    <h3>Join History (Last 200)</h3>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Time</th><th>Event</th><th>Name</th><th>IP</th><th>Reason</th></tr></thead>
        <tbody class="disasteroids-jh-table"></tbody>
      </table>
    </div>
  </div>"""

        # Sync-engine panel — toggles client rendering between LERP
        # (current single-snapshot lerp+extrap) and RING (Utenyaa-style
        # snapshot ring + interp/extrap). LERP is the known-good default;
        # RING activates only on clients built with the Phase 3+ binary.
        sync_panel = ""
        if slug == "disasteroids":
            sync_panel = f"""
  <div class="panel sync-panel">
    <h3>Sync Engine <span class="sync-badge"></span></h3>
    <p class="muted" style="margin-bottom:8px">
      Switches how clients render remote ships. <b>LERP</b> = current
      single-snapshot lerp + 3-frame forward extrapolation (known-good).
      <b>RING</b> = snapshot ring + bracketing-pair interpolation + velocity
      extrapolation (Utenyaa-style; smoother under jitter, requires a
      client binary built with the new sync code). Switching mid-match
      takes effect on the next match start.
    </p>
    <div class="tuning-presets">
      <label><input type="radio" name="syncmode-{slug}" value="LERP"> LERP <span class="muted">(default, current)</span></label>
      <label><input type="radio" name="syncmode-{slug}" value="RING"> RING <span class="muted">(Utenyaa-style)</span></label>
    </div>
    <div class="controls" style="margin-top:10px">
      <button class="btn" onclick="applySyncMode('{slug}')">Apply</button>
      <button class="btn btn-warn" onclick="applySyncModeDirect('{slug}','LERP')">Revert to LERP</button>
    </div>
  </div>"""

        # Tuning panel only rendered (and populated) for Disasteroids.
        tuning_panel = ""
        if slug == "disasteroids":
            tuning_panel = f"""
  <div class="panel tuning-panel">
    <h3>Bandwidth Tuning <span class="tuning-badge"></span></h3>
    <p class="muted" style="margin-bottom:8px">
      Throttles server-to-client SHIP_SYNC traffic to fit the 14,400-baud
      NetLink modem ceiling (~1,440 B/s). Defaults to PASSTHROUGH (current
      behavior). Changes apply live — no Saturn restart.
    </p>
    <div class="tuning-presets">
      <label><input type="radio" name="preset-{slug}" value="PASSTHROUGH"> PASSTHROUGH</label>
      <label><input type="radio" name="preset-{slug}" value="LIGHT"> LIGHT</label>
      <label><input type="radio" name="preset-{slug}" value="MODERATE"> MODERATE</label>
      <label><input type="radio" name="preset-{slug}" value="AGGRESSIVE"> AGGRESSIVE</label>
      <label><input type="radio" name="preset-{slug}" value="AUTO"> AUTO <span class="muted">(picks by player count)</span></label>
      <label><input type="radio" name="preset-{slug}" value="CUSTOM"> CUSTOM</label>
    </div>
    <div class="tuning-estimate muted"></div>
    <details class="tuning-advanced">
      <summary>Advanced knobs (enabled in CUSTOM mode)</summary>
      <div class="tuning-knobs">
        <label>ship_sync_decimate
          <input type="number" min="1" max="4" step="1" data-key="ship_sync_decimate">
          <span class="muted">1 = every SHIP_STATE, 4 = every 4th</span>
        </label>
        <label><input type="checkbox" data-key="ship_sync_skip_stationary"> skip_stationary</label>
        <label>stationary_vel_threshold
          <input type="number" min="0" max="50" step="1" data-key="stationary_vel_threshold">
        </label>
        <label>ship_sync_keepalive_ticks
          <input type="number" min="15" max="120" step="1" data-key="ship_sync_keepalive_ticks">
        </label>
        <label>bot_sync_every_n_ticks
          <input type="number" min="1" max="4" step="1" data-key="bot_sync_every_n_ticks">
        </label>
      </div>
    </details>
    <div class="controls" style="margin-top:10px">
      <button class="btn" onclick="applyTuning('{slug}')">Apply</button>
      <button class="btn btn-warn" onclick="applyTuningPreset('{slug}','PASSTHROUGH')">Reset to PASSTHROUGH</button>
    </div>
  </div>"""

        tab_panels.append(f"""
<div id="tab-{slug}" class="{cls_pane}" data-slug="{slug}">
  <div class="info">
    <span>Next refresh: <span class="countdown">3</span>s</span>
    <span>Uptime: <span class="uptime">-</span></span>
    <span class="status_dot" style="color:#2ecc71">&#9679;</span>
  </div>

  <div class="panel">
    <h3>Server Status</h3>
    <div class="cards"></div>
  </div>
{tuning_panel}{sync_panel}{utenyaa_stage_panel}{mmm_panel}{mmm_history_panel}
  <div class="panel">
    <h3>Connected Players</h3>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Name</th><th>Status</th><th>Score</th><th>Deaths</th>
          <th>IP</th><th>Idle</th><th>Egress</th><th>Action</th>
        </tr></thead>
        <tbody class="ptable"></tbody>
      </table>
    </div>
  </div>

  <div class="panel">
    <h3>Server Controls</h3>
    <div class="controls">
      <button class="btn btn-warn"   onclick="endGame('{slug}')">End Game</button>
      <button class="btn btn-danger" onclick="restartGame('{slug}')">Restart Server</button>
    </div>
  </div>

{disasteroids_history_panel}{utenyaa_history_panel}
</div>""")

    html = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Saturn Admin</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#1a1a2e;color:#e0e0e0;font-family:-apple-system,system-ui,monospace;padding:12px;font-size:14px}
h1{color:#f5a623;margin-bottom:10px;font-size:22px}
h3{font-size:15px;margin-bottom:8px}
.info{color:#888;margin-bottom:12px;font-size:13px;display:flex;gap:14px;flex-wrap:wrap;align-items:center}
.panel{background:#16213e;padding:12px;border-radius:5px;margin:8px 0}
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;margin:6px 0;min-width:280px}
th,td{padding:6px 8px;text-align:left;border-bottom:1px solid #333;white-space:nowrap;font-size:13px}
th{background:#0f1a2e;color:#f5a623}
tr:hover{background:#1a2744}
.btn{background:#f5a623;color:#000;border:none;padding:8px 16px;cursor:pointer;font-family:inherit;font-size:13px;border-radius:3px;touch-action:manipulation;font-weight:bold}
.btn:active{opacity:0.7}
.btn-warn{background:#e94560;color:#fff}
.btn-danger{background:#d32f2f;color:#fff}
.status{display:inline-block;padding:2px 8px;border-radius:3px;font-size:12px}
.status-lobby{background:#2ecc71;color:#000}
.status-ingame{background:#3498db;color:#fff}
.status-dead{background:#7f8c8d;color:#fff}
#msg{position:fixed;top:10px;left:50%;transform:translateX(-50%);background:#2ecc71;color:#000;padding:10px 20px;border-radius:5px;display:none;font-weight:bold;z-index:9}
.cards{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0}
.card{flex:1;min-width:100px;background:#0f1a2e;border-radius:4px;padding:8px;text-align:center}
.card-label{font-size:11px;color:#888;margin-bottom:2px;text-transform:uppercase;letter-spacing:.5px}
.card-value{font-size:16px;font-weight:bold;color:#e0e0e0}
.tabs{display:flex;gap:4px;margin-bottom:8px;flex-wrap:wrap}
.tab{padding:10px 18px;background:#0f1a2e;color:#888;border:none;cursor:pointer;font-family:inherit;font-size:14px;border-radius:4px 4px 0 0}
.tab.active{background:#16213e;color:#f5a623;font-weight:bold;border-bottom:2px solid #f5a623}
.tab-content{display:none}
.tab-content.active{display:block}
.controls{display:flex;gap:10px;flex-wrap:wrap}
.offline{color:#d32f2f;font-weight:bold}
.muted{color:#888;font-size:12px}
.tuning-panel h3 .tuning-badge{font-size:11px;padding:2px 6px;border-radius:3px;margin-left:8px;vertical-align:middle}
.tuning-panel .tuning-badge.b-passthrough{background:#2ecc71;color:#000}
.tuning-panel .tuning-badge.b-light{background:#3498db;color:#fff}
.tuning-panel .tuning-badge.b-moderate{background:#f39c12;color:#000}
.tuning-panel .tuning-badge.b-aggressive{background:#e94560;color:#fff}
.tuning-panel .tuning-badge.b-auto{background:#9b59b6;color:#fff}
.tuning-panel .tuning-badge.b-custom{background:#7f8c8d;color:#fff}
.sync-panel h3 .sync-badge{font-size:11px;padding:2px 6px;border-radius:3px;margin-left:8px;vertical-align:middle}
.sync-panel .sync-badge.b-lerp{background:#2ecc71;color:#000}
.sync-panel .sync-badge.b-ring{background:#9b59b6;color:#fff}
.tuning-presets{display:flex;flex-wrap:wrap;gap:12px;margin:8px 0}
.tuning-presets label{cursor:pointer;padding:6px 10px;background:#0f1a2e;border-radius:3px;font-size:13px}
.tuning-estimate{margin-top:6px;font-family:monospace;background:#0f1a2e;padding:8px;border-radius:3px}
.tuning-advanced{margin-top:8px}
.tuning-advanced summary{cursor:pointer;color:#f5a623;font-size:13px;padding:4px 0}
.tuning-knobs{display:flex;flex-direction:column;gap:8px;margin-top:8px;padding-left:12px}
.tuning-knobs label{font-size:13px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.tuning-knobs input[type=number]{width:70px;background:#0f1a2e;color:#e0e0e0;border:1px solid #333;padding:4px 6px;border-radius:3px;font-family:inherit}
.tuning-knobs input[type=number]:disabled{opacity:0.5;cursor:not-allowed}
.mmm-panel .mmm-row{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin:6px 0}
.mmm-panel .mmm-label{font-size:13px;color:#bbb}
.mmm-panel select{background:#0f1a2e;color:#e0e0e0;border:1px solid #333;padding:4px 6px;border-radius:3px;font-family:inherit}
@media(max-width:699px){body{padding:8px}}
</style></head><body>
<h1>Saturn NetLink Admin</h1>
<div id="msg"></div>

<div class="tabs">__TAB_BUTTONS__</div>

__TAB_PANELS__

<script>
var REFRESH_SEC=3;
var GAMES=__GAMES__;
var CARD_ORDER=__CARD_ORDER__;
var CARD_LABELS=__CARD_LABELS__;
var state={};
GAMES.forEach(function(g){state[g.slug]={countdown:REFRESH_SEC,uptime:0}});
var activeSlug=GAMES.length?GAMES[0].slug:null;

function $(sel,root){return (root||document).querySelector(sel)}
function $$(sel,root){return Array.prototype.slice.call((root||document).querySelectorAll(sel))}
function panel(slug){return $('#tab-'+slug)}

function showTab(slug){
  activeSlug=slug;
  $$('.tab').forEach(function(t){t.classList.toggle('active',t.getAttribute('data-slug')===slug)});
  $$('.tab-content').forEach(function(p){p.classList.toggle('active',p.getAttribute('data-slug')===slug)});
  refresh(slug);
}

function showMsg(t,c){
  var m=$('#msg');m.textContent=t;m.style.background=c||'#2ecc71';m.style.display='block';
  setTimeout(function(){m.style.display='none'},3000);
}

function api(method,slug,endpoint,body){
  var opts={method:method};
  if(body){opts.headers={'Content-Type':'application/json'};opts.body=JSON.stringify(body)}
  return fetch('api/'+slug+'/'+endpoint,opts).then(function(r){
    if(!r.ok){var e=new Error('HTTP '+r.status);e.status=r.status;throw e;}
    return r.json();
  });
}

function fmtTime(s){
  if(s==null||s<0)return'-';
  var m=Math.floor(s/60),sec=Math.floor(s%60);
  return m>0?m+'m '+sec+'s':sec+'s';
}

function prettyKey(k){
  if(CARD_LABELS[k])return CARD_LABELS[k];
  return k.replace(/_/g,' ').replace(/\\b\\w/g,function(c){return c.toUpperCase()});
}

function prettyValue(k,v){
  if(k==='active')return v?'ACTIVE':'Lobby';
  if(typeof v==='boolean')return v?'Yes':'No';
  if(v==null)return'-';
  return String(v);
}

function orderedGameKeys(g){
  var out=[];var seen={};
  CARD_ORDER.forEach(function(k){if(k in g){out.push(k);seen[k]=1}});
  Object.keys(g).sort().forEach(function(k){if(!seen[k])out.push(k)});
  return out;
}

function renderCards(slug,game){
  var wrap=$('.cards',panel(slug));
  var keys=orderedGameKeys(game||{});
  var html='';
  keys.forEach(function(k){
    html+='<div class="card"><div class="card-label">'+prettyKey(k)
         +'</div><div class="card-value">'+prettyValue(k,game[k])+'</div></div>';
  });
  if(!html)html='<div style="color:#888">(offline)</div>';
  wrap.innerHTML=html;
}

function renderPlayers(slug,players){
  var tb=$('.ptable',panel(slug));
  tb.innerHTML='';
  if(!players||!players.length){
    tb.innerHTML='<tr><td colspan="8" style="color:#888;text-align:center">No players connected</td></tr>';
    return;
  }
  players.forEach(function(p){
    var sc='status-lobby';
    if(p.status==='in-game')sc='status-ingame';
    else if(p.status==='dead')sc='status-dead';
    var bps=(p.egress_bps==null)?'-':(p.egress_bps+' B/s');
    var tr=document.createElement('tr');
    tr.innerHTML='<td><b>'+p.username+'</b></td>'
      +'<td><span class="status '+sc+'">'+p.status+'</span></td>'
      +'<td>'+(p.score||0)+'</td><td>'+(p.deaths||0)+'</td>'
      +'<td>'+p.address+'</td>'
      +'<td>'+fmtTime(p.idle)+'</td>'
      +'<td>'+bps+'</td>'
      +'<td><button class="btn" onclick="kick(\\''+slug+'\\',\\''+p.uuid+'\\',\\''+p.username.replace(/\\'/g,"")+'\\')">Kick</button></td>';
    tb.appendChild(tr);
  });
}

// ------- Tuning: fetch, render, estimate, apply -------
var tuningCache={};  // slug -> last /api/tuning payload
var tuningDirty={};  // slug -> true when user has pending (un-Applied) changes
var CEILING_BPS=1440;  // 14,400 baud / 10 bits per byte

function markTuningDirty(slug){
  tuningDirty[slug]=true;
  var tp=$('.tuning-panel',panel(slug));
  if(tp)$('.tuning-badge',tp).textContent+=' (pending — click Apply)';
}

function wireTuningInputs(slug){
  var tp=$('.tuning-panel',panel(slug));
  if(!tp||tp.__wired)return;
  tp.__wired=true;
  $$('input[name=preset-'+slug+']',tp).forEach(function(r){
    r.addEventListener('change',function(){markTuningDirty(slug);});
  });
  $$('input[data-key]',tp).forEach(function(inp){
    inp.addEventListener('change',function(){markTuningDirty(slug);});
    inp.addEventListener('input', function(){markTuningDirty(slug);});
  });
}

function renderTuning(slug,t){
  var pane=panel(slug);
  var tp=$('.tuning-panel',pane);
  if(!tp||!t||!t.tuning)return;
  wireTuningInputs(slug);
  tuningCache[slug]=t;
  var tuning=t.tuning;
  var presetShown=t.auto_mode?'AUTO':(tuning.preset||'PASSTHROUGH');
  var dirty=!!tuningDirty[slug];
  // Radio selection — skip when user has pending changes so we don't
  // overwrite an in-progress selection on the 3-second refresh.
  if(!dirty){
    $$('input[name=preset-'+slug+']',tp).forEach(function(r){
      r.checked=(r.value===presetShown);
    });
  }
  // Badge — always reflects live server state
  var badge=$('.tuning-badge',tp);
  badge.className='tuning-badge b-'+presetShown.toLowerCase();
  var badgeText;
  if(t.auto_mode && t.auto_current && t.auto_current!=='AUTO'){
    badgeText='AUTO → '+t.auto_current+' (n='+(t.player_count||0)+')';
  }else{
    badgeText=presetShown+' (n='+(t.player_count||0)+')';
  }
  if(dirty)badgeText+=' (pending — click Apply)';
  badge.textContent=badgeText;
  // Advanced knobs — skip when dirty
  if(!dirty){
    $$('input[data-key]',tp).forEach(function(inp){
      var key=inp.getAttribute('data-key');
      var v=tuning[key];
      if(inp.type==='checkbox'){inp.checked=!!v;}
      else{inp.value=(v==null?'':v);}
      inp.disabled=(presetShown!=='CUSTOM');
    });
  }else{
    // When dirty, enable all knobs so the user can edit in CUSTOM mode
    // without the UI disabling them mid-edit.
    var customSelected=false;
    $$('input[name=preset-'+slug+']',tp).forEach(function(r){
      if(r.checked&&r.value==='CUSTOM')customSelected=true;
    });
    $$('input[data-key]',tp).forEach(function(inp){
      inp.disabled=!customSelected;
    });
  }
  // Estimate — always live
  $('.tuning-estimate',tp).textContent=estimateBandwidth(t);
}

function estimateBandwidth(t){
  var n=t.player_count||0;
  var tuning=t.tuning||{};
  // Each *other* ship contributes a SHIP_SYNC stream at (15Hz / decimate)
  // Stationary skip halves the contribution on average (rough).
  var d=Math.max(1,tuning.ship_sync_decimate||1);
  var perSrc=23*(15/d);
  if(tuning.ship_sync_skip_stationary)perSrc*=0.55;
  var sources=Math.max(0,n-1);
  // Bots relay extra at 20Hz / bot_sync_every_n_ticks per bot (conservative).
  // We don't distinguish humans vs bots client-side; assume worst case.
  var bps=Math.round(perSrc*sources);
  var pct=CEILING_BPS>0?Math.round(bps*100/CEILING_BPS):0;
  var warn=pct>=100?' OVER CEILING':(pct>=85?' near ceiling':'');
  return 'Est. per-client downstream: '+bps+' B/s  ('+pct+'% of '+CEILING_BPS+warn+')';
}

function applyTuning(slug){
  var tp=$('.tuning-panel',panel(slug));
  if(!tp)return;
  var preset=null;
  $$('input[name=preset-'+slug+']',tp).forEach(function(r){if(r.checked)preset=r.value;});
  var body={};
  if(preset==='CUSTOM'){
    // Send individual knobs — server will mark state as CUSTOM.
    $$('input[data-key]',tp).forEach(function(inp){
      var key=inp.getAttribute('data-key');
      if(inp.type==='checkbox')body[key]=inp.checked;
      else body[key]=parseInt(inp.value,10);
    });
  } else if(preset){
    body.preset=preset;
  }
  api('POST',slug,'tuning',body).then(function(r){
    if(r.error){showMsg('Tuning error: '+r.error,'#d32f2f');return;}
    showMsg(r.message||'Tuning applied');
    // Clear dirty so the next refresh shows the server's view, not the user's.
    tuningDirty[slug]=false;
    refreshTuning(slug);
  }).catch(function(e){showMsg('Tuning failed: '+e.message,'#d32f2f');});
}

function applyTuningPreset(slug,preset){
  api('POST',slug,'tuning',{preset:preset}).then(function(r){
    if(r.error){showMsg('Tuning error: '+r.error,'#d32f2f');return;}
    showMsg(r.message||'Tuning applied');
    tuningDirty[slug]=false;
    refreshTuning(slug);
  }).catch(function(e){showMsg('Tuning failed: '+e.message,'#d32f2f');});
}

function refreshTuning(slug){
  var pane=panel(slug);
  if(!$('.tuning-panel',pane))return;  // other tabs have no tuning panel
  api('GET',slug,'tuning').then(function(d){
    renderTuning(slug,d);
    renderSyncMode(slug,d);
  }).catch(function(){});
}

// ------- Sync engine (LERP/RING) -------
var syncDirty={};  // slug -> true while user has unapplied selection
function wireSyncInputs(slug){
  var sp=$('.sync-panel',panel(slug));
  if(!sp||sp.__wired)return;
  sp.__wired=true;
  $$('input[name=syncmode-'+slug+']',sp).forEach(function(r){
    r.addEventListener('change',function(){syncDirty[slug]=true;
      // Visual hint matching the bandwidth panel pattern.
      var b=$('.sync-badge',sp);
      if(b)b.textContent=(b.textContent.replace(/ \\(pending.*$/,''))+' (pending — click Apply)';
    });
  });
}
function renderSyncMode(slug,d){
  var sp=$('.sync-panel',panel(slug));
  if(!sp||!d||!d.tuning)return;
  wireSyncInputs(slug);
  var mode=d.tuning.sync_mode||'LERP';
  if(!syncDirty[slug]){
    $$('input[name=syncmode-'+slug+']',sp).forEach(function(r){
      r.checked=(r.value===mode);
    });
  }
  var badge=$('.sync-badge',sp);
  if(badge){
    badge.className='sync-badge b-'+mode.toLowerCase();
    badge.textContent=mode+(syncDirty[slug]?' (pending — click Apply)':'');
  }
}
function applySyncMode(slug){
  var sp=$('.sync-panel',panel(slug));
  if(!sp)return;
  var mode=null;
  $$('input[name=syncmode-'+slug+']',sp).forEach(function(r){if(r.checked)mode=r.value;});
  if(!mode){showMsg('Pick a sync mode first','#d32f2f');return;}
  api('POST',slug,'tuning',{sync_mode:mode}).then(function(r){
    if(r.error){showMsg('Sync error: '+r.error,'#d32f2f');return;}
    showMsg('Sync mode set to '+mode);
    syncDirty[slug]=false;
    refreshTuning(slug);
  }).catch(function(e){showMsg('Sync apply failed: '+e.message,'#d32f2f');});
}
function applySyncModeDirect(slug,mode){
  api('POST',slug,'tuning',{sync_mode:mode}).then(function(r){
    if(r.error){showMsg('Sync error: '+r.error,'#d32f2f');return;}
    showMsg('Sync mode set to '+mode);
    syncDirty[slug]=false;
    refreshTuning(slug);
  }).catch(function(e){showMsg('Sync apply failed: '+e.message,'#d32f2f');});
}

function refresh(slug){
  state[slug].countdown=REFRESH_SEC;
  var pane=panel(slug);
  api('GET',slug,'state').then(function(d){
    $('.status_dot',pane).style.color='#2ecc71';
    $('.uptime',pane).textContent=fmtTime(d.uptime);
    renderCards(slug,d.game||{});
    renderPlayers(slug,d.players||[]);
    // /api/state also carries tuning for the disasteroids tab; render if present.
    if(d.tuning){
      var tWrapped={tuning:d.tuning,auto_mode:d.tuning.auto_mode,
                    auto_current:d.tuning.auto_current,
                    player_count:d.tuning.player_count};
      renderTuning(slug,tWrapped);
      renderSyncMode(slug,tWrapped);
    }
    if(slug==='mmm'){
      renderMMMTuning(slug,d);
      loadMMMJoinHistory(slug);
    }
    if(slug==='utenyaa'){
      utenyaaLoadStages(slug);
      utenyaaLoadJoinHistory(slug);
    }
    if(slug==='disasteroids'){
      loadDisasteroidsJoinHistory(slug);
    }
  }).catch(function(){
    $('.status_dot',pane).style.color='#d32f2f';
    $('.uptime',pane).textContent='offline';
    renderCards(slug,{});
    renderPlayers(slug,[]);
  });
}

// ------- MMM-specific helpers -------
var mmmDirty={};  // slug -> true while user has unapplied form changes
function wireMMMInputs(slug){
  var pane=panel(slug);
  var tp=$('.mmm-panel',pane);
  if(!tp||tp.__wired)return;
  tp.__wired=true;
  ['.mmm-rand-track','.mmm-forced-track','.mmm-lap-count','.mmm-pup-respawn']
    .forEach(function(sel){
      var el=$(sel,tp);
      if(!el)return;
      el.addEventListener('change',function(){mmmDirty[slug]=true;});
      el.addEventListener('input',function(){mmmDirty[slug]=true;});
    });
}
function renderMMMTuning(slug,d){
  var pane=panel(slug);
  var tp=$('.mmm-panel',pane);
  if(!tp)return;
  wireMMMInputs(slug);
  // Player count = humans + bots from game dict.
  var g=d.game||{};
  $('.mmm-bot-count',tp).textContent=String(g.bot_count==null?0:g.bot_count);
  if(mmmDirty[slug])return;  // do not stomp user edits mid-form
  var t=d.tuning||{};
  if($('.mmm-rand-track',tp))$('.mmm-rand-track',tp).checked=!!t.random_track;
  if($('.mmm-forced-track',tp)&&t.forced_track_id!=null)
    $('.mmm-forced-track',tp).value=String(t.forced_track_id);
  if($('.mmm-lap-count',tp)&&t.lap_count!=null)
    $('.mmm-lap-count',tp).value=String(t.lap_count);
  if($('.mmm-pup-respawn',tp)&&t.powerup_respawn_secs!=null)
    $('.mmm-pup-respawn',tp).value=String(t.powerup_respawn_secs);
}
function mmmBot(slug,direction){
  var endpoint=(direction==='add'?'add_bot':'remove_bot');
  api('POST',slug,endpoint,{}).then(function(r){
    if(r.message)showMsg(r.message);refresh(slug);
  }).catch(function(e){showMsg('Bot '+direction+' failed: '+e.message,'#d32f2f');});
}
function mmmTuneOne(slug,key,val){
  return api('POST',slug,'tune?key='+encodeURIComponent(key)+
                       '&value='+encodeURIComponent(val),{});
}
function mmmApplyTune(slug){
  var tp=$('.mmm-panel',panel(slug));
  if(!tp)return;
  var rand=$('.mmm-rand-track',tp).checked;
  var ftrack=$('.mmm-forced-track',tp).value;
  var laps=$('.mmm-lap-count',tp).value;
  var respawn=$('.mmm-pup-respawn',tp).value;
  Promise.all([
    mmmTuneOne(slug,'random_track',rand?'true':'false'),
    mmmTuneOne(slug,'forced_track_id',ftrack),
    mmmTuneOne(slug,'lap_count',laps),
    mmmTuneOne(slug,'powerup_respawn_secs',respawn)
  ]).then(function(){
    showMsg('MMM tuning applied');
    mmmDirty[slug]=false;
    refresh(slug);
  }).catch(function(e){
    showMsg('Apply failed: '+e.message,'#d32f2f');
  });
}
/* === Utenyaa stage-pool toggles ===
 * Each checkbox in the Utenyaa tab maps to a server tune key
 * (stage_enabled_island/cross/valley/railway). userver.py's admin
 * endpoint uses PATH-style: GET /api/tune returns the full dict;
 * POST /api/tune/<key> with body {"value": <bool>} updates one. */
function utenyaaLoadStages(slug){
  var pane=panel(slug);
  if(!pane)return;
  var nodes=pane.querySelectorAll('.utenyaa-stage');
  if(!nodes.length)return;
  api('GET',slug,'tune').then(function(d){
    if(!d)return;
    nodes.forEach(function(box){
      var k=box.getAttribute('data-stage-key');
      if(typeof d[k]!=='undefined')box.checked=!!d[k];
    });
  }).catch(function(){/* leave at default */});
}
function utenyaaApplyStages(slug){
  var pane=panel(slug);
  if(!pane)return;
  var nodes=pane.querySelectorAll('.utenyaa-stage');
  var status=pane.querySelector('.utenyaa-stage-status');
  if(!nodes.length)return;
  var anyChecked=false;
  nodes.forEach(function(b){if(b.checked)anyChecked=true;});
  if(!anyChecked){
    if(status){status.textContent='Need at least one stage enabled.';status.style.color='#d32f2f';}
    return;
  }
  var promises=[];
  nodes.forEach(function(box){
    var k=box.getAttribute('data-stage-key');
    promises.push(api('POST',slug,'tune/'+encodeURIComponent(k),{value:!!box.checked}));
  });
  Promise.all(promises).then(function(){
    if(status){status.textContent='Applied at '+new Date().toLocaleTimeString();status.style.color='#2ecc71';}
  }).catch(function(e){
    if(status){status.textContent='Apply failed: '+e.message;status.style.color='#d32f2f';}
  });
}
function utenyaaLoadJoinHistory(slug){
  var pane=panel(slug);
  var tb=$('.utenyaa-jh-table',pane);
  if(!tb)return;
  api('GET',slug,'join_history?limit=200').then(function(d){
    tb.innerHTML='';
    var rows=(d&&d.events)||[];
    if(!rows.length){
      tb.innerHTML='<tr><td colspan="5" style="color:#888;text-align:center">No events yet</td></tr>';
      return;
    }
    rows.forEach(function(ev){
      var tr=document.createElement('tr');
      var dt=ev.t?new Date(ev.t*1000):null;
      var when=dt?dt.toLocaleString():'-';
      var evtype=ev.event||'?';
      var color={
        'join':'#2ecc71','rejoin':'#3498db',
        'leave':'#e67e22','kick':'#e94560','timeout':'#9b59b6'
      }[evtype]||'#aaa';
      tr.innerHTML=
        '<td style="white-space:nowrap">'+when+'</td>'+
        '<td style="color:'+color+';font-weight:600">'+evtype+'</td>'+
        '<td>'+(ev.name||'')+'</td>'+
        '<td style="font-family:monospace;font-size:12px">'+(ev.ip||'')+'</td>'+
        '<td class="muted">'+(ev.reason||'')+'</td>';
      tb.appendChild(tr);
    });
  }).catch(function(){
    tb.innerHTML='<tr><td colspan="5" style="color:#d32f2f;text-align:center">offline</td></tr>';
  });
}
function mmmForceEnd(slug){
  if(!confirm('Force end the current MMM race?'))return;
  api('POST',slug,'force_end_race',{}).then(function(r){
    if(r.message)showMsg(r.message,'#e94560');refresh(slug);
  }).catch(function(e){showMsg('Force-end failed: '+e.message,'#d32f2f');});
}
function loadMMMJoinHistory(slug){
  var pane=panel(slug);
  var tb=$('.mmm-jh-table',pane);
  if(!tb)return;
  api('GET',slug,'join_history?limit=20').then(function(d){
    tb.innerHTML='';
    var rows=d.events||[];
    if(!rows.length){
      tb.innerHTML='<tr><td colspan="5" style="color:#888;text-align:center">No events yet</td></tr>';
      return;
    }
    rows.forEach(function(e){
      var tr=document.createElement('tr');
      tr.innerHTML='<td>'+(e.ts||'-')+'</td>'
        +'<td>'+(e.event||'-')+'</td>'
        +'<td>'+(e.name||'-')+'</td>'
        +'<td>'+(e.ip||'-')+'</td>'
        +'<td>'+(e.reason||'-')+'</td>';
      tb.appendChild(tr);
    });
  }).catch(function(){
    tb.innerHTML='<tr><td colspan="5" class="offline">Unavailable</td></tr>';
  });
}

function loadDisasteroidsJoinHistory(slug){
  var pane=panel(slug);
  var tb=$('.disasteroids-jh-table',pane);
  if(!tb)return;
  api('GET',slug,'join_history?limit=200').then(function(d){
    tb.innerHTML='';
    var rows=(d&&d.events)||[];
    if(!rows.length){
      tb.innerHTML='<tr><td colspan="5" style="color:#888;text-align:center">No events yet</td></tr>';
      return;
    }
    rows.forEach(function(ev){
      var tr=document.createElement('tr');
      var dt=ev.t?new Date(ev.t*1000):null;
      var when=dt?dt.toLocaleString():(ev.ts||'-');
      var evtype=ev.event||'?';
      var color={
        'join':'#2ecc71','rejoin':'#3498db',
        'leave':'#e67e22','kick':'#e94560','timeout':'#9b59b6'
      }[evtype]||'#aaa';
      tr.innerHTML=
        '<td style="white-space:nowrap">'+when+'</td>'+
        '<td style="color:'+color+';font-weight:600">'+evtype+'</td>'+
        '<td>'+(ev.name||'')+'</td>'+
        '<td style="font-family:monospace;font-size:12px">'+(ev.ip||'')+'</td>'+
        '<td class="muted">'+(ev.reason||'')+'</td>';
      tb.appendChild(tr);
    });
  }).catch(function(){
    tb.innerHTML='<tr><td colspan="5" style="color:#d32f2f;text-align:center">offline</td></tr>';
  });
}

/* loadHistory() removed: was a global per-tab dead-end that called the
 * non-existent /api/<slug>/history endpoint and showed "Not tracked on
 * this server" as a duplicate of the working per-game history panels.
 * Each game's history is now rendered via its own dedicated loader
 * (utenyaaLoadJoinHistory, loadMMMJoinHistory, loadDisasteroidsJoinHistory). */

function kick(slug,uuid,name){
  if(!confirm('Kick '+name+' from '+slug+'?'))return;
  api('POST',slug,'kick',{uuid:uuid}).then(function(r){
    if(r.message)showMsg(r.message);refresh(slug);
  });
}

function endGame(slug){
  if(!confirm('End the current game on '+slug+'?'))return;
  api('POST',slug,'end_game').then(function(r){
    if(r.message)showMsg(r.message);refresh(slug);
  });
}

function restartGame(slug){
  if(!confirm('RESTART '+slug+' server? All connections will drop.'))return;
  api('POST',slug,'restart').then(function(r){
    if(r.message)showMsg(r.message,'#e94560');
  });
}

function tick(){
  GAMES.forEach(function(g){
    state[g.slug].countdown--;
    if(g.slug===activeSlug){
      $('.countdown',panel(g.slug)).textContent=Math.max(state[g.slug].countdown,0);
      if(state[g.slug].countdown<=0)refresh(g.slug);
    }
  });
}

// Initial render for every tab so each one has dashboard data if user switches.
GAMES.forEach(function(g){refresh(g.slug);refreshTuning(g.slug);});
setInterval(tick,1000);
</script></body></html>"""
    html = (html
            .replace("__TAB_BUTTONS__", "\n".join(tab_buttons))
            .replace("__TAB_PANELS__", "\n".join(tab_panels))
            .replace("__GAMES__", games_js)
            .replace("__CARD_ORDER__", card_order_js)
            .replace("__CARD_LABELS__", card_labels_js))
    return html.encode("utf-8")


ADMIN_HTML = _render_admin_html()


class AdminHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.debug("Admin HTTP: " + fmt, *args)

    def _send(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _send_json(self, data, code=200):
        self._send(code, json.dumps(data).encode("utf-8"), "application/json")

    def _send_upstream(self, status, body):
        if status == 0:
            self._send_json({"error": "upstream offline"}, 502)
            return
        self._send(status, body or b"{}", "application/json")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parsed.query
        if path == "/" or path == "":
            self._send(200, ADMIN_HTML, "text/html; charset=utf-8")
            return
        if path == "/api/games":
            self._send_json({"games": [
                {"slug": s, "label": l, "port": p} for s, l, p in GAMES
            ]})
            return
        # /api/<slug>/<endpoint>
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "api" and parts[2] in (
                "state", "history", "tuning", "tune",
                "join_history", "leaderboard", "client_logs"):
            port = _port_for(parts[1])
            if not port:
                self.send_error(404); return
            upstream_path = "/api/" + parts[2]
            if query:
                upstream_path += "?" + query
            status, body = _upstream_get(port, upstream_path)
            self._send_upstream(status, body)
            return
        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parsed.query
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len > 0 else b""
        parts = path.strip("/").split("/")
        if (len(parts) == 3 and parts[0] == "api"
                and parts[2] in (
                    "kick", "end_game", "restart", "tuning",
                    "add_bot", "remove_bot", "force_end_race", "tune",
                    "upload_waypoints", "control")):
            port = _port_for(parts[1])
            if not port:
                self.send_error(404); return
            upstream_path = "/api/" + parts[2]
            if query:
                upstream_path += "?" + query
            status, resp = _upstream_post(port, upstream_path, body)
            self._send_upstream(status, resp)
            return
        # Path-style tune: /api/<slug>/tune/<key>  (used by Utenyaa to
        # write a single named tune knob with a JSON body {value:...}).
        if (len(parts) == 4 and parts[0] == "api"
                and parts[2] in ("tune", "kick")):
            port = _port_for(parts[1])
            if not port:
                self.send_error(404); return
            upstream_path = "/api/" + parts[2] + "/" + parts[3]
            if query:
                upstream_path += "?" + query
            status, resp = _upstream_post(port, upstream_path, body)
            self._send_upstream(status, resp)
            return
        self.send_error(404)


def main():
    parser = argparse.ArgumentParser(
        description="Saturn NetLink unified admin portal")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind address (default loopback only)")
    parser.add_argument("--port", type=int, default=9099,
                        help="HTTP port (default 9099)")
    parser.add_argument("--verbose", action="store_true",
                        help="Debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    httpd = ThreadingHTTPServer((args.host, args.port), AdminHandler)
    httpd.daemon_threads = True
    log.info("Saturn unified admin listening on http://%s:%d/",
             args.host, args.port)
    for slug, label, port in GAMES:
        log.info("  tab: %-14s -> 127.0.0.1:%d  (%s)", slug, port, label)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        httpd.shutdown()


if __name__ == "__main__":
    main()
