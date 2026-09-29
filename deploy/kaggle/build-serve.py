#!/usr/bin/env python3
"""Build the Kaggle CPU serve notebook (DUMB server): laya-serve shim + tunnel
agent -> public URL, plus a stats-push thread reporting to the laya dashboard
worker.

Rotation is done EXTERNALLY (the laya worker cron pushes new kernel versions);
the notebook never pushes anything itself.

Usage:
    python3 build-serve.py --model orig|ft --tunnel-id ID --out-dir DIR

Env overrides: SERVE_TUNNEL_ID, SERVE_KERNEL_DIR, LAYA_INGEST_SECRET.
"""
import argparse
import json
import os

WORKER_HOST = 'tunnel-relay.ediprnm-keen.workers.dev'
LAYA_WORKER = 'laya.ediprnm-keen.workers.dev'
SECRET = open('/home/hatch/workspace/tunnel/.env.local').read().split('SECRET=')[1].split('\n')[0].strip()
INGEST_SECRET = os.environ.get('LAYA_INGEST_SECRET', '')

SERVE_SHIM = r'''
import asyncio, json, os, time
import laya
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

BACKEND_ID = __BACKEND_ID__
MODEL_NAME = __MODEL_NAME__

MODEL_DIR = open('/kaggle/working/model_dir.txt').read().strip()
print('loading model from', MODEL_DIR, flush=True)
t0 = time.time()
agent = laya.Agent(MODEL_DIR, device='cpu')
print(f'model loaded in {time.time()-t0:.1f}s', flush=True)

app = FastAPI()
gate = asyncio.Lock()
STATS = {"requests": 0, "errors": 0, "total_ms": 0.0}
STARTED = time.time()

@app.get('/health')
async def health():
    return {'ok': True, 'model_dir': MODEL_DIR}

@app.get('/stats')
async def stats():
    reqs = STATS["requests"]
    return {"backend_id": BACKEND_ID, "model": MODEL_NAME,
            "uptime_s": round(time.time() - STARTED, 1),
            "requests": reqs, "errors": STATS["errors"],
            "avg_ms": round(STATS["total_ms"] / max(reqs, 1), 1)}

@app.post('/v1/systemone')
async def systemone(request: Request):
    body = await request.json()
    questions = body.get('questions')
    if not isinstance(body, dict) or not questions:
        return JSONResponse({'error': "body must be an object with 'questions'"}, status_code=400)
    state = body.get('state')
    try:
        async with gate:
            t0 = time.perf_counter()
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, lambda: agent.predict(state, questions))
            ms = (time.perf_counter() - t0) * 1000
    except Exception as e:
        STATS["errors"] += 1
        return JSONResponse({'error': f'inference failed: {type(e).__name__}'}, status_code=500)
    STATS["requests"] += 1
    STATS["total_ms"] += ms
    # Agent.predict returns {'answers': {...}, ...} already Jev-shaped; unwrap one level
    answers = result.get('answers', result) if isinstance(result, dict) else result
    return JSONResponse({'model': MODEL_NAME, 'answers': answers,
                         'usage': {'inference_ms': round(ms, 1)}},
                        headers={'X-Inference-Time-Ms': f'{ms:.1f}'})
'''

AGENT_PY = open('/home/hatch/workspace/tunnel/agent/agent_py.py').read()


def cell(src, md=False):
    if md:
        return {'cell_type': 'markdown', 'metadata': {}, 'source': src}
    return {'cell_type': 'code', 'metadata': {}, 'source': src,
            'outputs': [], 'execution_count': None}


def normalize(nb):
    """Clear code outputs; join list sources to strings (mirrors kaggle CLI push)."""
    for c in nb['cells']:
        if c.get('cell_type') == 'code':
            c['outputs'] = []
            src = c.get('source')
            if isinstance(src, list):
                c['source'] = ''.join(src)
    return nb


MODEL_CFG = {
    'orig': {
        'slug': 'kinan21/laya-serve-orig',
        'title': 'Laya serve orig',
        'model_name': 'laya-orig',
        'alias': 'laya-orig',
        'dataset_sources': [],
        'kernel_sources': [],
        'header': ('# Laya (stock checkpoint) — CPU serve via tunnel relay\n'
                   'Public endpoint served through the Cloudflare tunnel-relay worker. '
                   'Free CPU, 12h max per run. Rotation is external; this notebook is a dumb server.'),
        'model_cell': (
            '# download the stock Laya checkpoint\n'
            'from huggingface_hub import snapshot_download\n'
            'model_dir = snapshot_download("convaiinnovations/laya")\n'
            'open(\'/kaggle/working/model_dir.txt\', \'w\').write(model_dir)\n'
            'print(\'MODEL_DIR =\', model_dir)'
        ),
        'smoke_cell': (
            '# sanity: checkpoint present and non-trivial\n'
            'import os\n'
            'model_dir = open(\'/kaggle/working/model_dir.txt\').read().strip()\n'
            'print(\'MODEL_DIR =\', model_dir)\n'
            'tot = 0\n'
            'for root, dirs, files in os.walk(model_dir):\n'
            '    for f in files:\n'
            '        tot += os.path.getsize(os.path.join(root, f))\n'
            'print(\'checkpoint size: %.1f MB\' % (tot / 1e6))\n'
            'assert tot > 100 * 1e6, \'checkpoint suspiciously small\''
        ),
    },
    'ft': {
        'slug': 'kinan21/laya-serve-finetuned',
        'title': 'Laya serve finetuned',
        'model_name': 'laya-finetuned',
        'alias': 'laya-finetuned',
        'dataset_sources': ['kinan21/laya-hl-trial'],
        'kernel_sources': ['kinan21/laya-hl-small-trial-baseline-vs-finetuned'],
        'header': ('# Laya HL fine-tuned — CPU serve via tunnel relay\n'
                   'Public endpoint served through the Cloudflare tunnel-relay worker. '
                   'Free CPU, 12h max per run. Rotation is external; this notebook is a dumb server.'),
        'model_cell': (
            '# resolve the fine-tuned model dir from the training kernel\'s output\n'
            'import os\n'
            'model_dir = None\n'
            'for root, dirs, files in os.walk(\'/kaggle/input\'):\n'
            '    if \'model.safetensors\' in files and \'rl_agent_config.json\' in files:\n'
            '        # prefer the fine-tuned output (has laya_hl_small in path or newest)\n'
            '        if \'laya_hl_small\' in root or model_dir is None:\n'
            '            model_dir = root\n'
            'assert model_dir, \'fine-tuned model not found under /kaggle/input\'\n'
            'print(\'MODEL_DIR =\', model_dir)\n'
            'open(\'/kaggle/working/model_dir.txt\', \'w\').write(model_dir)\n'
            'print(\'size: %.1f MB\' % (os.path.getsize(os.path.join(model_dir, \'model.safetensors\')) / 1e6))'
        ),
        'smoke_cell': (
            '# smoke test: one val case through the local API\n'
            'import json, urllib.request, time, os\n'
            'def J(v): return json.loads(v) if isinstance(v, str) else v\n'
            'def resolve_data_dir():\n'
            '    for c in [\'/kaggle/input/datasets/kinan21/laya-hl-trial\', \'/kaggle/input/laya-hl-trial\']:\n'
            '        if os.path.exists(os.path.join(c, \'val.jsonl\')): return c\n'
            '    for root, dirs, files in os.walk(\'/kaggle/input\'):\n'
            '        if \'val.jsonl\' in files: return root\n'
            'row = json.loads(open(os.path.join(resolve_data_dir(), \'val.jsonl\')).readline())\n'
            'req = json.dumps({\'state\': row[\'state\'], \'questions\': J(row[\'questions\'])}).encode()\n'
            't0 = time.time()\n'
            'r = urllib.request.urlopen(urllib.request.Request(\'http://127.0.0.1:8000/v1/systemone\', data=req,\n'
            '                           headers={\'Content-Type\': \'application/json\'}), timeout=300)\n'
            'out = json.loads(r.read())\n'
            'print(\'inference_ms:\', out[\'usage\'][\'inference_ms\'])\n'
            'print(\'bias:\', {k: round(v, 3) for k, v in out[\'answers\'][\'bias\'][\'probabilities\'].items()})\n'
            'print(\'intent:\', {k: round(v, 3) for k, v in out[\'answers\'][\'intent\'][\'probabilities\'].items()})'
        ),
    },
}


def build(model, tunnel_id):
    cfg = MODEL_CFG[model]
    shim = (SERVE_SHIM
            .replace('__BACKEND_ID__', json.dumps(tunnel_id))
            .replace('__MODEL_NAME__', json.dumps(cfg['model_name'])))

    nb = {'nbformat': 4, 'nbformat_minor': 4,
          'metadata': {'kernelspec': {'display_name': 'Python 3', 'language': 'python', 'name': 'python3'},
                       'language_info': {'name': 'python', 'version': '3.10'}},
          'cells': [
        cell(cfg['header'], md=True),

        cell('''!pip install -q git+https://github.com/NandhaKishorM/laya websockets fastapi "uvicorn[standard]" 2>&1 | tail -1
import os
os.environ['OMP_NUM_THREADS'] = '4'
print('deps ok')'''),

        cell(cfg['model_cell']),

        cell('''# write the serve shim + tunnel agent
open('/kaggle/working/serve_shim.py', 'w').write(%s)
open('/kaggle/working/agent_py.py', 'w').write(%s)
print('shim + agent written')''' % (json.dumps(shim), json.dumps(AGENT_PY))),

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

        cell(cfg['smoke_cell']),

        cell('''# start the tunnel agent -> public URL
import subprocess, time
_TUNNEL_ID = ''' + json.dumps(tunnel_id) + '''
_SECRET = ''' + json.dumps(SECRET) + '''
_WORKER_HOST = ''' + json.dumps(WORKER_HOST) + '''
alog = open('/kaggle/working/agent.log', 'a')
env = dict(__import__('os').environ, TUNNEL_ID=_TUNNEL_ID, TUNNEL_SECRET=_SECRET,
           WORKER_HOST=_WORKER_HOST, LOCAL_PORT='8000')
p = subprocess.Popen(['python3', '/kaggle/working/agent_py.py', '--tunnel', _TUNNEL_ID,
                      '--local-port', '8000', '--timeout', '110'],
                     env=env, stdout=alog, stderr=subprocess.STDOUT)
time.sleep(8)
print('agent pid:', p.pid)
print('agent log tail:')
print(open('/kaggle/working/agent.log').read()[-600:])
print()
print('PUBLIC URL: https://' + _WORKER_HOST + '/t/' + _TUNNEL_ID + '/v1/systemone')'''),

        cell('''# stats push -> laya dashboard worker (daemon thread; never crashes the notebook)
import threading, time as _time, json as _json, urllib.request as _urlreq
_BACKEND_ID = ''' + json.dumps(tunnel_id) + '''
_ALIAS = ''' + json.dumps(cfg['alias']) + '''
_MODEL = ''' + json.dumps(cfg['model_name']) + '''
_INGEST_SECRET = ''' + json.dumps(INGEST_SECRET) + '''
_INGEST_URL = ''' + json.dumps('https://' + LAYA_WORKER + '/ingest') + '''
def _post_ingest(event):
    try:
        s = _json.loads(_urlreq.urlopen('http://127.0.0.1:8000/stats', timeout=10).read().decode())
    except Exception as e:
        print('ingest: local /stats failed:', type(e).__name__, flush=True)
        return
    body = _json.dumps({'backend_id': _BACKEND_ID, 'alias': _ALIAS, 'model': _MODEL,
                        'ts': int(_time.time()), 'event': event, 'stats': s}).encode()
    try:
        req = _urlreq.Request(_INGEST_URL, data=body,
                              headers={'Content-Type': 'application/json',
                                       'Authorization': 'Bearer ' + _INGEST_SECRET,
                                       'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) laya-serve/1.0'})
        r = _urlreq.urlopen(req, timeout=30)
        print('ingest: %s -> %s' % (event, r.status), flush=True)
    except Exception as e:
        print('ingest: post failed:', type(e).__name__, flush=True)
def _ingest_loop():
    _post_ingest('boot')
    while True:
        _time.sleep(300)
        _post_ingest('stats')
_t = threading.Thread(target=_ingest_loop, daemon=True)
_t.start()
print('stats push thread started')'''),

        cell('''# keep-alive: hold the session up to the 12h cap
import time
print('serving. session ends at the 12h cap or on manual stop.')
t_end = time.time() + 11.5 * 3600
while time.time() < t_end:
    time.sleep(600)
print('12h nearly up - stopping.')'''),
    ]}
    return normalize(nb), cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--model', choices=('orig', 'ft'), required=True)
    ap.add_argument('--tunnel-id', default=None)
    ap.add_argument('--out-dir', default=None)
    a = ap.parse_args()

    tunnel_id = a.tunnel_id or os.environ.get('SERVE_TUNNEL_ID') or f"laya-{a.model}-dev"
    out_dir = a.out_dir or os.environ.get('SERVE_KERNEL_DIR') or f'/tmp/kaggle-serve-{a.model}'
    os.makedirs(out_dir, exist_ok=True)

    nb, cfg = build(a.model, tunnel_id)
    nb_text = json.dumps(nb)

    with open(os.path.join(out_dir, 'notebook.ipynb'), 'w') as f:
        f.write(nb_text)

    with open(os.path.join(out_dir, 'kernel-metadata.json'), 'w') as f:
        json.dump({
            "id": cfg['slug'],
            "title": cfg['title'],
            "code_file": "notebook.ipynb",
            "language": "python",
            "kernel_type": "notebook",
            "is_private": True,
            "enable_gpu": False,
            "enable_tpu": False,
            "enable_internet": True,
            "dataset_sources": cfg['dataset_sources'],
            "kernel_sources": cfg['kernel_sources'],
            "competition_sources": [],
            "model_sources": []
        }, f, indent=2)

    template_text = nb_text.replace(tunnel_id, '__BACKEND_ID__')
    with open(os.path.join(out_dir, f'template-{a.model}.json'), 'w') as f:
        json.dump({
            "slug": cfg['slug'],
            "title": cfg['title'],
            "text": template_text,
            "dataset_sources": cfg['dataset_sources'],
            "kernel_sources": cfg['kernel_sources'],
        }, f)

    print(f'model={a.model} tunnel_id={tunnel_id}')
    print(f'cells={len(nb["cells"])} out_dir={out_dir}')
    print('notebook.ipynb, kernel-metadata.json, template-%s.json written' % a.model)


if __name__ == '__main__':
    main()
