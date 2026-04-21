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
{tuning_panel}
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

  <div class="panel">
    <h3>Join History (Last 200)</h3>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Time</th><th>Name</th><th>IP</th><th>Event</th></tr></thead>
        <tbody class="htable"></tbody>
      </table>
    </div>
  </div>
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
.tuning-presets{display:flex;flex-wrap:wrap;gap:12px;margin:8px 0}
.tuning-presets label{cursor:pointer;padding:6px 10px;background:#0f1a2e;border-radius:3px;font-size:13px}
.tuning-estimate{margin-top:6px;font-family:monospace;background:#0f1a2e;padding:8px;border-radius:3px}
.tuning-advanced{margin-top:8px}
.tuning-advanced summary{cursor:pointer;color:#f5a623;font-size:13px;padding:4px 0}
.tuning-knobs{display:flex;flex-direction:column;gap:8px;margin-top:8px;padding-left:12px}
.tuning-knobs label{font-size:13px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.tuning-knobs input[type=number]{width:70px;background:#0f1a2e;color:#e0e0e0;border:1px solid #333;padding:4px 6px;border-radius:3px;font-family:inherit}
.tuning-knobs input[type=number]:disabled{opacity:0.5;cursor:not-allowed}
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
  loadHistory(slug);
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
  }).catch(function(){});
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
      renderTuning(slug,{tuning:d.tuning,auto_mode:d.tuning.auto_mode,
                         auto_current:d.tuning.auto_current,
                         player_count:d.tuning.player_count});
    }
  }).catch(function(){
    $('.status_dot',pane).style.color='#d32f2f';
    $('.uptime',pane).textContent='offline';
    renderCards(slug,{});
    renderPlayers(slug,[]);
  });
}

function loadHistory(slug){
  var pane=panel(slug);
  var tb=$('.htable',pane);
  api('GET',slug,'history').then(function(d){
    tb.innerHTML='';
    (d.entries||[]).forEach(function(e){
      var tr=document.createElement('tr');
      tr.innerHTML='<td>'+e.time+'</td><td>'+e.name+'</td><td>'+e.ip+'</td><td>'+e.event+'</td>';
      tb.appendChild(tr);
    });
    if(!(d.entries||[]).length){
      tb.innerHTML='<tr><td colspan="4" style="color:#888;text-align:center">No history</td></tr>';
    }
  }).catch(function(err){
    // A 404 means the per-game server doesn't expose history; not a real
    // error. Anything else (502 upstream offline, network failure, etc.)
    // is a real problem worth flagging in red.
    if(err && err.status===404){
      tb.innerHTML='<tr><td colspan="4" style="color:#888;text-align:center">Not tracked on this server</td></tr>';
    } else {
      tb.innerHTML='<tr><td colspan="4" class="offline">Unavailable</td></tr>';
    }
  });
}

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
GAMES.forEach(function(g){refresh(g.slug);loadHistory(g.slug);refreshTuning(g.slug);});
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
        path = urlparse(self.path).path
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
        if len(parts) == 3 and parts[0] == "api" and parts[2] in ("state", "history", "tuning"):
            port = _port_for(parts[1])
            if not port:
                self.send_error(404); return
            status, body = _upstream_get(port, "/api/" + parts[2])
            self._send_upstream(status, body)
            return
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len > 0 else b""
        parts = path.strip("/").split("/")
        if (len(parts) == 3 and parts[0] == "api"
                and parts[2] in ("kick", "end_game", "restart", "tuning")):
            port = _port_for(parts[1])
            if not port:
                self.send_error(404); return
            status, resp = _upstream_post(port, "/api/" + parts[2], body)
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
