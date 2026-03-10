// ── Config injected by server ──────────────────────────────────────────────
const CFG = window._BM_CFG || {};
const PRINTER_IP  = CFG.printer_ip || location.hostname;
const PORTS = (CFG.ports_conf||[]).map(e=>e.port);
if(!PORTS.length)(CFG.ports||[7002,7004,7005,7007]).forEach(p=>PORTS.push(p));
let _clientSvcList = (CFG.client_svcs||[]).map(e => Array.isArray(e)?{name:e[0],unit:e[1]}:e);
let _serverSvcList = []; // [{name, script, chroot, start, stop, log}, ...]

const WEB_VERSION = '0.5';
/* i18n shortcut — i18n.js is loaded in <head> before this script */
const t = (k, v) => window.i18n ? window.i18n.t(k, v) : k;
document.getElementById('ver-web').textContent = `v${WEB_VERSION}`;
document.getElementById('ver-client').textContent = CFG.version_monitor ? `v${CFG.version_monitor}` : '…';

// ── Embed mode (page inside <iframe>) ─────────────────────────────────────────────
const IS_EMBED = window.self !== window.top;
if(IS_EMBED) document.body.classList.add('embed-mode');

// ── Helpers ────────────────────────────────────────────────────────────────
function dot(cls){return `<span class="dot ${cls}"></span>`}

function renderEmbedChips(id, items){
  const el = document.getElementById(id);
  if(!el) return;
  // items: [{label, ok, dis, conn}]
  el.innerHTML = items.map(it => {
    const cls     = it.dis ? 'dis' : (it.ok ? 'ok' : 'err');
    const dotCls  = it.dis ? 'dot-warn' : (it.ok ? 'dot-ok' : 'dot-err');
    const connBadge = (it.ok && it.conn > 0)
      ? ` <span style="color:#58a6ff;font-size:10px">${it.conn}</span>` : '';
    return `<span class="embed-chip ${cls}">${dot(dotCls)}${_esc(String(it.label))}${connBadge}</span>`;
  }).join('');
}

function fmtSince(s){
  if(!s) return '—';
  const d = new Date(s.replace(' ','T'));
  if(isNaN(d)) return s;
  const dd = String(d.getDate()).padStart(2,'0');
  const mo = String(d.getMonth()+1).padStart(2,'0');
  const hh = String(d.getHours()).padStart(2,'0');
  const mn = String(d.getMinutes()).padStart(2,'0');
  const ss = String(d.getSeconds()).padStart(2,'0');
  return `<span class="since-date">${dd}/${mo} </span>${hh}:${mn}:${ss}`;
}

function fmtPid(p){return (!p||p=='0'||p===0)?'—':String(p)}

function fmtState(state){
  const map={active:'st_running',inactive:'st_stopped',activating:'st_starting',
             deactivating:'st_stopping',failed:'st_failed',unknown:'st_unknown'};
  const key = map[state];
  return key ? t(key) : state;
}

function stateClass(state){
  if(state==='running'||state==='active') return 'ok';
  if(state==='failed') return 'err';
  if(state==='starting'||state==='stopping') return 'warn';
  return 'err';
}

function fmtUptimeBar(uptimeStr, loadavg, mem){
  const load = loadavg ? `  ⚡️ load: ${loadavg.split(' ').slice(0,3).join(' ')}` : '';
  const memStr = mem && mem.total_mb ? `  💾 mem: ${mem.used_mb}/${mem.total_mb} MB` : '';
  return `⏱️ up: ${uptimeStr||'…'}${load}${memStr}`;
}

function showToast(msg, type='ok', ms=3500){
  clearTimeout(_toastTimer);
  const t=document.getElementById('toast');
  t.textContent=msg;
  t.className='show '+(type==='ok'?'ok-toast':type==='err'?'err-toast':'busy-toast');
  _toastTimer=setTimeout(()=>{ t.className=''; },ms);
}

// ── API calls ──────────────────────────────────────────────────────────────
async function apiFetch(url, opts={}){
  const r = await fetch(url, opts);
  if(!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

async function apiPost(url, params){
  const body = Object.entries(params).map(([k,v])=>`${k}=${encodeURIComponent(v)}`).join('&');
  return apiFetch(url, {method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body});
}

async function apiPostJSON(url, data){
  return apiFetch(url, {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify(data),
  });
}

// ── Ports config ───────────────────────────────────────────────────────────
let _portsList = []; // [{port, enabled}, ...] — single source of truth

function portsRenderTable(){
  const tbody = document.getElementById('ports-rows');
  tbody.innerHTML = _portsList.map((e,i) => `<tr>
    <td>${e.port}</td>
    <td style="text-align:center"><input type="checkbox" ${e.enabled?'checked':''}
      onchange="portsToggle(${i},this.checked)"></td>
    <td><button class="btn btn-stop" onclick="portsRemove(${i})">&#215;</button></td>
  </tr>`).join('');
}

function portsToggle(idx, val){
  if(_portsList[idx]) _portsList[idx].enabled = val;
}

function portsRemove(idx){
  _portsList.splice(idx, 1);
  portsRenderTable();
}

function portsAddRow(){
  const portEl = document.getElementById('new-port');
  const enEl   = document.getElementById('new-en');
  const port   = parseInt(portEl.value);
  if(!port || port < 1024 || port > 65535){ showToast(t('toast_invalid_port'), 'err'); return; }
  if(_portsList.find(e=>e.port===port)){ showToast(t('toast_port_exists'), 'err'); return; }
  _portsList.push({port, enabled: enEl.checked});
  portEl.value = '';
  portsRenderTable();
}

async function portsSave(){
  const statusEl = document.getElementById('ports-save-status');
  disableButtons();
  showToastPersist(t('toast_saving_ports'));
  statusEl.textContent = t('saving');
  try {
    const [rc, rs] = await Promise.allSettled([
      apiPostJSON('/api/ports-config',        _portsList),
      apiPostJSON('/api/server-ports-config', _portsList),
    ]);
    const cOk = rc.status==='fulfilled' && rc.value?.ok;
    const sOk = rs.status==='fulfilled' && rs.value?.ok;
    if(cOk && sOk){
      showToast(t('toast_saved_both'), 'ok');
      statusEl.textContent = t('toast_saved_both');
    } else {
      const msg = `client:${cOk?'ok':'ERR'}  server:${sOk?'ok':'ERR'}`;
      showToast(msg, 'err');
      statusEl.textContent = msg;
    }
    // Rebuild PORTS from all ports; stats total counts only enabled
    PORTS.length = 0;
    _portsList.forEach(e => PORTS.push(e.port));
    _cStats.total = _portsList.filter(e=>e.enabled).length || PORTS.length;
    _sStats.total = _cStats.total;
    scheduleClient(800);
    scheduleServer(800);
  } catch(e){
    showToast('Error: '+e.message, 'err');
    statusEl.textContent = 'error';
  } finally {
    enableButtons();
  }
}

async function portsLoadBoth(){
  // Load from client as single source of truth
  try {
    const d = await apiFetch('/api/ports-config');
    _portsList = d.ports || [];
    portsRenderTable();
  } catch(e){ console.warn('ports load:', e); }
}


async function openLog(type, param, name){
  document.getElementById('logmodal').classList.add('show');
  document.getElementById('log-content').textContent='Loading…';
  let title='', url='';
  if(type==='client-action'){
    title = t('log_client_action');
    url='/api/logs/client-action';
  } else if(type==='client-bridge'){
    title = t('log_client_bridge', {port: param});
    url=`/api/logs/client-bridge?port=${param}`;
  } else if(type==='client-svc'){
    title = t('log_client_svc', {name});
    url=`/api/logs/client-svc?unit=${encodeURIComponent(param)}`;
  } else if(type==='action'){
    title = t('log_server_action');
    url='/api/logs/action';
  } else if(type==='server-svc'){
    title = t('log_server_svc', {name});
    url=`/api/logs/server-svc?name=${name}`;
  } else if(type==='server-bridge'){
    title = t('log_server_bridge', {port: param});
    url=`/api/logs/server-bridge?port=${param}`;
  } else if(type==='server-tcpfwd'){
    title = t('log_server_tcpfwd', {port: param});
    url=`/api/logs/server-tcpfwd?port=${param}`;
  }
  document.getElementById('log-title').textContent=title;
  try {
    const d = await apiFetch(url);
    document.getElementById('log-content').textContent=(d.lines||d.content||[]).join('\n')||t('log_no_log');
  } catch(e){
    document.getElementById('log-content').textContent=t('toast_error',{msg:e.message});
  }
}
function closeLog(){document.getElementById('logmodal').classList.remove('show')}
document.getElementById('logmodal').addEventListener('click',e=>{
  if(e.target===document.getElementById('logmodal')) closeLog();
});

function openPortsModal(){
  portsRenderTable();
  document.getElementById('portsmodal').classList.add('show');
}
function closePortsModal(){document.getElementById('portsmodal').classList.remove('show')}
document.getElementById('portsmodal').addEventListener('click',e=>{
  if(e.target===document.getElementById('portsmodal')) closePortsModal();
});

// ── Unified action runner ──────────────────────────────────────────────────
let _toastTimer = null;

function showToastPersist(msg, type='busy'){
  clearTimeout(_toastTimer);
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'show ' + (type==='ok'?'ok-toast':type==='err'?'err-toast':'busy-toast');
}

async function runAction(postFn, label, side='both', refreshDelay=500){
  disableButtons();
  showToastPersist(`⚙ ${label}…`);
  try {
    const d = await postFn();
    if(d && d.async){
      pollUntilDone(label, side);
    } else {
      showToast(d.msg || `${label}: done`, 'ok');
      enableButtons();
      if(side==='client'||side==='both') scheduleClient(refreshDelay);
      if(side==='server'||side==='both') scheduleServer(refreshDelay);
    }
  } catch(e){
    showToast(`Error: ${e.message}`, 'err');
    enableButtons();
  }
}

async function pollUntilDone(label, side='server'){
  let attempts = 0;
  async function poll(){
    attempts++;
    try {
      const d = await apiFetch('/api/logs/action?n=6');
      const lines = (d.lines||[]);
      const last = lines[lines.length-1]||'';
      if(last.includes('=== done ===') || attempts > 60){
        showToast(t('toast_done', {label}), 'ok');
        enableButtons();
        if(side==='client'||side==='both') scheduleClient(0);
        if(side==='server'||side==='both') scheduleServer(0);
        return;
      }
      const progress = lines.filter(l=>l.trim()&&!l.startsWith('===')).pop()||'…';
      showToastPersist(`⚙ ${label}: ${progress.slice(0,55)}`);
    } catch(e){}
    setTimeout(poll, 2000);
  }
  poll();
}

// ── Client bridge actions ──────────────────────────────────────────────────
async function clientAction(port, action){
  await runAction(()=>apiPost('/api/client-action',{port,action}), `${action} bridge:${port}`, 'client', 500);
}
async function clientAllAction(action){
  await runAction(()=>apiPost('/api/client-action',{port:'all',action}), `${action} all clients`, 'client', 500);
}
async function clientSvcAction(unit, action){
  await runAction(()=>apiPost('/api/client-svc-action',{unit,action}), `${action} ${unit}`, 'client', 500);
}

// ── Server actions ─────────────────────────────────────────────────────────
async function serverAction(action){
  await runAction(()=>apiPost('/api/server-action',{action}), `server: ${action}`, 'server');
  // refresh mode button after switch actions
  if(action.startsWith('switch-')) scheduleServer(800);
}

function _renderModeBar(){
  const wrap = document.getElementById('mode-btn-wrap');
  const icon = document.getElementById('st-mode-icon');
  const lbl  = document.getElementById('st-mode-lbl');
  if(_currentMode === 'local'){
    if(icon) icon.textContent = '🖨';
    if(lbl)  lbl.textContent  = t('mode_local');
    if(wrap) wrap.innerHTML = `<button class="btn btn-mode-remote" onclick="serverAction('switch-remote')">&#x1F4E1;&nbsp;${t('mode_to_remote')}</button>`;
  } else if(_currentMode === 'remote'){
    if(icon) icon.textContent = '📡';
    if(lbl)  lbl.textContent  = t('mode_remote');
    if(wrap) wrap.innerHTML = `<button class="btn btn-mode-local" onclick="serverAction('switch-local')">&#x1F5A8;&nbsp;${t('mode_to_local')}</button>`;
  } else {
    if(icon) icon.textContent = '?';
    if(lbl)  lbl.textContent  = t('lbl_mode');
    if(wrap) wrap.innerHTML = `
      <button class="btn btn-mode-local"  onclick="serverAction('switch-local')" style="margin-bottom:4px">&#x1F5A8;&nbsp;${t('mode_local')}</button>
      <button class="btn btn-mode-remote" onclick="serverAction('switch-remote')">&#x1F4E1;&nbsp;${t('mode_remote')}</button>`;
  }
}

// ── Render helpers  (side = 'client' | 'server') ──────────────────────────
function renderBridgeRow(port, data, side){
  const n   = String(port).slice(-1);
  const dev = side==='client' ? `/dev/ttyV${n}` : `/dev/ttyS${n}`;

  // Check if port is disabled in ports config
  const portCfg = _portsList.find(e => e.port === port);
  const cfgDisabled = portCfg && !portCfg.enabled;
  if(cfgDisabled){
    const logType = side==='client' ? 'client-bridge' : 'server-bridge';
    return `<tr style="opacity:.45">
    <td>${port}</td>
    <td class="muted col-pty">${dev}</td>
    <td class="muted col-center">${dot('dot-err')}<span class="st-txt">${t('st_disabled')}</span></td>
    <td class="muted col-center">—</td>
    <td class="muted col-center">—</td>
    <td class="muted">—</td>
    <td class="muted" style="font-size:11px">${t('st_disabled')}</td>
    <td><a href="#" onclick="openLog('${logType}',${port},'');return false">${t('th_log')}</a></td>
  </tr>`;
  }

  const ok  = side==='client' ? (data.ok||false) : (data.running||false);
  const rawState = side==='client' ? (data.active||'unknown') : (ok?'active':'inactive');
  const state = side==='client' ? fmtState(rawState) : (ok?t('st_running'):t('st_stopped'));
  const sCls  = ok ? stateClass(rawState) : 'err';
  let connTd;
  if(!ok){
    connTd = `<span class="muted">—</span>`;
  } else if(side==='client'){
    connTd = data.connected
      ? `<span class="dot dot-conn"></span><span class="ok">yes</span>`
      : `<span class="dot dot-err"></span><span class="muted">no</span>`;
  } else {
    const c = data.connected||0;
    connTd = c>0
      ? `<span class="dot dot-conn"></span><span class="ok">${c}</span>`
      : `<span class="dot dot-err"></span><span class="muted">0</span>`;
  }
  const stopFn    = side==='client' ? `clientAction(${port},'stop')`    : `serverAction('stop-port-${port}')`;
  const restartFn = side==='client' ? `clientAction(${port},'restart')` : `serverAction('restart-port-${port}')`;
  const startFn   = side==='client' ? `clientAction(${port},'start')`   : `serverAction('start-port-${port}')`;
  const dis = _actionBusy ? ' disabled' : '';
  const ctrl = ok
    ? `<button class="btn btn-stop"${dis} onclick="${stopFn}">■</button>
       <button class="btn btn-restart"${dis} onclick="${restartFn}">↺</button>`
    : `<button class="btn btn-start"${dis} onclick="${startFn}">▶</button>`;
  const logType = side==='client' ? 'client-bridge' : 'server-bridge';
  return `<tr>
    <td>${port}</td>
    <td class="muted col-pty">${dev}</td>
    <td class="${sCls} col-center">${dot(ok?'dot-ok':'dot-err')}<span class="st-txt">${state}</span></td>
    <td class="muted col-center">${fmtPid(data.pid)}</td>
    <td class="muted col-center" style="font-size:12px">${fmtSince(data.since)}</td>
    <td>${connTd}</td>
    <td style="text-align:center">${ctrl}</td>
    <td><a href="#" onclick="openLog('${logType}',${port},'');return false">${t('th_log')}</a></td>
  </tr>`;
}

// unit only needed for client side
function renderSvcRow(name, data, side, unit=''){
  const ok    = side==='client' ? (data.ok||false) : (data.running||false);
  const rawState = side==='client' ? (data.active||'unknown') : (ok?'active':'inactive');
  const state = side==='client' ? fmtState(rawState) : (ok?t('st_running'):t('st_stopped'));
  const sCls  = ok ? stateClass(rawState) : 'err';
  const stopFn    = side==='client' ? `clientSvcAction('${unit}','stop')`    : `serverAction('stop-svc-${name}')`;
  const restartFn = side==='client' ? `clientSvcAction('${unit}','restart')` : `serverAction('restart-svc-${name}')`;
  const startFn   = side==='client' ? `clientSvcAction('${unit}','start')`   : `serverAction('start-svc-${name}')`;
  const dis = _actionBusy ? ' disabled' : '';
  const ctrl = ok
    ? `<button class="btn btn-stop"${dis} onclick="${stopFn}">■</button>
       <button class="btn btn-restart"${dis} onclick="${restartFn}">↺</button>`
    : `<button class="btn btn-start"${dis} onclick="${startFn}">▶</button>`;
  const logType  = side==='client' ? 'client-svc' : 'server-svc';
  const logParam = side==='client' ? unit : name;
  return `<tr>
    <td>${name}</td>
    <td class="${sCls} col-center">${dot(ok?'dot-ok':'dot-err')}<span class="st-txt">${state}</span></td>
    <td class="muted col-center">${fmtPid(data.pid)}</td>
    <td class="muted col-center" style="font-size:12px">${fmtSince(data.since)}</td>
    <td style="text-align:center">${ctrl}</td>
    <td><a href="#" onclick="openLog('${logType}','${logParam}','${name}');return false">${t('th_log')}</a></td>
  </tr>`;
}

// ── Main refresh ───────────────────────────────────────────────────────────
let _lastClientData = null;
let _lastServerData = null;
let _currentMode = 'unknown';  // 'local' | 'remote' | 'unknown'
let _lastUpdateTs   = 0;
let _clientTimer  = null;
let _serverTimer  = null;
let _clientBusy   = false;
let _serverBusy   = false;
let _actionBusy   = false;
let _clientOk     = true;
let _serverOk     = true;
let _cStats = {cActive:0, cConn:0, total: (CFG.ports_conf||[]).filter(e=>e.enabled).length || PORTS.length};
let _sStats = {sActive:0, sConn:0, total: _cStats.total};

function _setCardOffline(cardId, errId, offline, msg){
  const card = document.getElementById(cardId);
  const err  = document.getElementById(errId);
  if(card) card.classList.toggle('card-offline', offline);
  if(err){ err.style.display = offline ? 'block' : 'none'; if(msg) err.textContent = msg; }
}

function _updateCfgBarState(){
  const offline = !_clientOk || !_serverOk;
  if(!_actionBusy)
    document.querySelectorAll('.cfg-bar .btn').forEach(b => b.disabled = offline);
}

async function refreshClient(){
  try {
    const d = await apiFetch('/api/client-status');
    _lastClientData = d;
    _clientOk = true;
    _setCardOffline('client-card', 'client-api-err-block', false);
    _updateCfgBarState();
    document.getElementById('client-tag').textContent = `${d.hostname||'host'} — ${d.ip||'?'}`;

    // bridge rows
    let rows='';
    let cActive=0, cConn=0;
    for(const port of PORTS){
      const st = (d.bridges||{})[port]||{};
      if(st.ok) cActive++;
      if(st.connected) cConn++;
      rows += renderBridgeRow(port, st, 'client');
    }
    document.getElementById('client-rows').innerHTML = rows||`<tr><td colspan="8" class="muted">${t('no_data')}</td></tr>`;

    // svc rows
    let svcRows='';
    for(const {name, unit} of _clientSvcList){
      const st = (d.services||{})[unit]||{active:'unknown',pid:'',since:'',ok:false};
      svcRows += renderSvcRow(name, st, 'client', unit);
    }
    document.getElementById('client-svc-rows').innerHTML = svcRows;

    // uptime
    document.getElementById('client-uptime').textContent = fmtUptimeBar(d.uptime, d.loadavg, d.mem);

    if(IS_EMBED){
      renderEmbedChips('client-embed-bridges', PORTS.map(port => {
        const st = (d.bridges||{})[port]||{};
        const cfg = _portsList.find(e=>e.port===port);
        return {label: port, ok: st.ok||false, dis: cfg&&!cfg.enabled, conn: st.connected?1:0};
      }));
      renderEmbedChips('client-embed-svcs', _clientSvcList.map(({name, unit}) => {
        const st = (d.services||{})[unit]||{};
        return {label: name, ok: st.ok||false};
      }));
    }

    return {cActive, cConn, total:PORTS.length, ok:true};
  } catch(e){
    _clientOk = false;
    _setCardOffline('client-card', 'client-api-err-block', true, `⚠ Host API unavailable: ${e.message}`);
    _updateCfgBarState();
    return {cActive:0, cConn:0, total:PORTS.length, ok:false};
  }
}

async function refreshServer(){
  try {
    const d = await apiFetch('/api/server-status');
    _lastServerData = d;
    _currentMode = d.mode || 'remote';
    _renderModeBar();
    const apiOk = d.ok !== false;
    _serverOk = apiOk;
    _setCardOffline('server-card', 'api-err-block', !apiOk,
      apiOk ? '' : '⚠ Printer API unavailable');
    _updateCfgBarState();
    const cfgIp = PRINTER_IP;
    const apiIp  = d.ip || '';
    const ipStr  = apiIp && apiIp !== cfgIp ? `${cfgIp} / ${apiIp}` : cfgIp;
    document.getElementById('server-tag').textContent = `${d.hostname||'printer'} — ${ipStr}`;
    if(d.version) document.getElementById('ver-server').textContent = `v${d.version}`;

    // bridge rows
    let rows='';
    let sActive=0, sConn=0;
    for(const port of PORTS){
      const info = (d.bridges||{})[String(port)]||{};
      if(info.running) sActive++;
      sConn += (info.connected||0);
      rows += renderBridgeRow(port, info, 'server');
    }
    document.getElementById('server-rows').innerHTML = rows;

    // svc rows — keys come from API, no hardcoding
    let svcRows='';
    for(const [name, info] of Object.entries(d.services||{})){
      svcRows += renderSvcRow(name, info, 'server');
    }
    document.getElementById('server-svc-rows').innerHTML = svcRows;

    // uptime
    document.getElementById('server-uptime').textContent = fmtUptimeBar((d.uptime||{}).pretty, d.loadavg, d.mem);

    renderTcpFwdStatusRows(d.tcp_fwds || {});

    if(IS_EMBED){
      renderEmbedChips('server-embed-bridges', PORTS.map(port => {
        const info = (d.bridges||{})[String(port)]||{};
        const cfg  = _portsList.find(e=>e.port===port);
        return {label: port, ok: info.running||false, dis: cfg&&!cfg.enabled, conn: info.connected||0};
      }));
      renderEmbedChips('server-embed-tcpfwds', Object.values(d.tcp_fwds||{}).map(e => ({
        label: e.name, ok: e.running||false, dis: !e.enabled, conn: e.connected||0
      })));
      renderEmbedChips('server-embed-svcs', Object.entries(d.services||{}).map(([name, info]) => ({
        label: name, ok: info.running||false
      })));
    }

    return {sActive, sConn, total:PORTS.length, ok:apiOk};
  } catch(e){
    _serverOk = false;
    _setCardOffline('server-card', 'api-err-block', true, `⚠ Printer API unavailable: ${e.message}`);
    _updateCfgBarState();
    return {sActive:0, sConn:0, total:PORTS.length, ok:false};
  }
}

function updateStats(cStats, sStats){
  if(cStats) _cStats = cStats;
  if(sStats) _sStats = sStats;
  const total = _cStats.total;
  const setNum = (id, n, t) => {
    const el = document.getElementById(id);
    el.textContent = `${n}/${t}`;
    el.className = 'num ' + (n===t?'ok':n>0?'warn':'err');
  };
  setNum('st-clients', _cStats.cActive, total);
  setNum('st-servers', _sStats.sActive, total);
  const connEl = document.getElementById('st-conn');
  const minConn = Math.min(_cStats.cConn, _sStats.sConn);
  connEl.textContent = `${_cStats.cConn}/${_sStats.sConn}`;
  connEl.className = 'num ' + (minConn===total?'ok':minConn>0?'warn':'muted');
  // only advance the "last updated" timestamp on a real successful response
  if((cStats && cStats.ok) || (sStats && sStats.ok))
    _lastUpdateTs = Date.now();
}

async function doRefreshClient(){
  if(_clientBusy) return;
  _clientBusy = true;
  try {
    const stats = await refreshClient();
    updateStats(stats, null);
  } finally {
    _clientBusy = false;
    if(_actionBusy) document.querySelectorAll('.btn').forEach(b=>b.disabled=true);
    scheduleClient();
  }
}

async function doRefreshServer(){
  if(_serverBusy) return;
  _serverBusy = true;
  try {
    const stats = await refreshServer();
    updateStats(null, stats);
  } finally {
    _serverBusy = false;
    if(_actionBusy) document.querySelectorAll('.btn').forEach(b=>b.disabled=true);
    scheduleServer();
  }
}

function scheduleClient(ms = 5000){
  clearTimeout(_clientTimer);
  _clientTimer = setTimeout(doRefreshClient, ms);
}

function scheduleServer(ms = 5000){
  clearTimeout(_serverTimer);
  _serverTimer = setTimeout(doRefreshServer, ms);
}

function disableButtons(){
  _actionBusy = true;
  document.querySelectorAll('.btn').forEach(b=>b.disabled=true);
}
function enableButtons(){
  _actionBusy = false;
  document.querySelectorAll('.btn').forEach(b=>b.disabled=false);
}

// ── Client services config ─────────────────────────────────────────────────
function _esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;'); }

function csvcRenderTable(){
  const tbody = document.getElementById('csvc-rows');
  tbody.innerHTML = _clientSvcList.map((e,i) => `<tr>
    <td><input class="svc-input" style="width:90px" value="${_esc(e.name)}" onchange="csvcSet(${i},'name',this.value)"></td>
    <td><input class="svc-input" style="width:175px" value="${_esc(e.unit)}" onchange="csvcSet(${i},'unit',this.value)"></td>
    <td><button class="btn btn-stop" onclick="csvcRemove(${i})">&#215;</button></td>
  </tr>`).join('');
}
function csvcSet(i,k,v){ if(_clientSvcList[i]) _clientSvcList[i][k]=v; }
function csvcRemove(i){ _clientSvcList.splice(i,1); csvcRenderTable(); }
function csvcAddRow(){
  const n=document.getElementById('csvc-new-name').value.trim();
  const u=document.getElementById('csvc-new-unit').value.trim();
  if(!n||!u){ showToast(t('toast_name_unit_req'),'err'); return; }
  _clientSvcList.push({name:n,unit:u});
  document.getElementById('csvc-new-name').value='';
  document.getElementById('csvc-new-unit').value='';
  csvcRenderTable();
}
async function csvcSave(){
  const st=document.getElementById('csvc-save-status');
  st.textContent=t('saving');
  try {
    const d=await apiPostJSON('/api/client-services-config',_clientSvcList);
    if(d.ok){ _clientSvcList=d.services||_clientSvcList; st.textContent=t('saved'); showToast(t('toast_client_svcs_saved'),'ok'); scheduleClient(500); }
    else { st.textContent='ERR: '+(d.error||'?'); showToast(t('toast_error',{msg:d.error||'?'}),'err'); }
  } catch(e){ st.textContent=t('load_error'); showToast(t('toast_error',{msg:e.message}),'err'); }
}
async function openCsvcModal(){
  document.getElementById('csvc-rows').innerHTML=`<tr><td colspan="3" class="muted">${t('loading')}</td></tr>`;
  document.getElementById('csvc-modal').classList.add('show');
  try { const d=await apiFetch('/api/client-services-config'); _clientSvcList=d.services||_clientSvcList; } catch(e){}
  csvcRenderTable();
}
function closeCsvcModal(){ document.getElementById('csvc-modal').classList.remove('show'); }
document.getElementById('csvc-modal').addEventListener('click',e=>{
  if(e.target===document.getElementById('csvc-modal')) closeCsvcModal();
});

// ── Server services config ─────────────────────────────────────────────────
let _ssvcExpanded = new Set();

function ssvcRenderList(){
  const container = document.getElementById('ssvc-list');
  if(!container) return;
  container.innerHTML = _serverSvcList.map((e,i) => {
    const open = _ssvcExpanded.has(i);
    const scriptTail = (e.script||'').split('/').pop();
    return `<div class="ssvc-item" id="ssvc-item-${i}">
  <div class="ssvc-item-header" onclick="ssvcToggle(${i})">
    <span class="ssvc-item-arrow">${open?'▼':'▶'}</span>
    <span class="ssvc-item-name">${_esc(e.name||'(new)')}</span>
    <span class="ssvc-item-script">${_esc(scriptTail)}</span>
    <button class="btn btn-stop ssvc-del" onclick="ssvcRemove(${i});event.stopPropagation()">&#215;</button>
  </div>
  <div class="ssvc-item-body"${open?'':' style="display:none"'}>
    <div class="ssvc-field"><label>Name</label>
      <input class="svc-input" value="${_esc(e.name||'')}" oninput="ssvcSet(${i},'name',this.value);ssvcUpdateHeader(${i})"></div>
    <div class="ssvc-field"><label>Script</label>
      <input class="svc-input" value="${_esc(e.script||'')}" placeholder="/opt/config/mod/.shell/root/Sxx" oninput="ssvcSet(${i},'script',this.value);ssvcUpdateHeader(${i})"></div>
    <div class="ssvc-field"><label>Pre-start</label>
      <input class="svc-input" value="${_esc(e.pre_start||'')}" placeholder="optional cmd before start" onchange="ssvcSet(${i},'pre_start',this.value||null)"></div>
    <div class="ssvc-field"><label>Mode switch</label>
      <select class="svc-input" onchange="ssvcSet(${i},'local_mode',this.value||null)">
        <option value=""    ${!e.local_mode            ?'selected':''}>\u2014 no action</option>
        <option value="start" ${e.local_mode==='start'  ?'selected':''}>\u25b6 Start on Local / Stop on Remote</option>
        <option value="stop"  ${e.local_mode==='stop'   ?'selected':''}>\u23f9 Stop on Local / Start on Remote</option>
      </select></div>
    <div class="ssvc-field"><label>Log path</label>
      <input class="svc-input" value="${_esc(e.log||'')}" placeholder="optional" onchange="ssvcSet(${i},'log',this.value||null)"></div>
    <div class="ssvc-fields-row">
      <div class="ssvc-field-sm"><label>Start verb</label>
        <input class="svc-input ssvc-verb" value="${_esc(e.start||'start')}" onchange="ssvcSet(${i},'start',this.value)"></div>
      <div class="ssvc-field-sm"><label>Stop verb</label>
        <input class="svc-input ssvc-verb" value="${_esc(e.stop||'stop')}" onchange="ssvcSet(${i},'stop',this.value)"></div>
      <div class="ssvc-field-chk"><input type="checkbox" id="ssvc-chr-${i}" ${e.chroot?'checked':''} onchange="ssvcSet(${i},'chroot',this.checked)">
        <label for="ssvc-chr-${i}">Chroot</label></div>
    </div>
    <div class="ssvc-fields-row">
      <div class="ssvc-field-chk"><input type="checkbox" id="ssvc-sos-${i}" ${e.stop_on_start?'checked':''} onchange="ssvcSet(${i},'stop_on_start',this.checked)">
        <label for="ssvc-sos-${i}">⏹ Stop on API start</label></div>
      <div class="ssvc-field-chk"><input type="checkbox" id="ssvc-sas-${i}" ${e.start_on_start?'checked':''} onchange="ssvcSet(${i},'start_on_start',this.checked)">
        <label for="ssvc-sas-${i}">▶ Start on API start</label></div>
    </div>
  </div>
</div>`;
  }).join('');
}

function ssvcUpdateHeader(i){
  const item = document.getElementById(`ssvc-item-${i}`);
  if(!item) return;
  const e = _serverSvcList[i];
  const nameEl = item.querySelector('.ssvc-item-name');
  const scriptEl = item.querySelector('.ssvc-item-script');
  if(nameEl) nameEl.textContent = e.name||'(new)';
  if(scriptEl) scriptEl.textContent = (e.script||'').split('/').pop();
}

function ssvcToggle(i){
  if(_ssvcExpanded.has(i)) _ssvcExpanded.delete(i); else _ssvcExpanded.add(i);
  ssvcRenderList();
}
function ssvcSet(i,k,v){ if(_serverSvcList[i]) _serverSvcList[i][k]=v; }
function ssvcRemove(i){
  _serverSvcList.splice(i,1);
  _ssvcExpanded.clear();
  ssvcRenderList();
}
function ssvcAddNew(){
  const idx = _serverSvcList.length;
  _serverSvcList.push({name:'',script:'',chroot:true,start:'start',stop:'stop',
    stop_on_start:false,start_on_start:false,pre_start:null,local_mode:null,log:null});
  _ssvcExpanded.add(idx);
  ssvcRenderList();
  setTimeout(()=>{ const el=document.getElementById(`ssvc-item-${idx}`); if(el) el.scrollIntoView({behavior:'smooth',block:'nearest'}); },50);
}
async function ssvcSave(){
  const st=document.getElementById('ssvc-save-status');
  st.textContent=t('saving');
  try {
    const d=await apiPostJSON('/api/server-services-config',_serverSvcList);
    if(d.ok){ _serverSvcList=d.services||_serverSvcList; st.textContent=t('saved'); showToast(t('toast_server_svcs_saved'),'ok'); scheduleServer(500); }
    else { st.textContent='ERR: '+(d.error||'?'); showToast(t('toast_error',{msg:d.error||'?'}),'err'); }
  } catch(e){ st.textContent=t('load_error'); showToast(t('toast_error',{msg:e.message}),'err'); }
}
async function openSsvcModal(){
  const container = document.getElementById('ssvc-list');
  if(container) container.innerHTML=`<div class="muted" style="padding:8px">${t('loading')}</div>`;
  document.getElementById('ssvc-modal').classList.add('show');
  try { const d=await apiFetch('/api/server-services-config'); _serverSvcList=d.services||_serverSvcList; } catch(e){}
  _ssvcExpanded.clear();
  ssvcRenderList();
}
function closeSsvcModal(){ document.getElementById('ssvc-modal').classList.remove('show'); }
document.getElementById('ssvc-modal').addEventListener('click',e=>{
  if(e.target===document.getElementById('ssvc-modal')) closeSsvcModal();
});

// ── TCP Port Forwards ─────────────────────────────────────────────────────────
let _tcpFwdAll    = [];  // [{name, src_port, dst_port, enabled}, ...]
let _tcpFwdStatus = {};  // {src_port_str: {running, pid, since, connected, ...}}

function renderTcpFwdStatusRows(fwds){
  const section = document.getElementById('tcpfwd-status-section');
  const entries = Object.values(fwds);
  if(!entries.length){ if(section) section.style.display='none'; return; }
  if(section) section.style.display='';
  const tbody = document.getElementById('tcpfwd-status-rows');
  if(!tbody) return;
  const dis = _actionBusy ? ' disabled' : '';
  tbody.innerHTML = entries.map(e => {
    const ok   = e.running;
    const conn = e.connected || 0;
    const connTd = ok
      ? (conn > 0
          ? `${dot('dot-conn')}<span class="ok">${conn}</span>`
          : `${dot('dot-err')}<span class="muted">0</span>`)
      : `<span class="muted">—</span>`;
    const ctrl = ok
      ? `<button class="btn btn-stop"${dis} onclick="serverAction('stop-tcpfwd-${e.src_port}')">&#9632;</button>
         <button class="btn btn-restart"${dis} onclick="serverAction('start-tcpfwd-${e.src_port}')">&#8635;</button>`
      : `<button class="btn btn-start"${dis} onclick="serverAction('start-tcpfwd-${e.src_port}')">&#9654;</button>`;
    const sCls = ok ? 'ok' : (e.enabled ? 'err' : 'muted');
    const lbl  = ok ? t('st_running') : (e.enabled ? t('st_stopped') : t('st_disabled'));
    return `<tr>
      <td>${_esc(e.name)}</td>
      <td class="muted">${e.src_port}&rarr;${e.dst_port}</td>
      <td class="${sCls} col-center">${dot(ok?'dot-ok':'dot-err')}<span class="st-txt">${lbl}</span></td>
      <td class="muted col-center">${fmtPid(e.pid)}</td>
      <td class="muted col-center" style="font-size:12px">${fmtSince(e.since)}</td>
      <td>${connTd}</td>
      <td style="text-align:center">${ctrl}</td>
      <td><a href="#" onclick="openLog('server-tcpfwd',${e.src_port},'');return false">${t('th_log')}</a></td>
    </tr>`;
  }).join('');
}

function renderTcpFwdModalRows(){
  const tbody = document.getElementById('tcpfwd-rows');
  tbody.innerHTML = _tcpFwdAll.map((e,i) => {
    return `<tr>
      <td><input class="svc-input" style="width:80px" value="${_esc(e.name)}"
           onchange="_tcpFwdAll[${i}].name=this.value"></td>
      <td><input class="svc-input" type="number" style="width:62px" value="${e.src_port}"
           onchange="_tcpFwdAll[${i}].src_port=parseInt(this.value)||${e.src_port}"></td>
      <td><input class="svc-input" type="number" style="width:62px" value="${e.dst_port}"
           onchange="_tcpFwdAll[${i}].dst_port=parseInt(this.value)||${e.dst_port}"></td>
      <td class="col-center"><input type="checkbox" ${e.enabled?'checked':''}
           onchange="_tcpFwdAll[${i}].enabled=this.checked"></td>
      <td class="col-center"><input type="checkbox" title="Keep running in Local mode" ${e.keep_on_local?'checked':''}
           onchange="_tcpFwdAll[${i}].keep_on_local=this.checked"></td>
      <td><button class="btn btn-stop" onclick="tcpFwdDeleteRow(${i})">&#215;</button></td>
    </tr>`;
  }).join('');
}

function tcpFwdAddRow(){
  const name = document.getElementById('tcpfwd-new-name').value.trim();
  const src  = parseInt(document.getElementById('tcpfwd-new-src').value);
  const dst  = parseInt(document.getElementById('tcpfwd-new-dst').value);
  const en   = document.getElementById('tcpfwd-new-en').checked;
  const local = document.getElementById('tcpfwd-new-local')?.checked || false;
  if(!name || !src || !dst){ showToast(t('toast_fill_fields'),'err'); return; }
  _tcpFwdAll.push({name, src_port:src, dst_port:dst, enabled:en, keep_on_local:local});
  document.getElementById('tcpfwd-new-name').value='';
  document.getElementById('tcpfwd-new-src').value='';
  document.getElementById('tcpfwd-new-dst').value='';
  renderTcpFwdModalRows();
}

function tcpFwdDeleteRow(i){
  _tcpFwdAll.splice(i,1);
  renderTcpFwdModalRows();
}

async function tcpFwdLoad(){
  const st = document.getElementById('tcpfwd-save-status');
  st.textContent = t('loading');
  try {
    const [rc, rs] = await Promise.all([
      apiFetch('/api/server-tcp-fwds-config'),
      apiFetch('/api/server-tcp-fwds'),
    ]);
    _tcpFwdAll    = rc.forwards || [];
    _tcpFwdStatus = rs.forwards || {};
    const hostEl  = document.getElementById('tcpfwd-host');
    if(hostEl) hostEl.textContent = rc.host || '?';
    renderTcpFwdModalRows();
    st.textContent = '';
  } catch(e){
    st.textContent = t('load_error');
  }
}

async function tcpFwdSave(){
  const st = document.getElementById('tcpfwd-save-status');
  st.textContent = t('saving');
  try {
    const d = await apiPostJSON('/api/server-tcp-fwds-config', _tcpFwdAll);
    if(d.ok){
      st.textContent = t('saved');
      showToast(t('toast_tcp_fwds_saved'),'ok');
      scheduleServer(1200);
      setTimeout(tcpFwdLoad, 1500);
    } else {
      st.textContent = d.error || t('load_error');
      showToast(d.error || t('load_error'),'err');
    }
  } catch(e){
    st.textContent = t('load_error');
    showToast(t('toast_error',{msg:e.message}),'err');
  }
}

async function tcpFwdActionModal(src_port, action){
  const act = `${action}-tcpfwd-${src_port}`;
  showToast(`${action} tcpfwd:${src_port}…`,'busy');
  try {
    const d = await apiPost('/api/server-action', {action: act});
    if(d.ok) showToast(`tcpfwd ${src_port} ${action} ok`,'ok');
    else showToast(d.error || 'error','err');
    setTimeout(tcpFwdLoad, 800);
    scheduleServer(1200);
  } catch(e){
    showToast('fetch error','err');
  }
}

function openTcpFwdModal(){
  document.getElementById('tcpfwd-modal').classList.add('show');
  tcpFwdLoad();
}
function closeTcpFwdModal(){
  document.getElementById('tcpfwd-modal').classList.remove('show');
}
document.getElementById('tcpfwd-modal').addEventListener('click',e=>{
  if(e.target===document.getElementById('tcpfwd-modal')) closeTcpFwdModal();
});

// ── Global keyboard shortcuts (Esc = close, Enter = save) ─────────────────
document.addEventListener('keydown', function(e){
  const _modals = [
    { id: 'logmodal',     close: closeLog,          save: null         },
    { id: 'portsmodal',   close: closePortsModal,   save: portsSave    },
    { id: 'csvc-modal',   close: closeCsvcModal,    save: csvcSave     },
    { id: 'ssvc-modal',   close: closeSsvcModal,    save: ssvcSave     },
    { id: 'tcpfwd-modal', close: closeTcpFwdModal,  save: tcpFwdSave   },
  ];
  const active = _modals.find(m => document.getElementById(m.id)?.classList.contains('show'));
  if(!active) return;
  if(e.key === 'Escape'){
    e.preventDefault();
    active.close();
  } else if(e.key === 'Enter' && active.save){
    if(e.target.tagName === 'TEXTAREA') return;
    e.preventDefault();
    active.save();
  }
});

// ── Start ──────────────────────────────────────────────────────────────────
function tickUpdated(){
  const el = document.getElementById('st-updated');
  if(!_lastUpdateTs){ el.textContent = '—'; el.className = 'num muted'; return; }
  const s = Math.floor((Date.now() - _lastUpdateTs) / 1000);
  el.textContent = `${s}s`;
  el.className = 'num ' + (s < 10 ? 'ok' : s < 30 ? 'warn' : 'err');
}
setInterval(tickUpdated, 1000);

doRefreshClient();
doRefreshServer();
portsLoadBoth();
