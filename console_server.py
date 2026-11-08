#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NAS 出片控制台后端 — 标准库实现，零依赖
端口 20130。所有操作针对真实服务：n8n / 执行器 / 模型网关 / 归档产物。
不伪造登录或发布状态。
"""
import json, subprocess, threading, time, uuid, re, sys, os, socket, sqlite3, hashlib, shutil
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, unquote
from shlex import quote as shq

# ---- 可配置项：均可用环境变量覆盖，默认值不含任何内网地址 ----
BASE = Path(os.environ.get('CONSOLE_BASE', str(Path(__file__).resolve().parent)))
WF_ROOT = Path(os.environ.get('WF_ROOT', str(BASE.parent / 'workflows')))
EXEC_ROOT = WF_ROOT / '01-agent-executor'
ARCHIVE_RUNS = WF_ROOT / '08-review-archive/runs'
PROD_RUNS = WF_ROOT / '07-video-production/runs'
# 数据库节点备份目录（可选）
BACKUP_DIR = Path(os.environ.get('BACKUP_DIR', str(WF_ROOT.parent / '_patch')))

# ---- 成片展示白名单 ----
# 成片页 / 总览「最新成片」只展示这里列出的成片（路径相对 WF_ROOT，带不带 video/ 层都行）。
# 留空 [] 即恢复为「扫描归档与出片目录下的全部成片」。自用后台：默认扫描全部。
SHOWCASE_VIDEOS = []


def _parse_showcase(paths):
    """把白名单路径解析成 {(run_id, version)}，兼容归档（含 video/ 层）与出片两种目录结构。"""
    allowed = set()
    for p in paths:
        m = re.search(r'runs/([^/]+)/(?:video/)?(version_[a-z])/video\.mp4$',
                      str(p).strip().replace('\\', '/'))
        if m:
            allowed.add((m.group(1), m.group(2)))
    return allowed


ALLOWED_VIDEOS = _parse_showcase(SHOWCASE_VIDEOS)


def video_allowed(run_id, version):
    """白名单为空时不限制；否则只放行列出的成片。"""
    if not ALLOWED_VIDEOS:
        return True
    return (run_id, version) in ALLOWED_VIDEOS


def all_run_dirs():
    """归档目录 + 出片目录下的所有 run 目录，按修改时间倒序（同名以归档为准）。"""
    found = {}
    for root in (ARCHIVE_RUNS, PROD_RUNS):
        try:
            for e in root.iterdir():
                if e.is_dir():
                    found.setdefault(e.name, e)
        except Exception:
            continue
    try:
        return sorted(found.values(), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return list(found.values())


STATE_F = BASE / 'state.json'
CONFIG_F = BASE / 'config.json'
ACCOUNTS_F = BASE / 'accounts.json'
QUEUE_F = BASE / 'queue.json'
PKG_ROOT = BASE / 'packages'
PORT = int(os.environ.get('CONSOLE_PORT', '20130'))
WF_ID = os.environ.get('WF_ID', 'nasFullPipelineManual')

# 下游服务地址（默认值为占位，部署时用环境变量覆盖）
N8N_URL = os.environ.get('N8N_URL', 'http://localhost:5678')
EXECUTOR_HOST = os.environ.get('EXECUTOR_HOST', '127.0.0.1')
EXECUTOR_PORT = int(os.environ.get('EXECUTOR_PORT', '18764'))
EXECUTOR_URL = os.environ.get('EXECUTOR_URL', 'http://%s:%d' % (EXECUTOR_HOST, EXECUTOR_PORT))
MODEL_GATEWAY_URL = os.environ.get('MODEL_GATEWAY_URL', 'http://localhost:20128')

STAGE_DIRS = [
    ('research', '市场研究', ['03-market-research/reviews', '03-market-research/runs']),
    ('selection', '选题', ['03-topic-selection/runs', '03-topic-selection/projects']),
    ('planning', '方案规划', ['03-project-planning/runs', '03-project-planning/projects']),
    ('development', '软件开发', ['04-development-agent/runs']),
    ('acceptance', '独立验收', ['05-software-acceptance/runs']),
    ('recording', '屏幕录制', ['06-screen-recording/runs']),
    ('video', '视频合成', ['07-video-production/runs']),
    ('archive', '归档复盘', ['08-review-archive/runs']),
]

_lock = threading.Lock()


def sh(cmd, t=40):
    try:
        p = subprocess.run(['bash', '-lc', cmd], capture_output=True, timeout=t)
        return p.returncode, (p.stdout or b'').decode('utf-8', 'replace').strip(), (p.stderr or b'').decode('utf-8', 'replace').strip()
    except subprocess.TimeoutExpired:
        return 124, '', 'timeout'
    except Exception as e:
        return 1, '', str(e)


def jread(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding='utf-8'))
    except Exception:
        return default


def jwrite(p, data):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def state():
    s = jread(STATE_F, {})
    return s if isinstance(s, dict) else {}


def mask(s, keep=4):
    if not s:
        return ''
    if len(s) <= keep * 2:
        return s[:2] + '*' * (len(s) - 2)
    return s[:keep] + '…' + s[-keep:]


# ---------- 真实状态采集 ----------

def n8n_status():
    rc, out, _ = sh("docker ps --filter name='^n8n$' --format '{{.Status}}'")
    up = bool(out)
    ver = ''
    if up:
        rc2, v, _ = sh("docker exec n8n n8n --version 2>/dev/null | head -1", 10)
        ver = v
    return {'up': up, 'status': out or 'not running', 'version': ver,
            'url': N8N_URL}


def executor_status():
    try:
        s = socket.create_connection((EXECUTOR_HOST, EXECUTOR_PORT), timeout=3)
        s.close()
        up = True
    except Exception:
        up = False
    svc = ''
    if up:
        rc, out, _ = sh("XDG_RUNTIME_DIR=/run/user/1000 systemctl --user is-active nas-agent-executor 2>/dev/null")
        svc = out or 'unknown'
    return {'up': up, 'service': svc, 'url': EXECUTOR_URL}


STAGE_ROLES = [('development', '开发（重编码）'), ('research', '调研'), ('selection', '选题'),
               ('planning', '规划'), ('video_plan', '解说脚本'), ('archive', '归档')]


def api_stage_models():
    """读取已注册模型与按阶段的模型映射。"""
    m = jread(EXEC_ROOT / 'pi-agent/models.json', {}) or {}
    prov = ((m.get('providers') or {}).get('nasrouter') or {})
    ids = [x.get('id') for x in (prov.get('models') or []) if x.get('id')]
    mp = jread(EXEC_ROOT / 'model_map.json', {}) or {}
    return {'default': ids[0] if ids else '', 'available': ids,
            'map': mp if isinstance(mp, dict) else {},
            'roles': [{'role': r, 'label': l, 'model': (mp or {}).get(r) or ''} for r, l in STAGE_ROLES]}


def api_stage_models_save(body):
    role = str(body.get('role') or '')
    model = str(body.get('model') or '')
    known = {r for r, _ in STAGE_ROLES}
    if role not in known:
        return {'ok': False, 'error': '未知阶段'}
    st = api_stage_models()
    if model and model not in st['available']:
        return {'ok': False,
                'error': '模型未在 models.json 注册，执行器 CLI 不会认。先注册再选。'}
    mp = jread(EXEC_ROOT / 'model_map.json', {}) or {}
    if not isinstance(mp, dict):
        mp = {}
    if model:
        mp[role] = model
    else:
        mp.pop(role, None)
    jwrite(EXEC_ROOT / 'model_map.json', mp)
    rc, out, err = sh("systemctl --user restart nas-agent-executor && sleep 3 "
                      "&& systemctl --user is-active nas-agent-executor", 60)
    return {'ok': True, 'map': mp, 'service': out.strip()[:40], 'note': '未指定阶段的角色继续用默认模型'}


def model_status():
    m = jread(EXEC_ROOT / 'pi-agent/models.json', {}) or {}
    prov = ((m.get('providers') or {}).get('nasrouter') or {})
    mid = ''
    models = prov.get('models') or []
    if models:
        mid = models[0].get('id', '')
    rc, key, _ = sh("cat %s 2>/dev/null" % shq(str(EXEC_ROOT / 'secrets/model-key')), 5)
    key = key.strip()
    return {'baseUrl': prov.get('baseUrl', ''), 'model': mid,
            'key_masked': mask(key), 'key_len': len(key)}


def sandbox_status():
    rc, out, _ = sh("docker ps --filter name=sandbox --format '{{.Names}}|{{.Status}}'")
    items = [l for l in out.splitlines() if l.strip()]
    return {'containers': items, 'ok': len(items) >= 2}


def pipeline_alive():
    rc, out, _ = sh("docker exec n8n sh -c \"ps -ef | grep 'n8n execute' | grep -v grep | wc -l\"", 15)
    try:
        return int(out.strip() or '0') > 0
    except Exception:
        return False


def stage_activity():
    """每个阶段：最新目录名 + mtime（真实文件系统扫描）"""
    out = []
    now = time.time()
    for key, label, dirs in STAGE_DIRS:
        latest, mt = None, 0
        for d in dirs:
            p = WF_ROOT / d
            if not p.is_dir():
                continue
            try:
                for c in p.iterdir():
                    if not c.name.startswith(('sandbox-20261107-002', 'voice17-smoke')):
                        m = c.stat().st_mtime
                        if m > mt:
                            mt, latest = m, c.name
            except Exception:
                pass
        out.append({'key': key, 'label': label, 'latest': latest,
                    'mtime': mt, 'age_min': round((now - mt) / 60, 1) if mt else None})
    return out


def latest_run_summary(n=1):
    """最近一次带成片的运行。配置了白名单时只在白名单成片所在的运行里挑。"""
    runs = []
    for e in all_run_dirs():
        m = jread(e / 'archive-manifest.json', {}) or {}
        videos = []
        for v in ('version_a', 'version_b'):
            if not video_allowed(e.name, v):
                continue
            vd = resolve_version_dir(e.name, v)
            if not vd:
                continue
            f = vd / 'video.mp4'
            videos.append({'version': v, 'size': f.stat().st_size,
                           'mtime': time.strftime('%m-%d %H:%M', time.localtime(f.stat().st_mtime)),
                           'path': str(f.relative_to(WF_ROOT))})
        if ALLOWED_VIDEOS and not videos:
            continue
        runs.append({'run_id': e.name,
                     'time': time.strftime('%Y-%m-%d %H:%M', time.localtime(e.stat().st_mtime)),
                     'status': m.get('status'), 'publishable': m.get('publishable'),
                     'blocking': m.get('blocking', []),
                     'project': m.get('project_id'),
                     'videos': videos,
                     'screenshot': str((e / 'acceptance/screenshot.png').relative_to(WF_ROOT))
                     if (e / 'acceptance/screenshot.png').is_file() else None})
        if len(runs) >= n:
            break
    return runs


def probe_video(path):
    """ffprobe 读分辨率与时长。流信息与容器时长可能分行输出，逐项取数字更稳。"""
    rc, out, _ = sh("ffprobe -v error -select_streams v:0 -show_entries stream=width,height "
                    "-of csv=p=0 %s 2>/dev/null" % shq(path), 15)
    nums = re.findall(r'\d+', out or '')
    if len(nums) < 2:
        return None
    try:
        w, h = int(nums[0]), int(nums[1])
    except ValueError:
        return None
    rc2, out2, _ = sh("ffprobe -v error -show_entries format=duration -of csv=p=0 %s 2>/dev/null"
                      % shq(path), 15)
    m = re.search(r'\d+\.?\d*', out2 or '')
    dur = float(m.group(0)) if m else None
    return {'w': w, 'h': h, 'duration': dur,
            'ratio': '9:16' if abs(w / h - 9 / 16) < 0.02 else '3:4' if abs(w / h - 3 / 4) < 0.02 else '%.2f' % (w / h)}


# ---------- 工作流选题注入 ----------

def patch_topic(query, ctx):
    """把选题方向写进工作流 Discovery Submit 节点（真实修改 workflow_entity.nodes）"""
    qj, cj = json.dumps(query, ensure_ascii=False), json.dumps(ctx, ensure_ascii=False)
    body = ("={{ JSON.stringify({role:'research_discovery',project_id:'nas-49',"
            "inputs:{\"query\":%s,\"business_context\":%s},timeout_seconds:300,output_type:'text'}) }}" % (qj, cj))
    DB = "docker exec n8n-postgres psql -U n8n -d n8n -At -c"
    rc, out, _ = sh("%s \"select nodes from workflow_entity where id='%s';\"" % (DB, WF_ID))
    if not out:
        return False, 'read workflow failed'
    try:
        nodes = json.loads(out)
    except Exception:
        return False, 'workflow nodes parse failed'
    hit = False
    for nd in nodes:
        if nd.get('name') == 'Discovery Submit':
            nd.setdefault('parameters', {})['jsonBody'] = body
            hit = True
    if not hit:
        return False, 'Discovery Submit node not found'
    stamp = time.strftime('%Y%m%d-%H%M%S')
    backup = BACKUP_DIR / ('wf-nodes-backup-%s.json' % stamp)
    sh("docker exec n8n-postgres psql -U n8n -d n8n -At -c \"select nodes from workflow_entity where id='%s';\" > %s" % (WF_ID, shq(str(backup))))
    sql = "update workflow_entity set nodes='%s', \"updatedAt\"=now() where id='%s';" % (
        json.dumps(nodes, ensure_ascii=False).replace("'", "''"), WF_ID)
    open('/tmp/wf-topic.sql', 'w', encoding='utf-8').write(sql)
    rc, out, _ = sh("docker cp /tmp/wf-topic.sql n8n-postgres:/tmp/wf-topic.sql && "
                    "docker exec n8n-postgres psql -U n8n -d n8n -f /tmp/wf-topic.sql")
    return rc == 0, ('ok' if rc == 0 else 'update failed: %s' % out[:200])


# ---------- 模型网关 ----------

def gateway_models(base):
    rc, key, _ = sh("cat %s 2>/dev/null" % shq(str(EXEC_ROOT / 'secrets/model-key')), 5)
    key = key.strip()
    try:
        import urllib.request
        req = urllib.request.Request(base.rstrip('/') + '/models',
                                     headers={'Authorization': 'Bearer ' + key})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.load(r)
        return [m.get('id') for m in (d.get('data') or d.get('models') or []) if isinstance(m, dict) and m.get('id')], None
    except Exception as e:
        return None, str(e)[:200]


def model_test(base, model, key=None):
    if not key:
        rc, key, _ = sh("cat %s 2>/dev/null" % shq(str(EXEC_ROOT / 'secrets/model-key')), 5)
        key = key.strip()
    body = json.dumps({'model': model, 'messages': [{'role': 'user', 'content': 'Reply with OK.'}],
                       'max_tokens': 16, 'stream': False}).encode()
    t0 = time.time()
    try:
        import urllib.request
        req = urllib.request.Request(base.rstrip('/') + '/chat/completions', data=body,
                                     headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.load(r)
        txt = (((d.get('choices') or [{}])[0].get('message') or {}).get('content') or '')[:60]
        return {'ok': True, 'reply': txt, 'latency': round(time.time() - t0, 1)}
    except Exception as e:
        return {'ok': False, 'error': str(e)[:200]}


# ---------- 执行记录（n8n DB 直查） ----------

def db_executions(limit=40):
    rc, out, _ = sh("docker exec n8n-postgres psql -U n8n -d n8n -At -F '|' -c "
                    "\"select id,status,mode,coalesce(\\\"startedAt\\\"::text,''),coalesce(\\\"stoppedAt\\\"::text,'') "
                    "from execution_entity order by id desc limit %d;\"" % limit)
    rows = []
    for l in out.splitlines():
        p = l.split('|')
        if len(p) >= 5:
            rows.append({'id': int(p[0]), 'status': p[1], 'mode': p[2], 'started': p[3][:19], 'stopped': p[4][:19]})
    return rows


# ---------- API 处理 ----------


VOICE_DIR = WF_ROOT / '07-video-production'
VOICE_ACTIVE = VOICE_DIR / 'voice-settings.json'
VOICE_PRESETS = VOICE_DIR / 'voice-presets'


def api_voice_get():
    current = {}
    try:
        if VOICE_ACTIVE.is_file() and not VOICE_ACTIVE.is_symlink() and VOICE_ACTIVE.stat().st_size <= 5000:
            current = json.loads(VOICE_ACTIVE.read_text(encoding='utf-8'))
    except Exception:
        current = {}
    presets = []
    try:
        for f in sorted(VOICE_PRESETS.glob('*.json')):
            if f.is_symlink() or f.stat().st_size > 5000:
                continue
            try:
                d = json.loads(f.read_text(encoding='utf-8'))
            except Exception:
                continue
            presets.append({
                'id': f.stem, 'label': d.get('label', f.stem), 'engine': d.get('engine'),
                'voice': d.get('voice'), 'rate': d.get('rate'), 'pitch': d.get('pitch'),
                'note': d.get('note', ''), 'license': d.get('license', ''),
                'online': bool(d.get('online')),
            })
    except Exception:
        pass
    active = None
    for item in presets:
        if (item['engine'] == current.get('engine')
                and (item['voice'] or None) == (current.get('voice') or None)
                and item['rate'] == current.get('rate')
                and item['pitch'] == current.get('pitch')):
            active = item['id']
            break
    return {
        'presets': presets, 'active': active, 'current': current,
        'note': '切换只影响下一次视频合成，不会重启执行器，也不会中断正在运行的任务。',
    }


def api_voice_save(body):
    pid = (body or {}).get('preset', '')
    if not isinstance(pid, str) or not re.fullmatch(r'[a-z0-9_-]{1,40}', pid):
        return {'ok': False, 'error': 'invalid preset'}
    f = VOICE_PRESETS / (pid + '.json')
    if f.is_symlink() or not f.is_file() or f.stat().st_size > 5000:
        return {'ok': False, 'error': 'preset not found'}
    try:
        d = json.loads(f.read_text(encoding='utf-8'))
    except Exception:
        return {'ok': False, 'error': 'invalid preset file'}
    allowed = {'engine', 'speaker', 'language', 'instruct', 'status',
               'synthesis_mode', 'voice', 'rate', 'pitch'}
    clean = {k: v for k, v in d.items() if k in allowed}
    if clean.get('status') != 'test_only':
        return {'ok': False, 'error': 'preset must be test_only'}
    with _lock:
        if VOICE_ACTIVE.exists():
            shutil.copy2(VOICE_ACTIVE, str(VOICE_ACTIVE) + '.before-voice-' + time.strftime('%Y%m%d-%H%M%S'))
        tmp = VOICE_ACTIVE.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        tmp.chmod(0o644)
        os.replace(tmp, VOICE_ACTIVE)
    return {'ok': True, 'message': '已切换：' + d.get('label', pid), 'preset': pid}

def api_overview():
    st = state()
    runs = latest_run_summary(1)
    return {
        'time': time.strftime('%Y-%m-%d %H:%M:%S'),
        'n8n': n8n_status(),
        'executor': executor_status(),
        'model': model_status(),
        'sandbox': sandbox_status(),
        'pipeline': {'running': pipeline_alive(),
                     'started_at': st.get('started_at'),
                     'log': st.get('log')},
        'stages': stage_activity(),
        'latest_run': runs[0] if runs else None,
    }


def api_config_get():
    cfg = jread(CONFIG_F, {}) or {}
    if not cfg:
        # 初始化默认值 = 工作流当前真实值
        DB = "docker exec n8n-postgres psql -U n8n -d n8n -At -c"
        rc, out, _ = sh("%s \"select nodes from workflow_entity where id='%s';\"" % (DB, WF_ID))
        q, b = 'workflow automation', 'B2B workflow automation; customer demand not yet established.'
        try:
            for nd in json.loads(out):
                if nd.get('name') == 'Discovery Submit':
                    m = re.search(r'"query":"([^"]*)".*"business_context":"([^"]*)"', nd['parameters'].get('jsonBody', ''))
                    if m:
                        q, b = m.group(1), m.group(2)
        except Exception:
            pass
        cfg = {'topic': {'query': q, 'business_context': b}}
        jwrite(CONFIG_F, cfg)
    return cfg


def api_config_save(body):
    topic = body.get('topic') or {}
    q = str(topic.get('query') or '').strip()
    b = str(topic.get('business_context') or '').strip()
    if not q or len(q) > 120:
        return {'ok': False, 'error': '选题方向 query 必填且 ≤120 字符'}
    if len(b) > 2000:
        return {'ok': False, 'error': '业务背景 ≤2000 字符'}
    cfg = jread(CONFIG_F, {}) or {}
    cfg['topic'] = {'query': q, 'business_context': b}
    jwrite(CONFIG_F, cfg)
    ok, msg = patch_topic(q, b)
    return {'ok': ok, 'message': '已保存并写入工作流 Discovery 节点: %s' % msg}


def api_model_save(body):
    base = str(body.get('baseUrl') or '').strip()
    model = str(body.get('model') or '').strip()
    key = str(body.get('key') or '').strip()
    if not re.match(r'^https?://[0-9A-Za-z.:-]+/v1/?$', base):
        return {'ok': False, 'error': 'baseUrl 需形如 http://host:port/v1'}
    if not model or len(model) > 60:
        return {'ok': False, 'error': '模型名必填'}
    test = model_test(base, model, key=key or None)
    if not test['ok']:
        return {'ok': False, 'error': '连通测试失败: %s' % test.get('error')}
    # 备份并写入
    stamp = time.strftime('%Y%m%d-%H%M%S')
    mp = EXEC_ROOT / 'pi-agent/models.json'
    sh("cp %s %s.before-console-%s" % (shq(str(mp)), shq(str(mp)), stamp))
    m = jread(mp, {}) or {}
    prov = ((m.get('providers') or {}).get('nasrouter') or {})
    prov['baseUrl'] = base
    models = prov.get('models') or [{}]
    models[0]['id'] = model
    models[0]['name'] = model
    prov['models'] = models
    m.setdefault('providers', {})['nasrouter'] = prov
    jwrite(mp, m)
    if key:
        kp = EXEC_ROOT / 'secrets/model-key'
        sh("cp %s %s.before-console-%s" % (shq(str(kp)), shq(str(kp)), stamp))
        kp.write_text(key + '\n', encoding='utf-8')
    rc, out, _ = sh("XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart nas-agent-executor")
    time.sleep(3)
    ex = executor_status()
    return {'ok': True, 'test': test, 'executor_restarted': ex['up'],
            'message': '模型配置已生效，执行器已重启'}


def api_pipeline_start():
    if pipeline_alive():
        return {'ok': False, 'error': '流水线已在运行中'}
    cfg = jread(CONFIG_F, {}) or {}
    topic = cfg.get('topic') or {}
    if topic.get('query'):
        patch_topic(topic['query'], topic.get('business_context') or '')
    ts = time.strftime('%Y%m%d-%H%M%S')
    logf = '/tmp/n8n-cli-%s.log' % ts
    rc, _, err = sh("docker exec -d n8n sh -c 'N8N_RUNNERS_BROKER_PORT=5681 "
                    "n8n execute --id=%s --rawOutput > %s 2>&1'" % (WF_ID, logf))
    if rc != 0:
        return {'ok': False, 'error': '启动失败: %s' % err[:200]}
    jwrite(STATE_F, {'log': logf, 'started_at': time.strftime('%Y-%m-%d %H:%M:%S')})
    return {'ok': True, 'message': '流水线已启动（n8n 引擎执行）', 'log': logf}


def newest_cli_log():
    """容器里最新的一次 CLI 执行日志。控制台之外（手敲 n8n execute）启动时也能对齐。"""
    rc, out, _ = sh("docker exec n8n sh -c 'ls -t /tmp/n8n-cli-*.log 2>/dev/null | head -1'", 15)
    line = (out or '').strip().splitlines()
    return line[-1].strip() if line else ''


def log_started_at(path):
    m = re.search(r'n8n-cli-(\d{8})-(\d{6})\.log$', path or '')
    if not m:
        return ''
    d, t = m.group(1), m.group(2)
    return '%s-%s-%s %s:%s' % (d[:4], d[4:6], d[6:8], t[:2], t[2:4])


def api_pipeline_status():
    st = state()
    alive = pipeline_alive()
    log = st.get('log') or ''
    started_at = st.get('started_at')
    fresh = newest_cli_log()
    if alive and fresh and fresh != log:
        log, started_at = fresh, log_started_at(fresh) or started_at
    tail = ''
    if log:
        rc, out, _ = sh("docker exec n8n sh -c 'tail -c 3000 %s 2>/dev/null'" % shq(log), 15)
        tail = out
    return {'running': alive, 'started_at': started_at,
            'log': log, 'log_tail': tail,
            'stages': stage_activity(), 'latest_run': (latest_run_summary(1) or [None])[0]}


def resolve_version_dir(run, version):
    """成片可能在归档目录（含 video/ 层）或出片目录（无 video/ 层），两处都找。"""
    for cand in (ARCHIVE_RUNS / run / 'video' / version, PROD_RUNS / run / version):
        if (cand / 'video.mp4').is_file():
            return cand
    return None


def api_runs():
    """成片列表。默认扫描归档 + 出片目录；配置了白名单时只返回白名单内的成片。"""
    out = []
    if ALLOWED_VIDEOS:
        names = [p.name for p in all_run_dirs()]
    else:
        seen = {}
        for root in (ARCHIVE_RUNS, PROD_RUNS):
            try:
                entries = sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
            except Exception:
                continue
            for e in entries[:12]:
                if not e.is_dir() or e.name in seen:
                    continue
                seen[e.name] = True
        names = sorted(seen, key=lambda n: max(
            (ARCHIVE_RUNS / n).stat().st_mtime if (ARCHIVE_RUNS / n).is_dir() else 0,
            (PROD_RUNS / n).stat().st_mtime if (PROD_RUNS / n).is_dir() else 0), reverse=True)[:12]
    for i, name in enumerate(names):
        m = jread(ARCHIVE_RUNS / name / 'archive-manifest.json', {}) or {}
        base = ARCHIVE_RUNS / name if (ARCHIVE_RUNS / name).is_dir() else PROD_RUNS / name
        item = {'run_id': name,
                'time': time.strftime('%Y-%m-%d %H:%M', time.localtime(base.stat().st_mtime)),
                'status': m.get('status'), 'publishable': m.get('publishable'),
                'archived': (ARCHIVE_RUNS / name).is_dir(),
                'blocking': m.get('blocking', []), 'videos': []}
        for v in ('version_a', 'version_b'):
            if not video_allowed(name, v):
                continue
            vd0 = resolve_version_dir(name, v)
            if not vd0:
                continue
            f = vd0 / 'video.mp4'
            vd = {'version': v, 'size': f.stat().st_size,
                  'media': str(f.relative_to(WF_ROOT))}
            if i < 5:
                pr = probe_video(str(f))
                if pr:
                    vd.update(pr)
            item['videos'].append(vd)
        if ALLOWED_VIDEOS and not item['videos']:
            continue
        ss = ARCHIVE_RUNS / name / 'acceptance/screenshot.png'
        if ss.is_file():
            item['screenshot'] = str(ss.relative_to(WF_ROOT))
        out.append(item)
    return {'runs': out}


ROLE_LABEL = {
    'research': '调研', 'selection': '选题', 'planning': '规划',
    'development': '开发', 'desktop_acceptance': '验收', 'desktop_recording': '录屏',
    'video_plan': '解说脚本', 'test_video_production': '出片', 'archive': '归档',
    'research_discovery': '线索发现', 'independent_ui_case': '独立 UI 用例',
    'multimodal_evidence_review': '图像证据复核',
}


def task_stats():
    """从执行器任务库统计阶段健康度与自动重试分布（只读）。"""
    db = EXEC_ROOT / 'tasks.sqlite'
    if not db.is_file():
        return {'roles': [], 'recent': [], 'retried': 0, 'total': 0}
    try:
        conn = sqlite3.connect('file:%s?mode=ro' % db, uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return {'roles': [], 'recent': [], 'retried': 0, 'total': 0}
    try:
        rows = conn.execute(
            "SELECT id,payload,status,error,created,updated,result FROM tasks "
            "ORDER BY created DESC LIMIT 300").fetchall()
    except sqlite3.Error:
        conn.close()
        return {'roles': [], 'recent': [], 'retried': 0, 'total': 0}
    conn.close()
    agg = {}
    recent, retried = [], 0
    for r in rows:
        try:
            payload = json.loads(r['payload'] or '{}')
        except ValueError:
            payload = {}
        role = payload.get('role') or 'unknown'
        attempts = int(payload.get('_retry', 0) or 0)
        if attempts:
            retried += 1
        a = agg.setdefault(role, {'role': role, 'label': ROLE_LABEL.get(role, role),
                                  'success': 0, 'failed': 0, 'other': 0, 'retried': 0})
        if attempts:
            a['retried'] += 1
        st = r['status']
        err = r['error'] or ''
        # 开发交付后交接独立验收是正常终态，不算失败
        handoff = st == 'failed' and err == 'ui_acceptance_pending'
        if st == 'succeeded' or handoff:
            a['success'] += 1
        elif st == 'failed':
            a['failed'] += 1
        else:
            a['other'] += 1
        # 结论确定的任务才进「最近失败与重试」表
        if st in ('succeeded', 'failed') and not handoff and (st == 'failed' or attempts) and len(recent) < 14:
            recent.append({
                'role': role, 'label': ROLE_LABEL.get(role, role),
                'status': st, 'error': err,
                'attempts': attempts,
                'updated': time.strftime('%m-%d %H:%M', time.localtime(r['updated'] or r['created'] or 0)),
            })
    for a in agg.values():
        done = a['success'] + a['failed']
        a['rate'] = round(a['success'] * 100.0 / done, 1) if done else None
    roles = sorted(agg.values(), key=lambda x: -(x['success'] + x['failed'] + x['other']))
    return {'roles': roles, 'recent': recent, 'retried': retried, 'total': len(rows)}


def api_analysis():
    execs = db_executions(60)
    ok = [e for e in execs if e['status'] == 'success']
    err = [e for e in execs if e['status'] == 'error']
    durs = []
    for e in ok:
        try:
            t0 = time.mktime(time.strptime(e['started'][:19], '%Y-%m-%d %H:%M:%S'))
            t1 = time.mktime(time.strptime(e['stopped'][:19], '%Y-%m-%d %H:%M:%S'))
            if t1 > t0:
                durs.append(round((t1 - t0) / 60, 1))
        except Exception:
            pass
    total_runs = 0
    try:
        total_runs = len(list(ARCHIVE_RUNS.iterdir()))
    except Exception:
        pass
    # 按天统计
    daily = {}
    for e in execs:
        d = e['started'][:10]
        daily.setdefault(d, {'success': 0, 'error': 0, 'other': 0})
        if e['status'] == 'success':
            daily[d]['success'] += 1
        elif e['status'] == 'error':
            daily[d]['error'] += 1
        else:
            daily[d]['other'] += 1
    daily = [{'date': k, **v} for k, v in sorted(daily.items())][-14:]
    return {'executions': execs[:30], 'total': len(execs),
            'success': len(ok), 'error': len(err),
            'avg_min': round(sum(durs) / len(durs), 1) if durs else None,
            'durations': durs, 'archive_runs': total_runs, 'daily': daily,
            'tasks': task_stats()}


def publish_default():
    return {
        'douyin': {'platform': '抖音', 'status': 'not_logged_in', 'note': '未接入平台开放接口，登录需后续接入自动化或官方 API'},
        'xhs': {'platform': '小红书', 'status': 'not_logged_in', 'note': '同上；发布包会另行生成 1080×1440 的 3:4 安全版（字幕按新画布重排并实测安全区）'}
    }


def api_publish_get():
    acc = jread(ACCOUNTS_F) or publish_default()
    if not acc:
        acc = publish_default()
    q = jread(QUEUE_F) or {'items': []}
    if not isinstance(q, dict) or 'items' not in q:
        q = {'items': []}
    return {'accounts': acc, 'queue': q['items']}


def api_publish_queue_add(body):
    run = str(body.get('run') or '')
    version = str(body.get('version') or '')
    platform = str(body.get('platform') or '')
    caption = str(body.get('caption') or '')
    if platform not in ('douyin', 'xhs') or version not in ('version_a', 'version_b'):
        return {'ok': False, 'error': '平台或版本参数无效'}
    vd = resolve_version_dir(run, version)
    if not vd:
        return {'ok': False, 'error': '视频文件不存在'}
    f = vd / 'video.mp4'
    pr = probe_video(str(f)) or {}
    checks = []
    if platform == 'douyin':
        checks.append({'name': '9:16 竖屏', 'pass': pr.get('ratio') == '9:16'})
    if platform == 'xhs':
        checks.append({'name': '小红书 3:4 或中央安全区适配', 'pass': pr.get('ratio') in ('3:4', '9:16'), 'warn': pr.get('ratio') != '3:4'})
    q = jread(QUEUE_F) or {'items': []}
    item = {'id': uuid.uuid4().hex[:12], 'run': run, 'version': version, 'platform': platform,
            'caption': caption[:500], 'created_at': time.strftime('%m-%d %H:%M'),
            'status': 'pending_auth', 'checks': checks,
            'media': str(f.relative_to(WF_ROOT))}
    q['items'].append(item)
    jwrite(QUEUE_F, q)
    return {'ok': True, 'item': item}


def api_publish_send(body):
    qid = str(body.get('id') or '')
    q = jread(QUEUE_F) or {'items': []}
    acc = jread(ACCOUNTS_F) or publish_default()
    for it in q['items']:
        if it['id'] == qid:
            a = acc.get(it['platform'], {})
            if a.get('status') != 'logged_in':
                it['status'] = 'pending_auth'
                jwrite(QUEUE_F, q)
                return {'ok': False,
                        'error': '账号未登录：当前部署未接入 %s 的登录与发布接口，未执行任何发布动作。' % a.get('platform', it['platform'])}
            return {'ok': False, 'error': '已登录但发布通道尚未接入'}
    return {'ok': False, 'error': '队列项不存在'}


def srt_sentences(path):
    """从 srt 取出解说句子，作为发布文案的唯一来源（不生成、不编造）。"""
    out = []
    try:
        text = Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return out
    for block in re.split(r'\n\s*\n', text.strip()):
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        body = [l for l in lines if not re.match(r'^\d+$', l) and '-->' not in l]
        if body:
            out.append(''.join(body))
    return out


def pkg_dir(platform, version, run):
    stamp = time.strftime('%m%d-%H%M%S')
    return PKG_ROOT / ('%s-%s-%s-%s' % (platform, version, run[-8:], stamp))


# ---------- 小红书 3:4 母版：真实重排，不是缩放裁切 ----------
# 9:16 母版的版式是固定的：1080x900 画面居中（y=510..1410），字幕带在 1560..1820，
# 品牌行在顶部 168。取回画面区后可以在 1080x1440 画布上重新排版字幕。
XHS_W, XHS_H = 1080, 1440
XHS_REC_Y = 180          # 画面顶边；180..1080 放录制区
XHS_CAP_MARGINV = 252    # 字幕底边距画布底部：252/1440 = 17.5%，高于小红书底部文案区
XHS_CAP_MARGINLR = 104
XHS_SAFE_BOTTOM_PCT = 12.0


def _srt_seconds(token):
    m = re.match(r'\s*(\d+):(\d+):(\d+)[,.](\d+)', token or '')
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000.0


def srt_cues(path):
    """带时间轴的字幕片段：3:4 重排必须知道每句话何时出现。"""
    cues = []
    try:
        text = Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return cues
    for block in re.split(r'\n\s*\n', text.strip()):
        lines = [l for l in block.splitlines() if l.strip()]
        if len(lines) < 3 or '-->' not in lines[1]:
            continue
        a, b = lines[1].split('-->')
        body = ' '.join(lines[2:]).strip()
        if body:
            cues.append((_srt_seconds(a), _srt_seconds(b), body))
    return cues


def _ass_clock(t):
    ms = max(0, round(float(t) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return '%d:%02d:%02d.%02d' % (h, m, s, ms // 10)


def xhs_ass(cues, path):
    head = ['[Script Info]', 'ScriptType: v4.00+', 'PlayResX: %d' % XHS_W,
            'PlayResY: %d' % XHS_H, 'WrapStyle: 0', 'ScaledBorderAndShadow: yes', '',
            '[V4+ Styles]',
            'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, '
            'BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, '
            'BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding',
            'Style: Caption,Noto Sans CJK SC,52,&H00FFFFFF,&H00FFFFFF,&H0012161C,&H00000000,'
            '-1,0,0,0,100,100,1,0,1,3,0,2,%d,%d,%d,1' % (XHS_CAP_MARGINLR, XHS_CAP_MARGINLR, XHS_CAP_MARGINV),
            '', '[Events]', 'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text']
    for start, end, text in cues:
        safe = text.replace('\\', '／').replace('{', '（').replace('}', '）').replace('\n', ' ')
        head.append('Dialogue: 0,%s,%s,Caption,,0,0,0,,%s' % (_ass_clock(start), _ass_clock(end), safe))
    Path(path).write_text('\n'.join(head) + '\n', encoding='utf-8')


def xhs_caption_report(video, cues):
    """逐句抽样量字幕位置：返回实测底边距、左右留白与是否贴边。"""
    y, h = XHS_H - 420, 420
    worst = None
    missing = 0
    for start, end, _t in cues:
        mid = (start + end) / 2.0
        raw_path = Path('/tmp') / ('_xhsband-%s.raw' % uuid.uuid4().hex[:8])
        rc, _o, _e = sh('ffmpeg -v error -ss %.3f -i %s -vf crop=%d:%d:0:%d -frames:v 1 '
                        '-pix_fmt gray -f rawvideo %s'
                        % (max(0.0, mid), shq(str(video)), XHS_W, h, y, shq(str(raw_path))), 40)
        try:
            raw = raw_path.read_bytes()
        except OSError:
            raw = b''
        finally:
            raw_path.unlink(missing_ok=True)
        if len(raw) != XHS_W * h:
            missing += 1
            continue
        xs, ys = [], []
        for i, v in enumerate(raw):
            if v >= 200:
                xs.append(i % XHS_W)
                ys.append(i // XHS_W)
        if len(xs) < 32:
            missing += 1
            continue
        item = {'bottom_px': XHS_H - (y + max(ys)),
                'bottom_pct': round((XHS_H - (y + max(ys))) * 100.0 / XHS_H, 1),
                'left_px': min(xs), 'right_gap_px': XHS_W - max(xs), 'top_px': y + min(ys)}
        if worst is None or item['bottom_pct'] < worst['bottom_pct']:
            worst = item
    if worst is None:
        return {'ok': False, 'cues': len(cues), 'missing': missing, 'detail': '未能在成品里量到字幕像素'}
    ok = (worst['bottom_pct'] >= XHS_SAFE_BOTTOM_PCT and worst['left_px'] >= 60
          and worst['right_gap_px'] >= 60 and missing == 0)
    return {'ok': ok, 'cues': len(cues), 'missing': missing, **worst}


def build_xhs_3x4(src, cues, dst, workdir):
    """把 9:16 母版的画面区取回，在 1080x1440 画布上重排字幕。"""
    ass = Path(workdir) / 'xhs_1080x1440.ass'
    xhs_ass(cues, ass)
    vf = ('crop=1080:900:0:510,pad=%d:%d:0:%d:color=0x171923,subtitles=%s'
          % (XHS_W, XHS_H, XHS_REC_Y, ass.as_posix().replace(':', '\\:')))
    rc, out, err = sh('ffmpeg -y -v error -i %s -vf "%s" -c:v libx264 -preset veryfast -crf 20 '
                      '-pix_fmt yuv420p -c:a copy -movflags +faststart %s'
                      % (shq(str(src)), vf, shq(str(dst))), 600)
    return rc, err, ass


def api_publish_package(body):
    """生成真实可用的发布包：平台适配校验 + 封面帧 + 文案 + 清单。不做任何上传。"""
    run = str(body.get('run') or '')
    version = str(body.get('version') or '')
    platform = str(body.get('platform') or '')
    if version not in ('version_a', 'version_b') or platform not in ('douyin', 'xhs', 'both'):
        return {'ok': False, 'error': '版本或平台参数无效'}
    if not re.fullmatch(r'api-[0-9a-f]{32}', run):
        return {'ok': False, 'error': '成片目录名不合法'}
    vdir = resolve_version_dir(run, version)
    if not vdir:
        return {'ok': False, 'error': '视频文件不存在'}
    src = vdir / 'video.mp4'
    pr = probe_video(str(src)) or {}
    if pr.get('ratio') != '9:16':
        return {'ok': False, 'error': '母版不是 9:16，拒绝按竖屏发布包处理'}

    out = pkg_dir(platform, version, run)
    out.mkdir(parents=True, exist_ok=True)
    made, notes, checks = [], [], []
    dur = float(pr.get('duration') or 0)
    shot = max(1.0, round(dur * 0.32, 2))
    sentences = srt_sentences(vdir / 'subtitles.srt')
    title = (sentences[0] if sentences else '')[:22]

    def hashes(files):
        return [{'name': f.name, 'bytes': f.stat().st_size,
                 'sha256': hashlib.sha256(f.read_bytes()).hexdigest()} for f in files]

    # ---- 抖音 9:16 母版：原样导出 + 抽封面帧 ----
    if platform in ('douyin', 'both'):
        dst = out / 'douyin_9x16.mp4'
        shutil.copyfile(src, dst)
        made.append(dst)
        cover = out / 'douyin_cover_9x16.png'
        rc, _, err = sh('ffmpeg -y -v error -ss %s -i %s -frames:v 1 -vf scale=1080:-2 %s'
                        % (shot, shq(str(src)), shq(str(cover))), 120)
        if cover.is_file():
            made.append(cover)
            checks.append({'name': '抖音 9:16 竖屏母版', 'pass': True})
            checks.append({'name': '封面帧已抽出', 'pass': True})
        else:
            checks.append({'name': '封面帧抽取', 'pass': False, 'detail': err[:160]})

    # ---- 小红书 3:4：取回画面区，在 1080x1440 画布上重排字幕（不是缩放裁切）----
    if platform in ('xhs', 'both'):
        dst = out / 'xhs_3x4.mp4'
        cues = srt_cues(vdir / 'subtitles.srt')
        if not cues:
            checks.append({'name': '小红书 3:4 重排', 'pass': False,
                           'detail': '缺少 subtitles.srt，无法按 3:4 重排字幕'})
        else:
            rc, err, ass = build_xhs_3x4(src, cues, dst, out)
            if dst.is_file():
                made.append(dst)
                p2 = probe_video(str(dst)) or {}
                ok34 = p2.get('ratio') == '3:4'
                checks.append({'name': '小红书 1080×1440（3:4 重排）', 'pass': ok34,
                               'detail': '实测 %sx%s' % (p2.get('w'), p2.get('h'))})
                rep = xhs_caption_report(dst, cues)
                checks.append({'name': '字幕落在小红书安全区', 'pass': rep.get('ok', False),
                               'detail': '实测字幕底边距底 %.1f%%（门槛 %.0f%%）、左右留白 %spx/%spx、校验句数 %s%s'
                                         % (rep.get('bottom_pct', 0), XHS_SAFE_BOTTOM_PCT,
                                            rep.get('left_px'), rep.get('right_gap_px'), rep.get('cues'),
                                            '' if rep.get('ok') else '；未达标，不要把这条直接发布')})
                if not rep.get('ok'):
                    notes.append('小红书 3:4 母版已重排，但实测字幕安全区未达标（%s），请调整画布或字幕位置后重新生成。'
                                 % json.dumps(rep, ensure_ascii=False))
                cover = out / 'xhs_cover_3x4.png'
                sh('ffmpeg -y -v error -ss %s -i %s -frames:v 1 -vf scale=1080:1440 %s'
                   % (shot, shq(str(dst)), shq(str(cover))), 120)
                if cover.is_file():
                    made.append(cover)
                made.append(ass)
            else:
                checks.append({'name': '小红书 3:4 重排', 'pass': False, 'detail': (err or '')[:200]})

    # ---- 文案：只取真实解说文本 ----
    body_txt = '\n'.join(sentences) if sentences else ''
    cap = ['# 标题（建议 %d 字以内）' % 22, title or '（srt 缺失，需人工补写）', '',
           '# 正文（来自成片解说，未改写）', body_txt or '（srt 缺失）', '',
           '# 话题标签', '（待人工补充：只写与内容相符的标签，不要堆无关热词）', '']
    capf = out / 'caption.md'
    capf.write_text('\n'.join(cap), encoding='utf-8')
    made.append(capf)

    # ---- 发布清单 ----
    steps = ['## 发布前必须人工完成', '',
             '1. 登录平台账号。控制台没有接入抖音/小红书的登录与发布接口，**必须人工在手机或浏览器完成扫码登录**，本工具不会也无法代登录。',
             '2. 核对视频与封面：确认是本轮需要的版本，封面不是黑帧或无关画面。',
             '3. 补充话题标签：caption.md 里的标签需人工按内容填写，不要堆热词。',
             '4. 人工发布：上传视频 → 选封面 → 粘贴文案 → 发布。本工具不上传、不发布。', '',
             '## 已知限制', '']
    steps += ['- ' + n for n in notes] if notes else ['- 无额外限制记录。']
    steps += ['', '## 平台适配检查', '']
    steps += ['- %s：%s%s' % (c['name'], '通过' if c['pass'] else '未通过',
                              ('（' + c['detail'] + '）') if c.get('detail') else '') for c in checks]
    (out / '发布清单.md').write_text('\n'.join(steps), encoding='utf-8')
    made.append(out / '发布清单.md')

    manifest = {
        'run': run, 'version': version, 'platform': platform,
        'source': str(src.relative_to(WF_ROOT)),
        'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'master': pr, 'checks': checks, 'notes': notes,
        'uploads': 'none — 本工具未上传、未登录任何平台',
        'files': hashes([f for f in made if f.is_file()]),
    }
    mf = out / 'manifest.json'
    mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'ok': True, 'dir': str(out), 'files': [f.name for f in made] + ['manifest.json'],
            'checks': checks, 'notes': notes}


class Handler(BaseHTTPRequestHandler):
    server_version = 'NASConsole/1.0'

    def log_message(self, fmt, *args):
        sys.stderr.write('[console] %s - %s\n' % (self.address_string(), fmt % args))

    def _send(self, code, data, ctype='application/json; charset=utf-8', raw=None):
        body = raw if raw is not None else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _body(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
            if n > 200000:
                return {}
            return json.loads(self.rfile.read(n).decode('utf-8')) if n else {}
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        path = unquote(u.path)
        if path in ('/', '/index.html'):
            f = BASE / 'index.html'
            if f.is_file():
                self._send(200, None, 'text/html; charset=utf-8', f.read_bytes())
            else:
                self._send(404, {'error': 'index.html missing'})
            return
        if path.startswith('/static/'):
            f = BASE / path[len('/static/'):]
            if f.is_file() and f.resolve().is_relative_to(BASE.resolve()):
                ctype = 'text/css; charset=utf-8' if f.suffix == '.css' else 'application/javascript; charset=utf-8'
                self._send(200, None, ctype, f.read_bytes())
            else:
                self._send(404, {'error': 'not found'})
            return
        if path.startswith('/media/'):
            self.serve_media(path[len('/media/'):])
            return
        if not path.startswith('/api/'):
            self._send(404, {'error': 'not found'})
            return
        api = path[5:]
        try:
            if api == 'overview':
                self._send(200, api_overview())
            elif api == 'config':
                self._send(200, api_config_get())
            elif api == 'stage-models':
                self._send(200, api_stage_models())
            elif api == 'voice':
                self._send(200, api_voice_get())
            elif api == 'models':
                m = model_status()
                ids, err = gateway_models(m['baseUrl'])
                self._send(200 if ids is not None else 502,
                           {'models': ids, 'error': err, 'current': m['model']})
            elif api == 'model/test':
                m = model_status()
                self._send(200, model_test(m['baseUrl'], m['model']))
            elif api == 'pipeline/status':
                self._send(200, api_pipeline_status())
            elif api == 'runs':
                self._send(200, api_runs())
            elif api == 'analysis':
                self._send(200, api_analysis())
            elif api == 'publish':
                self._send(200, api_publish_get())
            else:
                self._send(404, {'error': 'unknown api'})
        except Exception as e:
            self._send(500, {'error': str(e)[:300]})

    def serve_media(self, rel):
        f = (WF_ROOT / rel)
        try:
            f = f.resolve()
            if not f.is_relative_to(WF_ROOT.resolve()) or not f.is_file():
                self._send(404, {'error': 'not found'})
                return
        except Exception:
            self._send(404, {'error': 'not found'})
            return
        size = f.stat().st_size
        ctype = 'video/mp4' if f.suffix == '.mp4' else 'image/png' if f.suffix == '.png' else 'application/octet-stream'
        rng = self.headers.get('Range')
        if rng:
            m = re.match(r'bytes=(\d*)-(\d*)', rng)
            if m:
                start = int(m.group(1) or 0)
                end = int(m.group(2) or size - 1)
                end = min(end, size - 1)
                ln = end - start + 1
                self.send_response(206)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Content-Length', str(ln))
                self.end_headers()
                with open(f, 'rb') as fh:
                    fh.seek(start)
                    self.wfile.write(fh.read(ln))
                return
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(size))
        self.end_headers()
        with open(f, 'rb') as fh:
            while True:
                b = fh.read(65536)
                if not b:
                    break
                try:
                    self.wfile.write(b)
                except Exception:
                    break

    def do_POST(self):
        u = urlparse(self.path)
        api = unquote(u.path)[5:]
        body = self._body()
        try:
            if api == 'config':
                self._send(200, api_config_save(body))
            elif api == 'stage-models/save':
                self._send(200, api_stage_models_save(body))
            elif api == 'voice/save':
                self._send(200, api_voice_save(body))
            elif api == 'model/save':
                self._send(200, api_model_save(body))
            elif api == 'pipeline/start':
                self._send(200, api_pipeline_start())
            elif api == 'publish/queue':
                self._send(200, api_publish_queue_add(body))
            elif api == 'publish/send':
                self._send(200, api_publish_send(body))
            elif api == 'publish/package':
                self._send(200, api_publish_package(body))
            else:
                self._send(404, {'error': 'unknown api'})
        except Exception as e:
            self._send(500, {'error': str(e)[:300]})


def main():
    BASE.mkdir(parents=True, exist_ok=True)
    (BASE / 'logs').mkdir(exist_ok=True)
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    srv.daemon_threads = True
    sys.stderr.write('[console] listening on 0.0.0.0:%d\n' % PORT)
    sys.stderr.flush()
    srv.serve_forever()


if __name__ == '__main__':
    main()
