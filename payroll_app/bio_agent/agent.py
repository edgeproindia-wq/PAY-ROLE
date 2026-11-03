"""Biometric sync agent.

Runs on a computer in your office network (NOT on the website). It reads the punches from the eSSL / ZKTeco machine over the
office network and sends the new ones to the payroll site over HTTPS with a secret key. No port on the machine is opened to the
internet, and the machine's own address never leaves your office.

    python agent.py --config config.json            read the machine and send new punches (run this every 2 minutes)
    python agent.py --config config.json --test     check the machine and the website, send nothing

Needs: Python 3.8+ and   pip install pyzk
"""
import argparse
import datetime
import json
import logging
import logging.handlers
import os
import sys
import time
import urllib.error
import urllib.request

TIME_FORMAT = '%Y-%m-%dT%H:%M:%S'
REQUIRED = ('device_ip', 'server_url', 'api_key')
DEFAULTS = {'device_port': 4370, 'device_password': 0, 'first_run_days': 7, 'batch_size': 500, 'timeout_seconds': 15}
LOCK_STALE_SECONDS = 600
log = logging.getLogger('biometric-agent')


class AgentError(Exception):
    pass


# --------------------------------------------------------------------------- config and state
def load_config(path):
    try:
        with open(path, encoding='utf-8-sig') as fh:
            cfg = json.load(fh)
    except FileNotFoundError:
        raise AgentError('The config file was not found: %s' % path)
    except ValueError as exc:
        raise AgentError('The config file is not valid JSON: %s' % exc)
    for key in REQUIRED:
        if not str(cfg.get(key, '')).strip():
            raise AgentError('The config file needs "%s".' % key)
    for key, value in DEFAULTS.items():
        cfg.setdefault(key, value)
    cfg['server_url'] = str(cfg['server_url']).strip().rstrip('/')
    local = cfg['server_url'].startswith(('http://127.0.0.1', 'http://localhost'))
    if not (cfg['server_url'].startswith('https://') or local):
        raise AgentError('server_url must start with https:// (the key must not travel over plain http).')
    for key in ('device_port', 'first_run_days', 'batch_size', 'timeout_seconds'):
        try:
            cfg[key] = int(cfg[key])
        except (TypeError, ValueError):
            raise AgentError('"%s" must be a number.' % key)
    if not 1 <= cfg['batch_size'] <= 2000:
        raise AgentError('"batch_size" must be between 1 and 2000.')
    base = os.path.dirname(os.path.abspath(path))
    cfg.setdefault('state_file', os.path.join(base, 'state.json'))
    cfg.setdefault('log_file', os.path.join(base, 'agent.log'))
    cfg.setdefault('lock_file', os.path.join(base, 'agent.lock'))
    return cfg


def load_state(path):
    try:
        with open(path, encoding='utf-8') as fh:
            return datetime.datetime.strptime(json.load(fh)['last_sent'], TIME_FORMAT)
    except FileNotFoundError:
        return None
    except (ValueError, KeyError, TypeError, OSError):
        log.warning('The state file could not be read; starting again from the first-run window.')
        return None


def save_state(path, when):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump({'last_sent': when.strftime(TIME_FORMAT)}, fh)
    os.replace(tmp, path)


class Lock:
    """Stops two runs from overlapping. A lock older than ten minutes is treated as left over from a crash."""

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        try:
            if os.path.exists(self.path) and time.time() - os.path.getmtime(self.path) > LOCK_STALE_SECONDS:
                os.remove(self.path)
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
        except FileExistsError:
            raise AgentError('Another run is still in progress.')
        return self

    def __exit__(self, *exc):
        try:
            os.remove(self.path)
        except OSError:
            pass


# --------------------------------------------------------------------------- the machine
class ZkReader:
    """Reads punches from an eSSL / ZKTeco machine on port 4370 (pyzk)."""

    def __init__(self, cfg):
        self.cfg = cfg

    def read(self):
        try:
            from zk import ZK
        except ImportError:
            raise AgentError('pyzk is not installed. Run:  pip install pyzk')
        zk = ZK(self.cfg['device_ip'], port=self.cfg['device_port'], timeout=self.cfg['timeout_seconds'],
                password=self.cfg['device_password'], force_udp=False, ommit_ping=True)
        try:
            conn = zk.connect()
        except Exception as exc:
            raise AgentError('Could not connect to the machine at %s:%s (%s).' % (self.cfg['device_ip'], self.cfg['device_port'], type(exc).__name__))
        try:
            return [(str(r.user_id), r.timestamp) for r in conn.get_attendance()]
        except Exception as exc:
            raise AgentError('Could not read the punches from the machine (%s).' % type(exc).__name__)
        finally:
            try:
                conn.disconnect()
            except Exception:
                pass


# --------------------------------------------------------------------------- the website
def http_request(cfg, method, path, payload=None):
    """(status, parsed json or {}). The key is only ever sent in the Authorization header and never logged."""
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(cfg['server_url'] + path, data=data, method=method,
                                 headers={'Authorization': 'Bearer ' + cfg['api_key'], 'Content-Type': 'application/json', 'User-Agent': 'biometric-agent/1'})
    try:
        with urllib.request.urlopen(req, timeout=cfg['timeout_seconds']) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8') or '{}')
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode('utf-8') or '{}')
        except ValueError:
            body = {}
        return exc.code, body
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise AgentError('Could not reach the website (%s).' % type(exc).__name__)


# --------------------------------------------------------------------------- the work
def select_new(records, last_sent, now, first_run_days):
    """Punches at or after the last one sent (the website ignores repeats), oldest first. First run: only the last few days."""
    cutoff = last_sent if last_sent is not None else now - datetime.timedelta(days=first_run_days)
    return sorted(((ts, uid) for uid, ts in records if ts >= cutoff), key=lambda x: (x[0], x[1]))


def run_once(cfg, reader, post=http_request, now=None):
    """Read, send in batches, and move the state forward only after each batch was accepted."""
    now = now or datetime.datetime.now()
    result = {'read': 0, 'new': 0, 'sent': 0, 'status': 'ok'}
    records = reader.read()
    result['read'] = len(records)
    last = load_state(cfg['state_file'])
    new = select_new(records, last, now, cfg['first_run_days'])
    result['new'] = len(new)
    for i in range(0, len(new), cfg['batch_size']):
        batch = new[i:i + cfg['batch_size']]
        payload = {'punches': [{'id': uid, 'time': ts.strftime(TIME_FORMAT)} for ts, uid in batch]}
        status, body = post(cfg, 'POST', '/api/biometric/punches/', payload)
        if status == 401:
            raise AgentError('The website refused the key (401). Check api_key in the config, or add a new device on the Biometric page.')
        if status != 200 or not body.get('ok'):
            raise AgentError('The website did not accept the punches (HTTP %s: %s).' % (status, body.get('error', 'no details')))
        save_state(cfg['state_file'], batch[-1][0])
        result['sent'] += len(batch)
        log.info('Sent %d punch(es): %s', len(batch), {k: v for k, v in body.items() if k != 'ok'})
    return result


def check(cfg, reader, post=http_request):
    """--test: look at the machine and the website, send nothing. Returns lines to print."""
    lines = []
    records = reader.read()
    latest = max((ts for _u, ts in records), default=None)
    lines.append('Machine: OK, %d punch(es) stored%s' % (len(records), ', latest at %s' % latest.strftime(TIME_FORMAT) if latest else ''))
    status, body = post(cfg, 'GET', '/api/biometric/punches/')
    if status == 200 and body.get('ok'):
        lines.append('Website: OK, key accepted for device "%s"' % body.get('device', ''))
    elif status == 401:
        lines.append('Website: the key was refused (401)')
        raise AgentError('\n'.join(lines))
    else:
        lines.append('Website: unexpected answer (HTTP %s)' % status)
        raise AgentError('\n'.join(lines))
    return lines


def setup_logging(path):
    log.setLevel(logging.INFO)
    for old in list(log.handlers):                       # never log the same line twice
        log.removeHandler(old)
        old.close()
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    for handler in (logging.StreamHandler(sys.stdout), logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding='utf-8')):
        handler.setFormatter(fmt)
        log.addHandler(handler)


def main(argv=None, reader_factory=ZkReader, post=http_request):
    ap = argparse.ArgumentParser(description='Send biometric punches to the payroll site.')
    ap.add_argument('--config', default='config.json')
    ap.add_argument('--test', action='store_true', help='check the machine and the website, send nothing')
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config)
        setup_logging(cfg['log_file'])
        reader = reader_factory(cfg)
        if args.test:
            for line in check(cfg, reader, post):
                print(line)
            return 0
        with Lock(cfg['lock_file']):
            result = run_once(cfg, reader, post)
        log.info('Done: %s', result)
        return 0
    except AgentError as exc:
        if 'log' in globals() and log.handlers:
            log.error('%s', exc)
        else:
            print('ERROR: %s' % exc)
        return 1


if __name__ == '__main__':
    sys.exit(main())
