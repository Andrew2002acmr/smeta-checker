"""Read account quota without starting a model turn."""
from __future__ import annotations

import json
import math
import subprocess
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from queue import Empty, Queue
from threading import Thread
from time import monotonic


def _number(value):
    if type(value) in (int, float) and math.isfinite(value):
        return value
    return None


def _amount(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except InvalidOperation:
        return None


def read_quota(cli, env, cwd):
    requests = [
        {'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'smeta-worker', 'version': '1'}}},
        {'method': 'initialized'},
        {'id': 2, 'method': 'account/rateLimits/read'},
    ]
    snapshot = {'observed_at': datetime.now(UTC).isoformat(), 'status': 'unavailable', 'buckets': {}}
    try:
        with subprocess.Popen([cli, 'app-server', '--listen', 'stdio://'], cwd=cwd, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', errors='replace') as process:
            try:
                # Keep stdin open until the account request finishes. EOF can shut down app-server.
                lines = Queue()
                def collect():
                    for line in process.stdout:
                        lines.put(line)
                    lines.put('null')

                Thread(target=collect, daemon=True).start()
                deadline = monotonic() + 15
                def response(request_id):
                    while monotonic() < deadline:
                        event = json.loads(lines.get(timeout=max(0, deadline - monotonic())))
                        if event is None:
                            return None
                        if event.get('id') == request_id:
                            return event.get('result')
                    return None

                process.stdin.write(json.dumps(requests[0]) + '\n')
                process.stdin.flush()
                if response(1) is None:
                    return snapshot
                for request in requests[1:]:
                    process.stdin.write(json.dumps(request) + '\n')
                process.stdin.flush()
                payload = response(2)
            except (Empty, ValueError, OSError):
                return snapshot
            finally:
                if process.poll() is None:
                    process.kill()
    except OSError:
        return snapshot
    if not isinstance(payload, dict):
        return snapshot
    buckets = payload.get('rateLimitsByLimitId') or {}
    if not buckets and isinstance(payload.get('rateLimits'), dict):
        default = payload['rateLimits']
        buckets = {default.get('limitId') or 'codex': default}
    for name, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        windows = {}
        for key in ('primary', 'secondary'):
            if isinstance(window := bucket.get(key), dict):
                windows[key] = {field: _number(window.get(field))
                    for field in ('usedPercent', 'windowDurationMins', 'resetsAt')}
        credits = bucket.get('credits')
        if not isinstance(credits, dict):
            credits = {}
        balance = _amount(credits.get('balance'))
        snapshot['buckets'][name] = {'windows': windows,
            'credit_balance': str(balance) if balance is not None and credits.get('unlimited') is not True else None}
    snapshot['observed_at'] = datetime.now(UTC).isoformat()
    snapshot['status'] = 'available' if snapshot['buckets'] else 'unavailable'
    return snapshot


def weekly_reference_usd(environment):
    monthly = _amount(environment.get('CODEX_AUDIT_MONTHLY_QUOTA_REFERENCE_USD'))
    if monthly is not None:
        return monthly / 4
    return _amount(environment.get('CODEX_AUDIT_WEEKLY_QUOTA_REFERENCE_USD'))


def quota_metrics(before, after, reference_usd=None):
    reference = _amount(reference_usd)
    deltas = {}
    for name in before['buckets'].keys() & after['buckets'].keys():
        first, last = before['buckets'][name], after['buckets'][name]
        windows = {}
        for key in first['windows'].keys() & last['windows'].keys():
            start, end = first['windows'][key], last['windows'][key]
            a, b = start['usedPercent'], end['usedPercent']
            same_window = (start['resetsAt'] is not None and start['resetsAt'] == end['resetsAt']
                and start['windowDurationMins'] == end['windowDurationMins'])
            delta = round(b-a, 6) if same_window and a is not None and b is not None and 0 <= a <= b <= 100 and a < 100 else None
            windows[key] = {'account_delta_percentage_points': delta,
                'estimated_usd': str((Decimal(str(delta)) * reference / 100).quantize(Decimal('.01')))
                    if delta is not None and reference is not None and name == 'codex'
                    and start['windowDurationMins'] == 10080 else None,
                'measurement': 'unavailable' if delta is None else 'lower_bound' if b == 100 else 'observed_delta'}
        a, b = _amount(first['credit_balance']), _amount(last['credit_balance'])
        deltas[name] = {'windows': windows, 'account_credit_balance_decrease': str(a-b)
            if a is not None and b is not None and a >= b else None}
    return {'before': before, 'after': after, 'by_bucket': deltas,
        'scope': 'account_shared_with_other_activity', 'reference_usd_per_full_weekly_codex_window':
            str(reference) if reference is not None else None}
