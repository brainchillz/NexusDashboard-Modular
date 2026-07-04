"""Extracted verbatim from NexusStationDashboard app.py (Stage 1 split).
Routes converted @app.route -> @bp.route; logic unchanged."""
import os
import re
import json
import time
import hmac
import socket
import hashlib
import secrets
import shutil
import threading
import subprocess
import sqlite3
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from flask import Blueprint, jsonify, request, session, g, Response
from werkzeug.security import generate_password_hash, check_password_hash
from .config import *
from .runcmd import run, run_safe, err, _size_to_bytes, _human_bytes, _num
from .validators import *
from .services import (SYSTEM_SERVICES, SERVICE_OVERRIDES, resolve_service,
                             _unit_present, RE_SERVICE, LLAMA_SERVICE, LLAMA_CONF,
                             LLAMA_MODELS_DIR, LLAMA_DEFAULT_BIN, LLAMA_URL)
from .registry import load_disabled_modules, MODULES, MODULE_IDS
from .auth import _is_admin, _hash_token, RE_USERNAME
from .summary import _system_resources
from ..modules.zfs import _parse_arcstats, _arc_summary
from ..modules.gpu import _gpu_snapshot

bp = Blueprint('history', __name__)

# ─── Time-series history (bounded, on-disk) ───────────────────────────
# A tiny SQLite ring buffer sampled by the storage-dashboard-history timer. Disk
# is HARD-bounded: raw 5-min points kept a short window, folded to one row/day
# for long trends; auto_vacuum reclaims space; a size backstop prunes if it ever
# exceeds a cap. Only allowlisted metrics with small labels (pool/disk/gpu/mount)
# are stored, so cardinality can't explode. See docs/plans/01-history-store.md.
HISTORY_DB = os.environ.get('DASHBOARD_HISTORY_DB', os.path.join(APP_DIR, 'history.db'))
HISTORY_TIMER = UNIT_PREFIX + '-history.timer'
HISTORY_RAW_DAYS = int(os.environ.get('DASHBOARD_HISTORY_RAW_DAYS', 3))
HISTORY_DAILY_DAYS = int(os.environ.get('DASHBOARD_HISTORY_DAILY_DAYS', 400))
HISTORY_MAX_MB = int(os.environ.get('DASHBOARD_HISTORY_MAX_MB', 64))
# Allowlisted metrics. gpu_*/llama_tokens_total are pre-listed so features 02/06c
# can write them without touching this set. Labels are bounded names.
HISTORY_METRICS = {
    'cpu_pct', 'mem_pct', 'load1', 'pool_alloc', 'pool_size',
    'arc_size', 'arc_hit_ratio', 'gpu_util', 'gpu_mem_pct', 'gpu_temp',
    'llama_tokens_total',
}
RE_HISTORY_LABEL = re.compile(r'^[A-Za-z0-9 ._:/-]{0,64}$')


def _history_conn():
    first = not os.path.exists(HISTORY_DB)
    conn = sqlite3.connect(HISTORY_DB, timeout=5, isolation_level=None)  # autocommit
    if first:
        conn.execute('PRAGMA auto_vacuum=FULL')   # must precede table creation
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute("CREATE TABLE IF NOT EXISTS samples("
                 "ts INTEGER NOT NULL, metric TEXT NOT NULL, "
                 "label TEXT NOT NULL DEFAULT '', value REAL)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_samples ON samples(metric,label,ts)")
    conn.execute("CREATE TABLE IF NOT EXISTS daily("
                 "day TEXT NOT NULL, metric TEXT NOT NULL, label TEXT NOT NULL DEFAULT '', "
                 "avg REAL, min REAL, max REAL, last REAL, PRIMARY KEY(day,metric,label))")
    conn.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    return conn


def _history_record(rows):
    """rows: iterable of (metric, label, value). One shared timestamp. Best-effort
    — never raise into a caller (history must not break a request or a tick)."""
    try:
        ts = int(time.time())
        clean = [(ts, m, (l or ''), float(v)) for (m, l, v) in rows
                 if m in HISTORY_METRICS and v is not None]
        if not clean:
            return
        conn = _history_conn()
        try:
            conn.executemany('INSERT INTO samples(ts,metric,label,value) VALUES(?,?,?,?)', clean)
        finally:
            conn.close()
    except Exception:
        pass


def _history_query(metric, label, since_ts):
    conn = _history_conn()
    try:
        cur = conn.execute('SELECT ts,value FROM samples WHERE metric=? AND label=? AND ts>=? '
                           'ORDER BY ts', (metric, label or '', since_ts))
        return [[r[0], r[1]] for r in cur.fetchall()]
    finally:
        conn.close()


def _history_query_daily(metric, label, days):
    conn = _history_conn()
    try:
        cur = conn.execute('SELECT day,avg,min,max,last FROM daily WHERE metric=? AND label=? '
                           'ORDER BY day DESC LIMIT ?', (metric, label or '', days))
        rows = [{'day': r[0], 'avg': r[1], 'min': r[2], 'max': r[3], 'last': r[4]}
                for r in cur.fetchall()]
        return rows[::-1]
    finally:
        conn.close()


def _history_prune_raw():
    conn = _history_conn()
    try:
        conn.execute('DELETE FROM samples WHERE ts < ?', (int(time.time()) - HISTORY_RAW_DAYS * 86400,))
    finally:
        conn.close()


def _history_maybe_rollup():
    """Once per day: fold whole prior days of raw into `daily`, prune old daily,
    VACUUM to release disk. Idempotent (upsert), gated by a meta marker."""
    today = datetime.now().strftime('%Y-%m-%d')
    conn = _history_conn()
    try:
        cur = conn.execute("SELECT v FROM meta WHERE k='last_rollup'")
        row = cur.fetchone()
        if row and row[0] == today:
            return
        conn.execute(
            "INSERT INTO daily(day,metric,label,avg,min,max,last) "
            "SELECT date(ts,'unixepoch','localtime') AS d, metric, label, "
            "  AVG(value), MIN(value), MAX(value), "
            "  (SELECT value FROM samples s2 WHERE s2.metric=samples.metric "
            "     AND s2.label=samples.label "
            "     AND date(s2.ts,'unixepoch','localtime')"
            "         =date(samples.ts,'unixepoch','localtime') "
            "   ORDER BY s2.ts DESC LIMIT 1) "
            "FROM samples WHERE date(ts,'unixepoch','localtime') < ? "
            "GROUP BY d, metric, label "
            "ON CONFLICT(day,metric,label) DO UPDATE SET "
            "  avg=excluded.avg, min=excluded.min, max=excluded.max, last=excluded.last",
            (today,))
        day_cut = (datetime.now() - timedelta(days=HISTORY_DAILY_DAYS)).strftime('%Y-%m-%d')
        conn.execute('DELETE FROM daily WHERE day < ?', (day_cut,))
        conn.execute('DELETE FROM samples WHERE ts < ?', (int(time.time()) - HISTORY_RAW_DAYS * 86400,))
        conn.execute("INSERT INTO meta(k,v) VALUES('last_rollup',?) "
                     "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (today,))
        conn.execute('VACUUM')
    finally:
        conn.close()


def _history_size_backstop():
    """Last-resort bound: if the db somehow exceeds the cap, aggressively drop the
    oldest raw and VACUUM. Returns MB after the check."""
    try:
        mb = os.path.getsize(HISTORY_DB) / (1024 * 1024)
        if mb > HISTORY_MAX_MB:
            conn = _history_conn()
            try:
                conn.execute('DELETE FROM samples WHERE ts < ?', (int(time.time()) - 86400,))
                conn.execute('VACUUM')
            finally:
                conn.close()
            print(f'history: size cap hit ({mb:.0f}MB > {HISTORY_MAX_MB}MB) — pruned', flush=True)
        return mb
    except OSError:
        return 0



def _gpu_history_samples():
    """Per-GPU util/mem/temp for the history sampler (feature 02). Empty when no
    GPU tooling is present, so history stays a no-op on GPU-less hosts."""
    rows = []
    for gp in _gpu_snapshot().get('gpus', []):
        idx = gp.get('index')
        lbl = 'gpu%d' % idx if idx is not None else 'gpu'
        if gp.get('util') is not None:
            rows.append(('gpu_util', lbl, gp['util']))
        if gp.get('mem_pct') is not None:
            rows.append(('gpu_mem_pct', lbl, gp['mem_pct']))
        if gp.get('temp') is not None:
            rows.append(('gpu_temp', lbl, gp['temp']))
    return rows


def _llama_tokens_total():
    """llama-server's cumulative tokens_predicted_total counter, or None if the
    server isn't up / the module is off. Cheap: one short HTTP GET, no sudo."""
    if 'llamacpp' in load_disabled_modules():
        return None
    import urllib.request
    try:
        with urllib.request.urlopen(LLAMA_URL.rstrip('/') + '/metrics', timeout=3) as r:
            text = r.read().decode()
    except Exception:
        return None
    for m in re.finditer(r'^(\w[\w:]*)\s+([0-9.eE+-]+)\s*$', text, re.M):
        name = m.group(1)
        if name.split(':', 1)[-1] == 'tokens_predicted_total':
            try:
                return float(m.group(2))
            except ValueError:
                return None
    return None


def _llama_history_samples():
    """Persist the cumulative predicted-token counter (feature 06c). The sparkline
    derives tokens/sec from the slope, so a raw counter is what we store. A restart
    resets the counter; that produces one downward step the UI can ignore."""
    tot = _llama_tokens_total()
    return [('llama_tokens_total', '', tot)] if tot is not None else []


def _history_sample():
    """Gather the current allowlisted metrics as (metric, label, value) tuples.
    Cheap sources only (/proc + `zpool list -Hp` + arcstats). 02/06c append more."""
    rows = []
    try:
        r = _system_resources()
        rows.append(('cpu_pct', '', r.get('cpu_pct')))
        rows.append(('mem_pct', '', (r.get('memory') or {}).get('pct')))
        rows.append(('load1', '', (r.get('load') or {}).get('1')))
    except Exception:
        pass
    try:
        out, _, _ = run(['zpool', 'list', '-Hp', '-o', 'name,size,alloc'])
        for line in out.strip().split('\n'):
            if '\t' not in line:
                continue
            parts = line.split('\t')
            name = parts[0]
            rows.append(('pool_size', name, _num(parts[1]) if len(parts) > 1 else None))
            rows.append(('pool_alloc', name, _num(parts[2]) if len(parts) > 2 else None))
    except Exception:
        pass
    try:
        with open('/proc/spl/kstat/zfs/arcstats') as f:
            s = _arc_summary(_parse_arcstats(f.read()))
        rows.append(('arc_size', '', s.get('size')))
        if s.get('hit_ratio') is not None:
            rows.append(('arc_hit_ratio', '', s.get('hit_ratio')))
    except Exception:
        pass
    try:
        rows.extend(_gpu_history_samples())   # feature 02 (no-op if absent)
    except Exception:
        pass
    try:
        rows.extend(_llama_history_samples())  # feature 06c (no-op if absent)
    except Exception:
        pass
    return rows


def _history_forecast_slope(points):
    """Least-squares slope (value units per second) over [[ts,value],...].
    Returns None if fewer than 3 points or the fit is degenerate."""
    pts = [(float(t), float(v)) for t, v in points if v is not None]
    n = len(pts)
    if n < 3:
        return None
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    denom = sum((p[0] - mx) ** 2 for p in pts)
    if denom == 0:
        return None
    return sum((p[0] - mx) * (p[1] - my) for p in pts) / denom


@bp.route('/api/history')
def history_get():
    metric = request.args.get('metric', '')
    label = request.args.get('label', '')
    if metric not in HISTORY_METRICS:
        return err('Unknown metric')
    if label and not RE_HISTORY_LABEL.match(label):
        return err('Invalid label')
    if request.args.get('res') == 'daily':
        days = max(1, min(_num(request.args.get('days')) or 90, HISTORY_DAILY_DAYS))
        return jsonify({'metric': metric, 'label': label, 'resolution': 'daily',
                        'points': _history_query_daily(metric, label, days)})
    max_since = HISTORY_RAW_DAYS * 86400
    since = min(_num(request.args.get('since')) or max_since, max_since)
    return jsonify({'metric': metric, 'label': label, 'resolution': 'raw',
                    'points': _history_query(metric, label, int(time.time()) - since)})


@bp.route('/api/history/forecast')
def history_forecast():
    """'full in ~N days' for a pool from its alloc trend (daily, else raw)."""
    label = request.args.get('label', '')
    if not label or not RE_POOL.match(label):
        return err('Invalid pool')
    daily = _history_query_daily('pool_alloc', label, 90)
    pts = [[datetime.strptime(d['day'], '%Y-%m-%d').timestamp(), d['last']]
           for d in daily if d['last'] is not None]
    if len(pts) < 3:
        pts = _history_query('pool_alloc', label, int(time.time()) - HISTORY_RAW_DAYS * 86400)
    slope = _history_forecast_slope(pts)   # bytes/sec
    out, _, _ = run(['zpool', 'list', '-Hp', '-o', 'size,alloc', label])
    parts = out.strip().split('\t')
    size = _num(parts[0]) if len(parts) > 0 else None
    cur = _num(parts[1]) if len(parts) > 1 else None
    rate_day = int(slope * 86400) if slope else 0
    days_to_full = None
    if slope and slope > 0 and size and cur is not None and size > cur:
        days_to_full = round((size - cur) / (slope * 86400), 1)
    return jsonify({'pool': label, 'fill_rate_bytes_per_day': rate_day,
                    'days_to_full': days_to_full})


def cli_history_tick():
    _history_record(_history_sample())
    _history_prune_raw()
    _history_maybe_rollup()
    _history_size_backstop()
    return 0


# ─── GPU monitoring (feature 02) ──────────────────────────────────────
