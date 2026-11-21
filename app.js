/* NAS 出片控制台前端逻辑 — 零依赖
   本版：瑞士排版视觉 + 审查 P0 修复（错误态 / loading / 空闲态语义 / 响应式 / 高风险确认 / 键盘可达） */
'use strict';
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

/* ---------- 基础 ---------- */
function timeoutSignal(ms) {
  if (typeof AbortSignal.timeout === 'function') return AbortSignal.timeout(ms);
  const c = new AbortController();
  setTimeout(() => c.abort(), ms);
  return c.signal;
}
/* 访问令牌：优先取 ?token= 并记住，其次读 localStorage。服务端未启用令牌时留空即可。 */
const CONSOLE_TOKEN = (() => {
  const q = new URLSearchParams(location.search).get('token');
  if (q) { try { localStorage.setItem('console_token', q); } catch (e) {} return q; }
  try { return localStorage.getItem('console_token') || ''; } catch (e) { return ''; }
})();
async function api(path, opts) {
  const o = Object.assign({ signal: timeoutSignal(8000) }, opts || {});
  o.headers = Object.assign({}, o.headers || {});
  if (CONSOLE_TOKEN) o.headers['X-Console-Token'] = CONSOLE_TOKEN;
  let r;
  try { r = await fetch('/api/' + path, o); }
  catch (e) { throw new Error(e.name === 'TimeoutError' || e.name === 'AbortError' ? '请求超时（8 秒）' : '网络不可达'); }
  if (r.status === 401) throw new Error('未授权：请用 /?token=你的令牌 打开控制台');
  if (!r.ok) throw new Error('接口返回 HTTP ' + r.status);
  try { return await r.json(); }
  catch { throw new Error('接口返回的不是有效 JSON'); }
}
function toast(msg, type) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast show ' + (type || '');
  clearTimeout(t._tm);
  t._tm = setTimeout(() => t.className = 'toast', 3400);
}
function fmtSize(b) {
  if (b > 1048576) return (b / 1048576).toFixed(1) + ' MB';
  return (b / 1024).toFixed(0) + ' KB';
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function cssVar(n) {
  return getComputedStyle(document.documentElement).getPropertyValue(n).trim();
}

/* ---------- 错误态 ---------- */
function showError(view, msg) {
  const box = document.querySelector('[data-error-for="' + view + '"]');
  if (!box) return;
  box.hidden = false;
  box.innerHTML = '<span>数据读取失败：' + esc(msg) + '。页面仍可浏览，恢复后点重试。</span>' +
    '<button class="btn btn-sm" data-retry="' + esc(view) + '">重试</button>';
}
function clearError(view) {
  const box = document.querySelector('[data-error-for="' + view + '"]');
  if (box) { box.hidden = true; box.innerHTML = ''; }
}
/* 重绘 innerHTML 前后保留键盘焦点 */
function withFocus(container, render) {
  const active = document.activeElement;
  const key = active && container.contains(active)
    ? (active.getAttribute('data-media') || active.getAttribute('data-play') || active.getAttribute('data-m') || '')
    : null;
  render();
  if (key) {
    const back = Array.from(container.querySelectorAll('[data-media],[data-play],[data-m]')).find(el =>
      el.getAttribute('data-media') === key || el.getAttribute('data-play') === key || el.getAttribute('data-m') === key);
    if (back) back.focus({ preventScroll: true });
  }
}

/* ---------- 导航 ---------- */
function gotoView(v) {
  $$('.tab').forEach(t => {
    const on = t.dataset.view === v;
    t.classList.toggle('active', on);
    t.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  $$('.view').forEach(s => s.classList.toggle('active', s.id === 'view-' + v));
  if (v === 'analysis') loadAnalysis();
  if (v === 'video') loadRuns();
  if (v === 'publish') loadPublish();
  if (v === 'settings') { loadSettings(); loadStageModels(); loadVoice(); }
}
const reloaders = {
  overview: () => { loadOverview(); loadRunStatus(); },
  run: () => loadRunStatus(),
  video: () => loadRuns(),
  analysis: () => loadAnalysis(),
  publish: () => loadPublish(),
  settings: () => { loadSettings(); loadStageModels(); loadVoice(); }
};

/* ---------- 全局事件委托（innerHTML 重绘后依然有效） ---------- */
document.addEventListener('click', (e) => {
  const tab = e.target.closest('.tab');
  if (tab) { gotoView(tab.dataset.view); return; }
  const go = e.target.closest('[data-goto]');
  if (go) { gotoView(go.dataset.goto); return; }
  const retry = e.target.closest('[data-retry]');
  if (retry) { const fn = reloaders[retry.dataset.retry]; if (fn) fn(); return; }
  const play = e.target.closest('[data-play]');
  if (play) { gotoView('video'); playMedia(play.dataset.play); return; }
  const card = e.target.closest('.vcard');
  if (card) { playMedia(card.dataset.media); return; }
  const chip = e.target.closest('.model-chip');
  if (chip) { $('#mdlModel').value = chip.dataset.m; return; }
  const send = e.target.closest('[data-send]');
  if (send) { checkPublishCondition(send.dataset.send); return; }
});

/* ---------- 时钟 ---------- */
setInterval(() => {
  const n = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  $('#clock').textContent = n;
  $('#footTime').textContent = new Date().toLocaleString('zh-CN', { hour12: false });
}, 1000);

/* ---------- 总览 ---------- */
let ovSeq = 0, runSeq = 0;
function setLed(k, state, txt) {
  const el = document.querySelector('[data-led="' + k + '"]');
  if (el) el.className = state;
  const item = el && el.parentElement;
  const t = item && item.querySelector('.led-txt');
  if (t) t.textContent = txt;
}
async function loadOverview() {
  const my = ++ovSeq;
  try {
    const d = await api('overview');
    if (my !== ovSeq) return;
    if (!d || d.error) throw new Error(d && d.error || '空响应');
    renderOverview(d);
    clearError('overview');

    /* 选题是次要数据，失败只影响本区块 */
    try {
      const cfg = await api('config');
      if (my !== ovSeq) return;
      if (cfg && cfg.topic) {
        $('#ovTopic').innerHTML = '<div class="topic-view"><div class="q">' + esc(cfg.topic.query) + '</div>' +
          '<div class="b">' + esc(cfg.topic.business_context || '') + '</div></div>';
      } else { $('#ovTopic').innerHTML = '<div class="empty">尚未配置选题方向</div>'; }
    } catch (err) {
      $('#ovTopic').innerHTML = '<div class="empty">选题配置读取失败：' + esc(err.message) + '</div>';
    }
  } catch (e) {
    if (my === ovSeq) showError('overview', e.message);
  }
}
function renderOverview(d) {
  renderProgress(d);
  const running = !!(d.pipeline && d.pipeline.running);
  setLed('n8n', d.n8n && d.n8n.up ? 'on' : 'off', d.n8n && d.n8n.up ? '在线' : '离线');
  setLed('executor', d.executor && d.executor.up ? 'on' : 'off', d.executor && d.executor.up ? '在线' : '离线');
  setLed('model', 'idle', d.model && d.model.baseUrl ? '已配置' : '未配置');
  setLed('pipeline', running ? 'run' : 'idle', running ? '运行中' : '空闲');

  const pill = $('#ovPipePill');
  pill.textContent = running ? '运行中' : '空闲';
  pill.className = 'pill ' + (running ? 'run' : '');
  const st = $('#ovPipeState');
  st.textContent = running ? '流水线运行中' : '待启动';
  st.className = 'big-status ' + (running ? 'on' : 'idle');
  $('#ovPipeSince').textContent = '最近启动：' + ((d.pipeline && d.pipeline.started_at) || '—');

  /* 总览页「启动全流程」按钮：空闲可用，运行中禁用 */
  const ovStart = $('#ovStart');
  if (ovStart) ovStart.disabled = running;
  /* 卡片整块在空闲时可点启动 */
  const pipeMain = document.querySelector('#ovPipeline .pipe-main');
  if (pipeMain) pipeMain.classList.toggle('is-clickable', !running);

  const stages = d.stages || [];
  const nowKey = running ? currentStageKey(stages) : null;
  $('#ovStages').innerHTML = stages.map(s => {
    const cls = s.key === nowKey ? ' now' : (running && s.mtime ? ' done' : '');
    return '<span class="stage-chip' + cls + '">' + esc(s.label) + '</span>';
  }).join('') || '<span class="dim">暂无阶段数据</span>';

  const m = d.model || {};
  setOvModelFields(m);
  $('#ovModelPill').textContent = m.baseUrl ? '已连接' : '未配置';
  $('#ovModelPill').className = 'pill ' + (m.baseUrl ? 'ok' : 'warn');
  $('#ovExecutor').textContent = d.executor && d.executor.up ? '在线（' + d.executor.service + '）' : '离线';

  const infra = [
    ['n8n 引擎', d.n8n && d.n8n.up, d.n8n && d.n8n.status],
    ['执行器 18764', d.executor && d.executor.up, d.executor && d.executor.service],
    ['代码沙箱', d.sandbox && d.sandbox.ok, (d.sandbox && d.sandbox.containers || []).length + ' 容器'],
    ['模型网关', !!m.baseUrl, m.model],
  ];
  $('#ovInfra').innerHTML = infra.map(([n, on, v]) =>
    '<div class="infra-item"><span class="name">' + esc(n) + '</span><span class="val' + (on ? '' : ' off') + '">' + esc(v || '—') + '</span></div>').join('');

  const lr = d.latest_run;
  if (lr) {
    $('#ovRunPill').textContent = lr.publishable ? '可发布' : (lr.status || '—');
    $('#ovRunPill').className = 'pill ' + (lr.publishable ? 'ok' : 'warn');
    const vids = (lr.videos || []).map(v =>
      '<button class="link" data-play="' + esc(v.path) + '">' + (v.version === 'version_a' ? 'A 版' : 'B 版') + ' · ' + fmtSize(v.size) + '</button>').join('');
    $('#ovLatest').innerHTML =
      '<div class="mono dim" style="font-size:11px;margin-bottom:8px;overflow-wrap:anywhere">' + esc(lr.run_id) + '</div>' +
      '<div class="topic-view"><div class="q">' + esc(lr.project || '') + '</div><div class="b">' + esc(lr.time) + '</div></div>' +
      '<div style="margin-top:12px;font-size:13px">' + (vids || '无视频') + '</div>' +
      ((lr.blocking || []).length ? '<div class="hint" style="margin-top:10px">待办：' + esc(lr.blocking.join(' / ')) + '</div>' : '');
  } else {
    $('#ovLatest').innerHTML = '<div class="empty">暂无归档成片</div>';
  }
}
function currentStageKey(stages) {
  let key = null, maxT = 0;
  for (const s of stages) if (s.mtime && s.mtime > maxT) { maxT = s.mtime; key = s.key; }
  if (maxT && (Date.now() / 1000 - maxT) < 480) return key;
  return null;
}

/* ---------- 总览「模型与网关」行内编辑 ----------
   模型 / 网关 / 密钥三行直接改，保存走 /api/model/save（连通测试 → 写入执行器 → 重启）。
   轮询刷新（每 8 秒）不得覆盖正在编辑的内容：有未保存改动或焦点在本面板时跳过赋值。 */
let ovModelBase = { model: '', baseUrl: '', keyMasked: '' };
function setOvModelFields(m) {
  const editing = ovModelDirty() || $('#ovModelKv').contains(document.activeElement);
  if (!editing) {
    ovModelBase = { model: m.model || '', baseUrl: m.baseUrl || '', keyMasked: m.key_masked || '' };
    $('#ovModel').value = ovModelBase.model;
    $('#ovGateway').value = ovModelBase.baseUrl;
    $('#ovKey').value = '';
  }
  $('#ovKey').placeholder = m.key_masked ? ('已配置 ' + m.key_masked + '（留空 = 不修改）')
                                         : (m.baseUrl ? '未配置，输入 sk-…' : '—');
  ovModelSyncActions();
}
function ovModelDirty() {
  return $('#ovModel').value.trim() !== ovModelBase.model
    || $('#ovGateway').value.trim() !== ovModelBase.baseUrl
    || $('#ovKey').value.trim() !== '';
}
function ovModelSyncActions() {
  const box = $('#ovModelActions'), msg = $('#ovModelMsg');
  const dirty = ovModelDirty();
  box.hidden = !dirty && !msg.textContent;
  if (!dirty && !msg.textContent) { msg.textContent = ''; msg.className = 'form-msg'; }
}
$('#ovModelKv').addEventListener('input', (e) => {
  if (e.target.id === 'ovModel') ovModelSyncList(e.target);
  ovModelSyncActions();
});
let ovModelListLoaded = false;
async function ovModelSyncList(input) {
  if (ovModelListLoaded || input.value.length < 2) return;
  ovModelListLoaded = true;
  try {
    const d = await api('stage-models');
    $('#ovModelList').innerHTML = (d.available || []).map(x =>
      '<option value="' + esc(x) + '"></option>').join('');
  } catch (e) { /* 列表拿不到不影响手填 */ }
}
$('#ovModelReset').addEventListener('click', () => {
  $('#ovModel').value = ovModelBase.model;
  $('#ovGateway').value = ovModelBase.baseUrl;
  $('#ovKey').value = '';
  const msg = $('#ovModelMsg');
  msg.textContent = ''; msg.className = 'form-msg';
  ovModelSyncActions();
});
$('#ovModelSave').addEventListener('click', async () => {
  const baseUrl = $('#ovGateway').value.trim(), model = $('#ovModel').value.trim(), key = $('#ovKey').value.trim();
  if (!confirm('保存会写入执行器配置并触发重启，正在运行的任务可能被中断。确定继续？')) return;
  const msg = $('#ovModelMsg'), btn = $('#ovModelSave');
  btn.disabled = true; msg.textContent = '测试连通并保存中…（可能需要 1 分钟）'; msg.className = 'form-msg';
  $('#ovModelActions').hidden = false;
  try {
    const d = await api('model/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ baseUrl: baseUrl, model: model, key: key })
    });
    msg.textContent = d.ok ? (d.message || '已生效') : (d.error || '保存失败');
    msg.className = 'form-msg ' + (d.ok ? 'ok' : 'err');
    toast(d.ok ? (d.message || '模型配置已生效') : (d.error || '保存失败'), d.ok ? 'ok' : 'err');
    if (d.ok) {
      ovModelDirtyReset();
      $('#ovKey').value = '';
      loadOverview();
    }
  } catch (e) {
    msg.textContent = '保存失败：' + e.message; msg.className = 'form-msg err';
    toast('保存失败：' + e.message, 'err');
  } finally { btn.disabled = false; ovModelSyncActions(); }
});
function ovModelDirtyReset() {
  ovModelBase = { model: $('#ovModel').value.trim(), baseUrl: $('#ovGateway').value.trim(), keyMasked: ovModelBase.keyMasked };
}

/* ---------- 顶部流水线总进度（8 个阶段 + 发布，随 overview 轮询实时刷新） ---------- */
function renderProgress(d) {
  const stages = d.stages || [];
  const running = !!(d.pipeline && d.pipeline.running);
  const nowKey = running ? currentStageKey(stages) : null;
  const pubDone = !!(d.latest_run && d.latest_run.publishable);
  const steps = stages.map(s => ({
    key: s.key, label: s.label, latest: s.latest || '',
    done: running ? !!(s.mtime && s.key !== nowKey) : !!s.mtime,
    now: running && s.key === nowKey
  }));
  steps.push({ key: 'publish', label: '发布', latest: '', done: pubDone, now: false });
  const doneN = steps.filter(s => s.done).length;
  const pct = Math.round(doneN / steps.length * 100);
  $('#ppFill').style.width = pct + '%';
  $('#ppPct').textContent = pct + '%';
  const nowStep = steps.find(s => s.now);
  $('#ppNow').textContent = running
    ? (nowStep ? '当前：' + nowStep.label : '运行中，等待阶段信号…')
    : '空闲 · 显示最近一次足迹';
  $('#ppSteps').innerHTML = steps.map((s, i) =>
    '<li class="pp-step' + (s.done ? ' done' : '') + (s.now ? ' now' : '') + '" data-i="' + i +
    '" title="' + esc(s.latest || s.label) + '">' +
    '<span class="pp-dot"></span><span class="pp-name">' + esc(s.label) + '</span></li>').join('');
}

/* ---------- 执行 ---------- */
async function loadRunStatus() {
  const my = ++runSeq;
  try {
    const d = await api('pipeline/status');
    if (my !== runSeq) return;
    if (!d) throw new Error('空响应');
    const running = !!d.running;
    const pill = $('#runPill');
    pill.textContent = running ? '运行中' : '空闲';
    pill.className = 'pill ' + (running ? 'run' : '');
    $('#btnStart').disabled = running;
    $('#btnStartFromOverview').disabled = running;
    $('#runMeta').innerHTML =
      '启动时间：' + esc(d.started_at || '—') + '<br>日志：' + esc(d.log || '—') +
      '<br>状态：' + (running ? 'n8n 引擎执行中' : '无运行实例');

    const stages = d.stages || [];
    const nowKey = running ? currentStageKey(stages) : null;
    $('#runStages').innerHTML = stages.map((s, i) => {
      const isNow = running && s.key === nowKey;
      const dot = isNow ? '●' : (running && s.mtime ? '·' : '—');
      return '<li class="stage-item' + (isNow ? ' now' : '') + '">' +
        '<span class="stage-dot">' + dot + '</span>' +
        '<span class="s-name">' + esc(s.label) + '</span>' +
        '<span class="s-latest">' + esc(s.latest || '—') + '</span>' +
        '<span class="s-age">' + (s.age_min != null ? s.age_min + ' 分钟前' : '') + '</span></li>';
    }).join('');

    const log = $('#runLog');
    if (d.log_tail) {
      const nearBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 24;
      const html = esc(d.log_tail).split('\n').map(l =>
        l.includes('rror') || l.includes('fail') ? '<span class="hl-err">' + l + '</span>'
          : l.includes('uccess') ? '<span class="hl-ok">' + l + '</span>' : l).join('\n');
      log.innerHTML = html;
      if (nearBottom) log.scrollTop = log.scrollHeight;
    } else if (!running) {
      log.textContent = '等待流水线启动…';
    }
    clearError('run');
  } catch (e) {
    if (my === runSeq) showError('run', e.message);
  }
}
async function startPipeline() {
  const a = $('#btnStart'), b = $('#btnStartFromOverview'), o = $('#ovStart');
  const all = [a, b, o].filter(Boolean);
  all.forEach(el => { el.disabled = true; });
  const old = { a: a.textContent, b: b.textContent, o: o ? o.textContent : '' };
  all.forEach(el => { el.textContent = '启动中…'; });
  try {
    const d = await api('pipeline/start', { method: 'POST' });
    toast(d.message || d.error, d.ok ? 'ok' : 'err');
    if (d.ok) { gotoView('run'); setTimeout(loadRunStatus, 1800); }
  } catch (e) { toast('启动失败：' + e.message, 'err'); }
  finally {
    a.textContent = old.a; b.textContent = old.b;
    if (o) o.textContent = old.o;
    loadRunStatus(); loadOverview();
  }
}
$('#btnStart').addEventListener('click', startPipeline);
$('#btnStartFromOverview').addEventListener('click', startPipeline);
$('#ovStart').addEventListener('click', startPipeline);
$('#btnRefreshRun').addEventListener('click', loadRunStatus);

/* 总览「流水线状态」整张卡片：空闲时点击卡片任意处（除按钮本身）即可启动 */
$('#ovPipeline').addEventListener('click', (e) => {
  if (e.target.closest('#ovStart')) return;            // 按钮有独立处理
  const main = e.target.closest('.pipe-main');
  if (!main || !main.classList.contains('is-clickable')) return;
  const o = $('#ovStart');
  if (!o || o.disabled) return;
  startPipeline();
});

/* ---------- 视频 ---------- */
let currentMedia = null;
let runsCache = null, runsCacheAt = 0;
async function getRuns(force) {
  if (!force && runsCache && Date.now() - runsCacheAt < 30000) return runsCache;
  const d = await api('runs');
  if (!d || !d.runs) throw new Error('成片列表为空或格式错误');
  runsCache = d; runsCacheAt = Date.now();
  return d;
}
async function loadRuns() {
  $('#videoEmpty').classList.remove('hide');
  $('#videoEmpty').textContent = '正在读取成片列表…';
  try {
    const d = await getRuns();
    const cards = [];
    for (const run of d.runs) {
      for (const v of (run.videos || [])) {
        cards.push('<button class="vcard" data-media="' + esc(v.media) + '" aria-label="播放 ' + esc(run.time) + ' ' + esc(v.version) + '">' +
          '<div class="v-run">' + esc(run.time) + ' · ' + esc(run.run_id.slice(0, 14)) + '…</div>' +
          '<div class="v-ver">' + (v.version === 'version_a' ? 'A 版' : 'B 版') + '</div>' +
          '<div class="v-meta"><span>' + fmtSize(v.size) + '</span>' + (v.duration ? '<span>' + v.duration.toFixed(1) + 's</span>' : '') + '</div>' +
          '<div class="v-tags">' +
            (v.ratio ? '<span class="stage-chip">' + esc(v.ratio) + '</span>' : '') +
            (v.w ? '<span class="stage-chip">' + v.w + '×' + v.h + '</span>' : '') +
            '<span class="stage-chip">' + (run.publishable ? '可发布' : '审阅中') + '</span>' +
          '</div></button>');
      }
    }
    withFocus($('#videoList'), () => {
      $('#videoList').innerHTML = cards.join('') || '<div class="empty">暂无成片</div>';
      $$('#videoList .vcard').forEach(c => c.classList.toggle('playing', c.dataset.media === currentMedia));
    });
    if (cards.length) {
      $('#videoEmpty').textContent = '点击右侧成片卡片开始观看';
    } else {
      $('#videoEmpty').textContent = '暂无成片';
    }
    clearError('video');
  } catch (e) {
    $('#videoList').innerHTML = '';
    $('#videoEmpty').textContent = '成片列表读取失败';
    showError('video', e.message);
  }
}
function playMedia(rel, skipReload) {
  if (rel === currentMedia && !skipReload) return;
  currentMedia = rel;
  const p = $('#videoPlayer');
  p.src = '/media/' + rel;
  p.play().catch(() => {});
  $('#videoInfo').textContent = rel;
  $('#videoEmpty').classList.add('hide');
  $$('#videoList .vcard').forEach(c => c.classList.toggle('playing', c.dataset.media === rel));
}

/* ---------- 分析 ---------- */
let lastAnalysis = null;
async function loadAnalysis() {
  try {
    const d = await api('analysis');
    if (!d) throw new Error('空响应');
    lastAnalysis = d;
    const stats = [
      [d.total, '执行记录总数', ''],
      [d.success, '成功执行', ''],
      [d.error, '失败执行', ''],
      [d.avg_min != null ? d.avg_min + ' 分' : '—', '平均成功耗时', 'acc'],
    ];
    $('#anStats').innerHTML = stats.map(([v, l, c]) =>
      '<div class="stat"><div class="s-val ' + c + '">' + esc(v) + '</div><div class="s-label">' + esc(l) + '</div></div>').join('');
    drawDaily(d.daily || []);
    drawDurations(d.durations || []);

    const rows = (d.executions || []).map(e => '<tr>' +
      '<td class="mono">' + e.id + '</td>' +
      '<td><span class="pill ' + (e.status === 'success' ? 'ok' : e.status === 'error' ? 'err' : '') + '">' + esc(e.status) + '</span></td>' +
      '<td class="mono">' + esc(e.mode) + '</td><td class="mono">' + esc(e.started) + '</td><td class="mono">' + esc(e.stopped) + '</td></tr>').join('');
    $('#anExecs tbody').innerHTML = rows || '<tr><td colspan="5" class="empty">无记录</td></tr>';

    const t = d.tasks || { roles: [], recent: [] };
    $('#anStages tbody').innerHTML = (t.roles || []).map(r => '<tr>' +
      '<td>' + esc(r.label) + '</td><td class="mono">' + r.success + '</td>' +
      '<td class="mono' + (r.failed ? ' is-err' : '') + '">' + r.failed + '</td>' +
      '<td class="mono">' + (r.retried || '—') + '</td>' +
      '<td class="mono">' + (r.rate == null ? '—' : r.rate + '%') + '</td></tr>').join('')
      || '<tr><td colspan="5" class="empty">无任务</td></tr>';
    $('#anRetries tbody').innerHTML = (t.recent || []).map(r => '<tr>' +
      '<td>' + esc(r.label) + '</td>' +
      '<td><span class="pill ' + (r.status === 'succeeded' ? 'ok' : 'err') + '">' + (r.status === 'succeeded' ? '已恢复' : '失败') + '</span>' +
        (r.error ? '<span class="dim mono" style="margin-left:8px">' + esc(r.error) + '</span>' : '') + '</td>' +
      '<td class="mono">' + (r.attempts ? r.attempts + ' 次' : '—') + '</td>' +
      '<td class="mono">' + esc(r.updated) + '</td></tr>').join('')
      || '<tr><td colspan="4" class="empty">最近没有失败或重试</td></tr>';
    clearError('analysis');
  } catch (e) {
    $('#anStats').innerHTML = '';
    $('#anDaily').innerHTML = '<div class="empty">图表不可用</div>';
    $('#anDur').innerHTML = '<div class="empty">图表不可用</div>';
    showError('analysis', e.message);
  }
}
/* W 取容器真实像素宽，viewBox 与之 1:1，SVG 文字字号不再随容器漂移 */
function chartW(el) { return Math.max(280, Math.round(el.clientWidth || 320)); }
function drawDaily(daily) {
  const el = $('#anDaily');
  if (!daily.length) { el.innerHTML = '<div class="empty">无数据</div>'; return; }
  const W = chartW(el), H = 200, PAD = 34;
  const cGrid = cssVar('--rule-soft'), cAxis = cssVar('--ink-2'), cInk = cssVar('--ink'), cRed = cssVar('--red');
  const cBar = cssVar('--chart-bar') || cInk, cErr = cssVar('--chart-err') || cRed, cOther = cssVar('--chart-other') || cGrid;
  const max = Math.max(1, ...daily.map(d => d.success + d.error + d.other));
  const bw = Math.min(34, (W - PAD * 2) / daily.length - 8);
  let svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="每日执行柱状图">';
  for (let i = 0; i <= 3; i++) {
    const y = 18 + (H - 58) * i / 3;
    svg += '<line x1="' + PAD + '" y1="' + y + '" x2="' + (W - 8) + '" y2="' + y + '" stroke="' + cGrid + '" stroke-width="1"/>';
    svg += '<text x="' + (PAD - 8) + '" y="' + (y + 4) + '" fill="' + cAxis + '" font-size="11" text-anchor="end" font-family="monospace">' + Math.round(max * (3 - i) / 3) + '</text>';
  }
  daily.forEach((d, i) => {
    const x = PAD + i * (W - PAD - 12) / daily.length;
    const total = d.success + d.error + d.other;
    const hTotal = (H - 58) * total / max;
    const hErr = (H - 58) * d.error / max;
    const hOther = (H - 58) * d.other / max;
    const base = 18 + (H - 58);
    svg += '<rect x="' + x + '" y="' + (base - hTotal) + '" width="' + bw + '" height="' + hTotal + '" rx="3" fill="' + cBar + '"/>';
    if (hErr) svg += '<rect x="' + x + '" y="' + (base - hTotal) + '" width="' + bw + '" height="' + hErr + '" rx="3" fill="' + cErr + '"/>';
    if (hOther) svg += '<rect x="' + x + '" y="' + (base - hOther) + '" width="' + bw + '" height="' + hOther + '" rx="3" fill="' + cOther + '"/>';
    if (daily.length <= 8 || i % 2 === 0) {
      svg += '<text x="' + (x + bw / 2) + '" y="' + (H - 10) + '" fill="' + cAxis + '" font-size="11" text-anchor="middle" font-family="monospace">' + esc(d.date.slice(5)) + '</text>';
    }
  });
  svg += '</svg>';
  el.innerHTML = svg;
}
function drawDurations(durs) {
  const el = $('#anDur');
  if (!durs.length) { el.innerHTML = '<div class="empty">暂无成功执行</div>'; return; }
  const W = chartW(el), H = 200, PAD = 40;
  const max = Math.max(...durs), min = Math.min(...durs, max);
  const span = Math.max(1, max - min);
  const flat = max === min;
  const pts = durs.map((v, i) => {
    const x = (durs.length === 1)
      ? PAD + (W - PAD - 14) / 2
      : PAD + i * (W - PAD - 14) / Math.max(1, durs.length - 1);
    const y = 18 + (H - 58) * (flat ? .5 : (1 - (v - min) / span));
    return [x, y, v];
  });
  const cAxis = cssVar('--ink-2'), cInk = cssVar('--ink'), cRed = cssVar('--red'), cPaper = cssVar('--paper');
  const cAxisLine = cssVar('--chart-axis') || cInk;
  let svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="成功执行耗时折线图">';
  svg += '<line x1="' + PAD + '" y1="18" x2="' + PAD + '" y2="' + (H - 40) + '" stroke="' + cAxisLine + '"/>';
  svg += '<line x1="' + PAD + '" y1="' + (H - 40) + '" x2="' + (W - 8) + '" y2="' + (H - 40) + '" stroke="' + cAxisLine + '"/>';
  svg += '<text x="' + (PAD - 8) + '" y="' + (flat ? Math.max(22, pts[0][1] - 8) : 24) + '" fill="' + cAxis + '" font-size="11" text-anchor="end" font-family="monospace">' + max + 'm</text>';
  if (!flat) svg += '<text x="' + (PAD - 8) + '" y="' + (H - 36) + '" fill="' + cAxis + '" font-size="11" text-anchor="end" font-family="monospace">' + min + 'm</text>';
  if (pts.length === 1) svg += '<line x1="' + PAD + '" y1="' + pts[0][1] + '" x2="' + (W - 8) + '" y2="' + pts[0][1] + '" stroke="' + cAxisLine + '" stroke-dasharray="4 4"/>';
  if (pts.length > 1) svg += '<polyline points="' + pts.map(p => p[0] + ',' + p[1]).join(' ') + '" fill="none" stroke="' + cRed + '" stroke-width="2" stroke-linejoin="round"/>';
  pts.forEach(p => { svg += '<circle cx="' + p[0] + '" cy="' + p[1] + '" r="3.5" fill="' + cPaper + '" stroke="' + cRed + '" stroke-width="2"/>'; });
  svg += '<text x="' + (W - 10) + '" y="14" fill="' + cAxis + '" font-size="11" text-anchor="end" font-family="monospace">n=' + durs.length + '</text>';
  svg += '</svg>';
  el.innerHTML = svg;
}
let resizeTm;
window.addEventListener('resize', () => {
  clearTimeout(resizeTm);
  resizeTm = setTimeout(() => {
    if (lastAnalysis && $('#view-analysis').classList.contains('active')) {
      drawDaily(lastAnalysis.daily || []); drawDurations(lastAnalysis.durations || []);
    }
  }, 180);
});

/* ---------- 发布 ---------- */
async function loadPublish() {
  try {
    const d = await api('publish');
    if (!d) throw new Error('空响应');
    const names = { douyin: '抖音', xhs: '小红书' };
    $('#pubAccounts').innerHTML = Object.entries(d.accounts || {}).map(([k, a]) => {
      const loggedIn = a.status === 'logged_in';
      return '<div class="acct"><div class="a-name"><span class="platform-logo">' + (k === 'douyin' ? 'DY' : 'XH') + '</span>' + esc(a.platform || names[k]) + '</div>' +
        '<div class="a-status"><span class="pill ' + (loggedIn ? 'ok' : 'warn') + '">' + (loggedIn ? '已登录' : '未登录') + '</span></div>' +
        '<div class="a-note">' + esc(a.note || '') + '</div></div>';
    }).join('');
    renderQueue(d.queue || []);
    clearError('publish');

    /* 成片列表较慢：先禁用提交，数据到位后再放开，避免把「加载中」误判成「无可用成片」 */
    let rd;
    try { rd = await getRuns(); }
    catch (e) {
      $('#pubVideo').innerHTML = '<option value="">成片读取失败</option>';
      showError('publish', e.message);
      return;
    }
    const opts = [];
    for (const run of (rd.runs || []).slice(0, 6)) {
      for (const v of (run.videos || [])) {
        opts.push('<option value="' + esc(run.run_id) + '|' + esc(v.version) + '">' + run.time + ' · ' + run.run_id.slice(10, 18) + ' · ' + (v.version === 'version_a' ? 'A 版' : 'B 版') + '</option>');
      }
    }
    $('#pubVideo').innerHTML = opts.join('') || '<option value="">无可用成片</option>';
    $('#pubAdd').disabled = !opts.length;
    $('#pubPackage').disabled = !opts.length;
  } catch (e) {
    showError('publish', e.message);
  }
}
function renderQueue(items) {
  const names = { douyin: '抖音', xhs: '小红书' };
  const rows = items.map(it => {
    const checks = (it.checks || []).map(c =>
      '<span class="pill ' + (c.pass ? 'ok' : 'err') + '">' + esc(c.name) + '</span>').join(' ');
    return '<tr>' +
      '<td class="mono" style="font-size:11px">' + esc(it.run.slice(10, 18)) + ' · ' + (it.version === 'version_a' ? 'A' : 'B') + '</td>' +
      '<td>' + esc(names[it.platform] || it.platform) + '</td>' +
      '<td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + esc(it.caption) + '">' + esc(it.caption || '—') + '</td>' +
      '<td>' + (checks || '—') + '</td>' +
      '<td><span class="pill ' + (it.status === 'pending_auth' ? 'warn' : '') + '">' + (it.status === 'pending_auth' ? '待账号授权' : esc(it.status)) + '</span></td>' +
      '<td><button class="btn btn-sm" data-send="' + esc(it.id) + '" title="只检查发布条件，不会真正上传">检查发布条件</button></td></tr>';
  }).join('');
  $('#pubQueue tbody').innerHTML = rows || '<tr><td colspan="6" class="empty">队列为空</td></tr>';
}
async function checkPublishCondition(id) {
  try {
    const d = await api('publish/send', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: id })
    });
    toast(d.message || d.error, d.ok ? 'ok' : 'err');
    loadPublish();
  } catch (e) { toast('检查失败：' + e.message, 'err'); }
}
$('#pubAdd').addEventListener('click', async () => {
  const val = ($('#pubVideo').value || '').split('|');
  if (val.length !== 2) { toast('请先选择一版成片', 'err'); return; }
  const btn = $('#pubAdd');
  btn.disabled = true;
  try {
    const d = await api('publish/queue', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run: val[0], version: val[1], platform: $('#pubPlatform').value, caption: $('#pubCaption').value })
    });
    toast(d.message || (d.ok ? '已加入队列' : d.error), d.ok ? 'ok' : 'err');
    if (d.ok) $('#pubCaption').value = '';
    await loadPublish();
  } catch (e) { toast('加入队列失败：' + e.message, 'err'); }
  finally { btn.disabled = !$('#pubVideo').value; }
});

/* ---------- 生成发布包 ---------- */
$('#pubPackage').addEventListener('click', async () => {
  const val = ($('#pubVideo').value || '').split('|');
  if (val.length !== 2) { toast('请先选择一版成片', 'err'); return; }
  const box = $('#pkgResult'), btn = $('#pubPackage');
  box.innerHTML = '<div class="skeleton">正在重排 3:4 字幕并抽帧，需要十几秒…</div>';
  btn.disabled = true;
  try {
    const d = await api('publish/package', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run: val[0], version: val[1], platform: $('#pubPlatform').value })
    });
    if (!d.ok) { box.innerHTML = '<div class="form-msg err">' + esc(d.error || '生成失败') + '</div>'; return; }
    const checks = (d.checks || []).map(c => '<li><span class="pill ' + (c.pass ? 'ok' : 'err') + '">' + (c.pass ? '通过' : '注意') + '</span> ' +
      esc(c.name) + (c.detail ? '<span class="dim mono"> · ' + esc(c.detail) + '</span>' : '') + '</li>').join('');
    const notes = (d.notes || []).map(n => '<li class="dim">' + esc(n) + '</li>').join('');
    box.innerHTML = '<div class="pkg-path mono">' + esc(d.dir) + '</div>' +
      '<div class="dim">文件：' + (d.files || []).map(esc).join(' · ') + '</div>' +
      '<ul class="pkg-checks">' + checks + '</ul>' +
      (notes ? '<div class="form-msg err">需注意：</div><ul class="pkg-checks">' + notes + '</ul>' : '') +
      '<div class="dim">账号登录与上传需人工完成，本工具没有代登录、也没有代发布。</div>';
    toast('发布包已生成', 'ok');
  } catch (e) {
    box.innerHTML = '<div class="form-msg err">' + esc(e.message) + '</div>';
    toast('生成失败：' + e.message, 'err');
  } finally { btn.disabled = !$('#pubVideo').value; }
});

/* ---------- 按阶段指定模型（改为显式应用 + 二次确认） ---------- */
let smDirty = null;
async function loadStageModels() {
  const box = $('#stageModels');
  if (!box) return;
  try {
    const d = await api('stage-models');
    if (!d) throw new Error('空响应');
    const opts = (m) => ['<option value="">默认（' + esc(d.default || '—') + '）</option>']
      .concat((d.available || []).map(x => '<option value="' + esc(x) + '"' + (x === m ? ' selected' : '') + '>' + esc(x) + '</option>')).join('');
    box.innerHTML = (d.roles || []).map(r =>
      '<div class="stage-row"><label class="s-name" for="role-' + esc(r.role) + '">' + esc(r.label) + '</label>' +
      '<select data-role="' + esc(r.role) + '" id="role-' + esc(r.role) + '" aria-label="' + esc(r.label) + ' 使用的模型">' + opts(r.model) + '</select>' +
      '<span class="s-state ' + (r.model ? 'acc' : '') + '">' + (r.model ? '已单独指定' : '跟随默认') + '</span></div>').join('');
    smDirty = null;
    $('#smApply').disabled = true;
    $('#smMsg').textContent = '';
    clearError('settings');
  } catch (e) { showError('settings', e.message); }
}
$('#stageModels').addEventListener('change', (e) => {
  const sel = e.target.closest('select[data-role]');
  if (!sel) return;
  smDirty = { role: sel.dataset.role, model: sel.value };
  $('#smApply').disabled = false;
  $('#smMsg').textContent = '有未应用的改动；点击「应用更改」才会写入。';
  $('#smMsg').className = 'form-msg';
});
$('#smApply').addEventListener('click', async () => {
  if (!smDirty) return;
  if (!confirm('应用后会重启执行器，正在运行的任务会被中断。确定继续？')) return;
  const btn = $('#smApply'), msg = $('#smMsg');
  btn.disabled = true; msg.textContent = '写入并重启执行器…'; msg.className = 'form-msg';
  try {
    const r = await api('stage-models/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(smDirty)
    });
    msg.textContent = r.ok ? ('已生效 · 执行器 ' + (r.service || '')) : (r.error || '保存失败');
    msg.className = 'form-msg ' + (r.ok ? 'ok' : 'err');
    toast(r.ok ? '模型配置已应用' : (r.error || '保存失败'), r.ok ? 'ok' : 'err');
    if (r.ok) await loadStageModels();
  } catch (e) {
    msg.textContent = '保存失败：' + e.message; msg.className = 'form-msg err';
    toast('保存失败：' + e.message, 'err');
  } finally { btn.disabled = !smDirty; }
});

/* ---------- 设置 ---------- */
async function loadSettings() {
  try {
    const cfg = await api('config');
    if (cfg && cfg.topic) {
      $('#cfgQuery').value = cfg.topic.query || '';
      $('#cfgContext').value = cfg.topic.business_context || '';
    }
    clearError('settings');
  } catch (e) { showError('settings', '配置读取失败：' + e.message); }
  try {
    const ov = await api('overview');
    const m = ov && ov.model;
    if (m) { $('#mdlBase').value = m.baseUrl || ''; $('#mdlModel').value = m.model || ''; }
  } catch (e) { $('#mdlMsg').textContent = '网关信息读取失败：' + e.message; }
}
$('#cfgSave').addEventListener('click', async () => {
  const msg = $('#cfgMsg'), btn = $('#cfgSave');
  btn.disabled = true; msg.textContent = '保存中…'; msg.className = 'form-msg';
  try {
    const d = await api('config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ topic: { query: $('#cfgQuery').value, business_context: $('#cfgContext').value } })
    });
    msg.textContent = d.message || d.error;
    msg.className = 'form-msg ' + (d.ok ? 'ok' : 'err');
    toast(d.message || d.error, d.ok ? 'ok' : 'err');
  } catch (e) { msg.textContent = '保存失败：' + e.message; msg.className = 'form-msg err'; toast('保存失败：' + e.message, 'err'); }
  finally { btn.disabled = false; }
});
$('#mdlFetch').addEventListener('click', async () => {
  const list = $('#mdlList');
  list.innerHTML = '<span class="dim" style="font-size:12px">获取中…</span>';
  try {
    const d = await api('models');
    if (d.error || !d.models) { list.innerHTML = '<span class="dim" style="font-size:12px">失败：' + esc(d.error || '') + '</span>'; return; }
    list.innerHTML = d.models.map(m => '<button class="model-chip" data-m="' + esc(m) + '">' + esc(m) + (m === d.current ? ' ✓' : '') + '</button>').join('');
  } catch (e) { list.innerHTML = ''; toast('获取失败：' + e.message, 'err'); }
});
$('#mdlTest').addEventListener('click', async () => {
  const msg = $('#mdlMsg');
  msg.textContent = '测试中…'; msg.className = 'form-msg';
  try {
    const d = await api('model/test');
    msg.textContent = d.ok ? '连通正常（' + d.latency + 's）· 回复：' + d.reply : '失败：' + (d.error || '');
    msg.className = 'form-msg ' + (d.ok ? 'ok' : 'err');
  } catch (e) { msg.textContent = '测试失败：' + e.message; msg.className = 'form-msg err'; }
});
$('#mdlSave').addEventListener('click', async () => {
  if (!confirm('保存会写入执行器配置并触发重启，正在运行的任务可能被中断。确定继续？')) return;
  const msg = $('#mdlMsg'), btn = $('#mdlSave');
  btn.disabled = true; msg.textContent = '测试并保存中…（可能需要 1 分钟）'; msg.className = 'form-msg';
  try {
    const d = await api('model/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ baseUrl: $('#mdlBase').value.trim(), model: $('#mdlModel').value.trim(), key: $('#mdlKey').value.trim() })
    });
    msg.textContent = d.message || d.error;
    msg.className = 'form-msg ' + (d.ok ? 'ok' : 'err');
    if (d.ok) { $('#mdlKey').value = ''; loadOverview(); }
  } catch (e) { msg.textContent = '保存失败：' + e.message; msg.className = 'form-msg err'; toast('保存失败：' + e.message, 'err'); }
  finally { btn.disabled = false; }
});


/* ---------- 配音音色 ---------- */
let voiceActive = null;
async function loadVoice() {
  const sel = $('#voicePreset'), note = $('#voiceNote'), btn = $('#voiceApply');
  if (!sel) return;
  try {
    const d = await api('voice');
    if (!d || !d.presets) throw new Error('空响应');
    voiceActive = d.active;
    sel.innerHTML = d.presets.map(p =>
      '<option value="' + esc(p.id) + '"' + (p.id === d.active ? ' selected' : '') + '>' +
      esc(p.label) + (p.id === d.active ? '（当前）' : '') + '</option>').join('');
    const cur = d.presets.find(p => p.id === d.active) || d.presets[0];
    note.textContent = cur ? (cur.note + (cur.license ? '　授权：' + cur.license : '')) : '';
    btn.disabled = true;
    $('#voiceMsg').textContent = '';
    clearError('settings');
  } catch (e) { showError('settings', '配音配置读取失败：' + e.message); }
}
$('#voicePreset').addEventListener('change', (e) => {
  $('#voiceApply').disabled = (e.target.value === voiceActive);
  const msg = $('#voiceMsg');
  msg.textContent = e.target.value === voiceActive ? '' : '有未应用的改动；点击「应用音色」才会写入。';
  msg.className = 'form-msg';
});
$('#voiceApply').addEventListener('click', async () => {
  const btn = $('#voiceApply'), msg = $('#voiceMsg'), preset = $('#voicePreset').value;
  if (!preset || preset === voiceActive) return;
  btn.disabled = true; msg.textContent = '写入配音配置…'; msg.className = 'form-msg';
  try {
    const r = await api('voice/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ preset })
    });
    msg.textContent = r.ok ? (r.message + ' · 下一次出片生效') : (r.error || '保存失败');
    msg.className = 'form-msg ' + (r.ok ? 'ok' : 'err');
    toast(r.ok ? '配音音色已切换' : (r.error || '保存失败'), r.ok ? 'ok' : 'err');
    if (r.ok) await loadVoice();
  } catch (e) {
    msg.textContent = '保存失败：' + e.message; msg.className = 'form-msg err';
    toast('保存失败：' + e.message, 'err');
  } finally { btn.disabled = false; }
});

/* ---------- 顶部进度条：点步骤跳到对应面板（阶段→执行控制，发布→发布准备） ---------- */
$('#ppSteps').addEventListener('click', (e) => {
  const li = e.target.closest('.pp-step');
  if (!li) return;
  const i = +li.dataset.i;
  const target = i >= 8 ? $('#view-publish') : $('#view-run');
  if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
});

/* ---------- 主题切换：亮色为主，保留深色变体 ---------- */
const FAV_LIGHT = "data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20viewBox%3D%270%200%2032%2032%27%3E%3Crect%20width%3D%2732%27%20height%3D%2732%27%20rx%3D%278%27%20fill%3D%27%23E35D0C%27%2F%3E%3Crect%20x%3D%279%27%20y%3D%278%27%20width%3D%273.4%27%20height%3D%2716%27%20rx%3D%271.7%27%20fill%3D%27%23fff%27%2F%3E%3Crect%20x%3D%2714.4%27%20y%3D%278%27%20width%3D%278.6%27%20height%3D%273.4%27%20rx%3D%271.7%27%20fill%3D%27%23fff%27%2F%3E%3Crect%20x%3D%2714.4%27%20y%3D%2714.3%27%20width%3D%278.6%27%20height%3D%273.4%27%20rx%3D%271.7%27%20fill%3D%27%23fff%27%2F%3E%3Crect%20x%3D%2714.4%27%20y%3D%2720.6%27%20width%3D%278.6%27%20height%3D%273.4%27%20rx%3D%271.7%27%20fill%3D%27%23fff%27%2F%3E%3C%2Fsvg%3E";
const FAV_DARK = "data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20viewBox%3D%270%200%2032%2032%27%3E%3Crect%20width%3D%2732%27%20height%3D%2732%27%20rx%3D%278%27%20fill%3D%27%23F97316%27%2F%3E%3Crect%20x%3D%279%27%20y%3D%278%27%20width%3D%273.4%27%20height%3D%2716%27%20rx%3D%271.7%27%20fill%3D%27%23fff%27%2F%3E%3Crect%20x%3D%2714.4%27%20y%3D%278%27%20width%3D%278.6%27%20height%3D%273.4%27%20rx%3D%271.7%27%20fill%3D%27%23fff%27%2F%3E%3Crect%20x%3D%2714.4%27%20y%3D%2714.3%27%20width%3D%278.6%27%20height%3D%273.4%27%20rx%3D%271.7%27%20fill%3D%27%23fff%27%2F%3E%3Crect%20x%3D%2714.4%27%20y%3D%2720.6%27%20width%3D%278.6%27%20height%3D%273.4%27%20rx%3D%271.7%27%20fill%3D%27%23fff%27%2F%3E%3C%2Fsvg%3E";
function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  try { localStorage.setItem('nas-console-theme', t); } catch (e) {}
  const btn = document.getElementById('themeToggle');
  if (btn) {
    const dark = t === 'dark';
    btn.setAttribute('aria-pressed', dark ? 'true' : 'false');
    btn.textContent = dark ? '◑ 浅色' : '◐ 深色';
  }
  const fav = document.querySelector('link[rel="icon"]');
  if (fav) fav.href = t === 'dark' ? FAV_DARK : FAV_LIGHT;
  if (lastAnalysis && $('#view-analysis').classList.contains('active')) {
    drawDaily(lastAnalysis.daily || []);
    drawDurations(lastAnalysis.durations || []);
  }
}
(function initTheme() {
  const cur = document.documentElement.getAttribute('data-theme');
  applyTheme(cur === 'dark' ? 'dark' : 'light');
  const btn = document.getElementById('themeToggle');
  if (btn) btn.addEventListener('click', () =>
    applyTheme(document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark'));
})();

/* ---------- 初始化（展示型单页：所有章节一次性加载） ---------- */
function bootAll() {
  loadOverview();
  loadRunStatus();
  loadRuns();
  loadAnalysis();
  loadPublish();
  loadSettings();
  loadStageModels();
  loadVoice();
}
bootAll();
setInterval(() => {
  if (document.visibilityState !== 'visible') return;
  loadOverview();
  loadRunStatus();
}, 8000);

/* ---------- 导航高亮：滚动到哪个章节就点亮哪个胶囊 ---------- */
(function initScrollSpy() {
  const links = Array.prototype.slice.call(document.querySelectorAll('.anchornav a[href^="#"]'));
  if (!links.length || !('IntersectionObserver' in window)) return;
  const targets = links.map(a => document.querySelector(a.getAttribute('href'))).filter(Boolean);
  function setActive(id) {
    links.forEach(a => a.classList.toggle('active', a.getAttribute('href') === '#' + id));
  }
  const seen = new Map();
  const io = new IntersectionObserver(entries => {
    entries.forEach(e => seen.set(e.target.id, e.intersectionRatio));
    let bestId = null, best = 0;
    seen.forEach((ratio, id) => { if (ratio > best) { best = ratio; bestId = id; } });
    if (bestId) setActive(bestId);
  }, { rootMargin: '-90px 0px -45% 0px', threshold: [0, .15, .35, .6, 1] });
  targets.forEach(t => io.observe(t));
  setActive((targets[0] || {}).id || '');
})();

/* ---------- 侧栏「控制台」：点导航直接展开折叠面板 ---------- */
document.querySelectorAll('.anchornav a[href="#demo-console"]').forEach(a => {
  a.addEventListener('click', () => {
    const d = document.getElementById('demo-console');
    if (d) d.open = true;
  });
});
