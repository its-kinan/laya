#!/usr/bin/env python3
"""Build the Kaggle CPU serve notebook: laya-serve shim + tunnel agent -> public URL."""
import json, os, secrets

def _env_secret(name):
    for line in open('/home/hatch/workspace/tunnel/.env.local'):
        if line.startswith(name + '='):
            return line.strip().split('=', 1)[1]
    return ''

TUNNEL_ID = os.environ.get('SERVE_TUNNEL_ID', 'kaggle-laya-cpu')
# (host, secret) pairs the kernel's agents will dial. Same tunnel id on all.
# Single source of truth: ~/workspace/tunnel/relays.json (host + secret_name).
def _load_relays():
    cfg = json.load(open('/home/hatch/workspace/tunnel/relays.json'))
    out = []
    for r in cfg.get('relays', []):
        sec = _env_secret(r.get('secret_name', 'SECRET'))
        if r.get('host') and sec:
            out.append((r['host'], sec))
        else:
            print('WARNING: relay entry skipped (missing host or secret):', r.get('host'))
    return out
RELAYS = _load_relays()

SERVE_SHIM = r'''
import asyncio, json, os, time
import laya
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

MODEL_DIR = open('/kaggle/working/model_dir.txt').read().strip()
print('loading model from', MODEL_DIR, flush=True)
t0 = time.time()
agent = laya.Agent(MODEL_DIR, device='cpu')
print(f'model loaded in {time.time()-t0:.1f}s', flush=True)

app = FastAPI()
gate = asyncio.Lock()

@app.get('/health')
async def health():
    return {'ok': True, 'model_dir': MODEL_DIR}

@app.post('/v1/systemone')
async def systemone(request: Request):
    body = await request.json()
    questions = body.get('questions')
    if not isinstance(body, dict) or not questions:
        return JSONResponse({'error': "body must be an object with 'questions'"}, status_code=400)
    state = body.get('state')
    async with gate:
        t0 = time.perf_counter()
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, lambda: agent.predict(state, questions))
        ms = (time.perf_counter() - t0) * 1000
    # Agent.predict returns {'answers': {...}, ...} already Jev-shaped; unwrap one level
    answers = result.get('answers', result) if isinstance(result, dict) else result
    return JSONResponse({'model': 'laya-hl-finetuned', 'answers': answers,
                         'usage': {'inference_ms': round(ms, 1)}},
                        headers={'X-Inference-Time-Ms': f'{ms:.1f}'})

@app.post('/admin/shutdown')
async def admin_shutdown(request: Request):
    """Remote shutdown: the keeper (or an operator holding the per-kernel
    token) tells this kernel to end its Kaggle session, freeing the slot.
    The notebook's keep-alive loop watches for the sentinel file."""
    auth = request.headers.get('authorization', '')
    try:
        token = open('/kaggle/working/shutdown_token.txt').read().strip()
    except Exception:
        token = ''
    if not token or auth != 'Bearer ' + token:
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    open('/kaggle/working/SHUTDOWN', 'w').write('shutdown requested')
    return {'ok': True}
'''

AGENT_PY = open('/home/hatch/workspace/tunnel/agent/agent_py.py').read()

def cell(src, md=False):
    return {'cell_type': 'markdown' if md else 'code',
            'metadata': {}, 'source': src,
            'outputs': [], 'execution_count': None} if not md else \
           {'cell_type': 'markdown', 'metadata': {}, 'source': src}

assert RELAYS, 'no relays configured'
print(f'relays: {[h for h, _ in RELAYS]}')

# Per-kernel shutdown token: lets the keeper (or an operator) tell a running
# serving kernel to stop itself via POST /t/<tunnel-id>/admin/shutdown,
# without the Kaggle API. The keeper holds these; the kernel only verifies.
TOKEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'shutdown_tokens.json')
SHUTDOWN_TOKEN = secrets.token_hex(16)
try:
    _tokens = json.load(open(TOKEN_PATH))
except Exception:
    _tokens = {}
_tokens[TUNNEL_ID] = SHUTDOWN_TOKEN
_tokens = dict(list(_tokens.items())[-5:])  # keep the last few generations
json.dump(_tokens, open(TOKEN_PATH, 'w'), indent=1)
print('shutdown token stored for', TUNNEL_ID)

nb = {'nbformat': 4, 'nbformat_minor': 4,
      'metadata': {'kernelspec': {'display_name': 'Python 3', 'language': 'python', 'name': 'python3'},
                   'language_info': {'name': 'python', 'version': '3.10'}},
      'cells': [
cell('# Laya HL fine-tuned — CPU serve via tunnel relays\nPublic endpoints served through Cloudflare tunnel-relay workers (one agent per relay, same tunnel id). Free CPU, 12h max per run.', md=True),

cell('''!pip install -q git+https://github.com/NandhaKishorM/laya websockets fastapi "uvicorn[standard]" 2>&1 | tail -1
import os
os.environ['OMP_NUM_THREADS'] = '4'
print('deps ok')'''),

cell('''# resolve the fine-tuned model dir from the training kernel's output
import os
model_dir = None
for root, dirs, files in os.walk('/kaggle/input'):
    if 'model.safetensors' in files and 'rl_agent_config.json' in files:
        # prefer the fine-tuned output (has laya_hl_small in path or newest)
        if 'laya_hl_small' in root or model_dir is None:
            model_dir = root
assert model_dir, 'fine-tuned model not found under /kaggle/input'
print('MODEL_DIR =', model_dir)
open('/kaggle/working/model_dir.txt', 'w').write(model_dir)
print('size: %.1f MB' % (os.path.getsize(os.path.join(model_dir, 'model.safetensors')) / 1e6))'''),

cell('''# write the serve shim + tunnel agent
open('/kaggle/working/serve_shim.py', 'w').write(%s)
open('/kaggle/working/agent_py.py', 'w').write(%s)
open('/kaggle/working/shutdown_token.txt', 'w').write(%s)
print('shim + agent written')''' % (json.dumps(SERVE_SHIM), json.dumps(AGENT_PY), json.dumps(SHUTDOWN_TOKEN))),

cell('''# start the serve shim on :8000
import subprocess, time, urllib.request
log = open('/kaggle/working/serve.log', 'a')
p = subprocess.Popen(['python3', '-m', 'uvicorn', 'serve_shim:app',
                      '--host', '127.0.0.1', '--port', '8000'],
                     cwd='/kaggle/working', stdout=log, stderr=subprocess.STDOUT)
for i in range(60):
    try:
        r = urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)
        print('serve up:', r.read().decode()[:120]); break
    except Exception as e:
        time.sleep(10)
else:
    raise RuntimeError('serve did not come up; see serve.log')
open('/kaggle/working/serve.pid', 'w').write(str(p.pid))'''),

cell('''# smoke test: one val case through the local API
import json, urllib.request, time, os
def J(v): return json.loads(v) if isinstance(v, str) else v
def resolve_data_dir():
    for c in ['/kaggle/input/datasets/kinan21/laya-hl-trial', '/kaggle/input/laya-hl-trial']:
        if os.path.exists(os.path.join(c, 'val.jsonl')): return c
    for root, dirs, files in os.walk('/kaggle/input'):
        if 'val.jsonl' in files: return root
row = json.loads(open(os.path.join(resolve_data_dir(), 'val.jsonl')).readline())
req = json.dumps({'state': row['state'], 'questions': J(row['questions'])}).encode()
t0 = time.time()
r = urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8000/v1/systemone', data=req,
                           headers={'Content-Type': 'application/json'}), timeout=300)
out = json.loads(r.read())
print('inference_ms:', out['usage']['inference_ms'])
print('bias:', {k: round(v, 3) for k, v in out['answers']['bias']['probabilities'].items()})
print('intent:', {k: round(v, 3) for k, v in out['answers']['intent']['probabilities'].items()})'''),

cell('''# start one tunnel agent per relay -> public URLs (same tunnel id everywhere)
import subprocess, time, os
_TUNNEL_ID = ''' + json.dumps(TUNNEL_ID) + '''
_RELAYS = ''' + json.dumps(RELAYS) + '''
procs = []
for i, (_host, _secret) in enumerate(_RELAYS):
    alog = open(f'/kaggle/working/agent{i}.log', 'a')
    env = dict(os.environ, TUNNEL_ID=_TUNNEL_ID, TUNNEL_SECRET=_secret,
               WORKER_HOST=_host, LOCAL_PORT='8000')
    p = subprocess.Popen(['python3', '/kaggle/working/agent_py.py', '--tunnel', _TUNNEL_ID,
                          '--local-port', '8000', '--timeout', '110'],
                         env=env, stdout=alog, stderr=subprocess.STDOUT)
    procs.append(p)
    print(f'agent {i} pid: {p.pid} -> {_host}')
time.sleep(8)
print('agent log tails:')
for i in range(len(_RELAYS)):
    print(f'--- agent{i} ---')
    print(open(f'/kaggle/working/agent{i}.log').read()[-400:])
print()
for _host, _ in _RELAYS:
    print('PUBLIC URL: ' + _host.rstrip('/') + '/t/' + _TUNNEL_ID + '/v1/systemone')'''),

cell('''# keep-alive: hold the session until the 12h cap OR until replaced.
# Kaggle slot discipline: exactly 1 serving slot in steady state. When the
# keeper rotates (new backend boots, alias flips), this kernel notices the
# public alias no longer points at it and stops itself, freeing the session
# slot. Rotation therefore holds 2 slots only transiently (boot+verify).
import time, json, os, urllib.request
print('serving. session ends at the 12h cap, on remote shutdown, or when the alias moves to a newer backend.')
t_end = time.time() + 11.5 * 3600
_ALIAS = 'kaggle-laya-cpu'
_seen_self = False  # only trust the exit check after the alias pointed here once
_mismatch = 0       # consecutive polls where a relay positively points elsewhere
while time.time() < t_end:
    if os.path.exists('/kaggle/working/SHUTDOWN'):
        print('remote shutdown requested - stopping to free the session slot.', flush=True)
        break
    time.sleep(60)
    try:
        _here = _other = 0
        for _host, _secret in _RELAYS:
            _host = _host.split('://', 1)[-1].rstrip('/')
            try:
                _req = urllib.request.Request(
                    'https://' + _host + '/admin/alias?name=' + _ALIAS,
                    headers={'Authorization': 'Bearer ' + _secret,
                             # Cloudflare edge 403s Python-urllib's default UA
                             'User-Agent': 'laya-keeper/1.0'})
                _resp = urllib.request.urlopen(_req, timeout=20)
                _data = json.loads(_resp.read().decode())
                if _resp.status == 200 and _data.get('backend') == _TUNNEL_ID:
                    _here += 1
                elif _resp.status == 200:
                    _other += 1
            except Exception as _e:
                print('alias check %s: %s' % (_host, _e), flush=True)
        if _here:
            _seen_self = True
            _mismatch = 0
        elif _seen_self and _other:
            _mismatch += 1
            print('alias points elsewhere (%d/3)' % _mismatch, flush=True)
            if _mismatch >= 3:
                print('replaced by a newer backend - stopping to free the session slot.', flush=True)
                break
    except Exception as _e:
        print('keep-alive loop: %s' % _e, flush=True)
print('stopping.')'''),
]}

os.makedirs(os.environ.get('SERVE_KERNEL_DIR', '/tmp/kaggle-serve'), exist_ok=True)
json.dump(nb, open(os.path.join(os.environ.get('SERVE_KERNEL_DIR', '/tmp/kaggle-serve'), 'notebook.ipynb'), 'w'))
json.dump({
    "id": "kinan21/laya-hl-serve-cpu-via-tunnel",
    "title": "Laya HL serve (CPU) via tunnel",
    "code_file": "notebook.ipynb",
    "language": "python",
    "kernel_type": "notebook",
    "is_private": True,
    "enable_gpu": False,
    "enable_tpu": False,
    "enable_internet": True,
    "dataset_sources": ["kinan21/laya-hl-trial"],
    "kernel_sources": ["kinan21/laya-hl-finetune-multilingual-v1"],
    "competition_sources": [],
    "model_sources": []
}, open(os.path.join(os.environ.get('SERVE_KERNEL_DIR', '/tmp/kaggle-serve'), 'kernel-metadata.json'), 'w'), indent=2)
print('serve notebook written')
