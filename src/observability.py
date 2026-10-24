"""Local-first JSONL logging with optional bounded OpenObserve delivery."""
import base64
import fcntl
import json
import math
import os
from pathlib import Path
import queue
import re
import socket
import ssl
import stat
import threading
from datetime import datetime, timezone
from urllib.error import URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener
import uuid

from lib.common import secure_directory


_MAX_BYTES = 5 * 1024 * 1024
_BACKUPS = 5
_QUEUE_SIZE = 1000
_SENSITIVE_KEY = re.compile(r"(password|passwd|token|secret|authorization|credential)", re.I)
_INLINE_SECRET = re.compile(r"(?i)(authorization\s*[:=]\s*(?:basic|bearer)\s+|(?:password|passwd|token|secret)\s*[:=]\s*)[^\s,;]+")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_url(request, timeout):
    opener = build_opener(HTTPSHandler(context=ssl.create_default_context()), _NoRedirect())
    return opener.open(request, timeout=timeout)


def _boot_id():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip()
    except OSError:
        return 'unknown'


class Logger:
    def __init__(self, log_directory='/var/log/lxd-startup-manager',
                 secret_path='/etc/lxd-startup-manager/openobserve.json'):
        self.log_directory = secure_directory(log_directory)
        self.path = self.log_directory / 'events.jsonl'
        self.lock_path = self.log_directory / 'events.lock'
        self._lock = threading.Lock()
        self._closed = False
        self._config = self._load_config(Path(secret_path))
        self._secrets = []
        if self._config:
            for key in ('password', 'token', 'username'):
                value = self._config.get(key)
                if isinstance(value, str) and value:
                    self._secrets.append(value)
        self._queue = queue.Queue(maxsize=_QUEUE_SIZE)
        self._worker = None
        if not self._config and Path(secret_path).exists():
            self._write_local_diagnostic('warning', 'openobserve_config_invalid')
        if self._config:
            self._worker = threading.Thread(target=self._deliver, name='openobserve-log-delivery', daemon=True)
            self._worker.start()

    @staticmethod
    def _load_config(path):
        try:
            if any(parent.is_symlink() for parent in (path, *path.parents)):
                return None
            st = path.stat()
            if not (path.is_file() and st.st_uid in {0, os.geteuid()} and
                    st.st_mode & 0o777 == 0o600 and st.st_nlink == 1):
                return None
            cfg = json.loads(path.read_text(encoding='utf-8'))
            endpoint = cfg.get('endpoint')
            if not isinstance(endpoint, str):
                return None
            parsed = urlsplit(endpoint)
            if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment or any(ch in endpoint for ch in '\r\n'):
                return None
            if not isinstance(cfg.get('organization'), str) or not cfg['organization'] or not isinstance(cfg.get('stream'), str) or not cfg['stream']:
                return None
            if not (isinstance(cfg.get('token'), str) and cfg['token']) and not (isinstance(cfg.get('username'), str) and isinstance(cfg.get('password'), str)):
                return None
            timeout = float(cfg.get('timeout', 1.0))
            if not math.isfinite(timeout):
                return None
            cfg['timeout'] = min(3.0, max(0.2, timeout))
            cfg['batch_size'] = min(100, max(1, int(cfg.get('batch_size', 20))))
            cfg['_url'] = urlunsplit((parsed.scheme, parsed.netloc,
                                      parsed.path.rstrip('/') + '/api/' + quote(cfg['organization'], safe='') + '/' + quote(cfg['stream'], safe='') + '/_json', '', ''))
            return cfg
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def _redact(self, value):
        if isinstance(value, dict):
            return {str(k): ('[REDACTED]' if _SENSITIVE_KEY.search(str(k)) else self._redact(v)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._redact(v) for v in value]
        if isinstance(value, str):
            for secret in self._secrets:
                value = value.replace(secret, '[REDACTED]')
            return _INLINE_SECRET.sub(lambda m: m.group(1) + '[REDACTED]', value)[:2000]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return str(value)[:2000]

    def _rotate(self):
        self._validate_file(self.path, missing_ok=True)
        try:
            if self.path.stat().st_size < _MAX_BYTES:
                return
        except FileNotFoundError:
            return
        oldest = self.log_directory / f'{self.path.name}.{_BACKUPS}'
        self._validate_file(oldest, missing_ok=True)
        try:
            oldest.unlink()
        except FileNotFoundError:
            pass
        for i in range(_BACKUPS - 1, 0, -1):
            src = self.log_directory / f'{self.path.name}.{i}'
            dst = self.log_directory / f'{self.path.name}.{i + 1}'
            self._validate_file(src, missing_ok=True)
            self._validate_file(dst, missing_ok=True)
            if src.exists():
                os.replace(src, dst)
        os.replace(self.path, self.log_directory / f'{self.path.name}.1')

    @staticmethod
    def _validate_file(path, missing_ok=False):
        if path.is_symlink():
            raise OSError('unsafe_log_file')
        try:
            st = path.stat()
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o777 != 0o600 or st.st_nlink != 1:
            raise OSError('unsafe_log_file')

    def _append(self, line):
        self._validate_file(self.path, missing_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_nlink != 1 or st.st_mode & 0o777 != 0o600:
                raise OSError('unsafe_log_file')
            os.write(fd, (line + '\n').encode('utf-8'))
        finally:
            os.close(fd)

    def _with_file_lock(self):
        self._validate_file(self.lock_path, missing_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            st = os.fstat(fd)
            if st.st_uid != os.geteuid() or st.st_nlink != 1 or st.st_mode & 0o777 != 0o600:
                raise OSError('unsafe_log_lock')
            fcntl.flock(fd, fcntl.LOCK_EX)
            return fd
        except Exception:
            os.close(fd)
            raise

    def emit(self, **record):
        if self._closed:
            return
        instance = record.pop('instance', record.pop('instance_name', None))
        action = record.get('action')
        status = record.get('status')
        stage = record.pop('stage', None)
        if stage is None:
            if action == 'run':
                stage = 'start' if status == 'started' else 'finish'
            elif action == 'instance':
                stage = 'start' if status in {'started', 'started_after_timeout', 'started_after_error'} else (
                    'skip' if status in {'already_running', 'disabled'} else 'error')
            elif action in {'start', 'delay', 'skip', 'error', 'finish'}:
                stage = action
            else:
                stage = action
        event = {
            'timestamp': datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
            'hostname': socket.gethostname(),
            'boot_id': _boot_id(),
            'run_id': record.get('run_id'),
            'project': record.pop('project', None),
            'instance': instance,
            'stage': stage,
            'duration_ms': record.get('duration_ms', 0),
            'exit_code': record.get('exit_code'),
            **record,
        }
        event = self._redact(event)
        line = json.dumps(event, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        with self._lock:
            lock_fd = self._with_file_lock()
            try:
                self._rotate()
                self._append(line)
            finally:
                os.close(lock_fd)
        if self._config:
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                self._write_delivery_error('queue_full')

    def _write_local_diagnostic(self, status, reason):
        event = {'timestamp': datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
                 'hostname': socket.gethostname(), 'boot_id': _boot_id(), 'event': 'openobserve_delivery',
                 'stage': 'error' if status == 'failed' else 'warning', 'status': status, 'error': reason}
        line = json.dumps(event, separators=(',', ':'))
        with self._lock:
            lock_fd = self._with_file_lock()
            try:
                self._rotate()
                self._append(line)
            finally:
                os.close(lock_fd)

    def _write_delivery_error(self, reason):
        self._write_local_diagnostic('failed', reason)

    def _deliver(self):
        cfg = self._config
        while True:
            first = self._queue.get()
            if first is None:
                return
            batch = [first]
            while len(batch) < cfg['batch_size']:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    return
                batch.append(item)
            try:
                data = json.dumps(batch, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
                headers = {'Content-Type': 'application/json'}
                if cfg.get('token'):
                    headers['Authorization'] = 'Bearer ' + cfg['token']
                else:
                    raw = (cfg['username'] + ':' + cfg['password']).encode()
                    headers['Authorization'] = 'Basic ' + base64.b64encode(raw).decode('ascii')
                req = Request(cfg['_url'], data=data, headers=headers, method='POST')
                with _open_url(req, timeout=cfg['timeout']) as response:
                    if not 200 <= response.status < 300:
                        raise URLError('http_status')
                    ack = json.loads(response.read(65536).decode('utf-8'))
                if ack.get('code') != 200 or not isinstance(ack.get('status'), list):
                    raise ValueError('invalid_ack')
                result = next((item for item in ack['status'] if item.get('name') == cfg['stream']), None)
                if not isinstance(result, dict) or result.get('successful') != len(batch) or result.get('failed') != 0:
                    raise ValueError('incomplete_ack')
            except Exception:
                self._write_delivery_error('delivery_failed')

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._worker is not None:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
            self._worker.join(timeout=0.1)
