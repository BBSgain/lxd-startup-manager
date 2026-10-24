"""Transactional queue and execution history."""
import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3

from lib.common import AppError, secure_directory, secure_file, validate_queue


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class Store:
    VERSION = 1

    def __init__(self, path='/var/lib/lxd-startup-manager/state.db'):
        self.path = Path(path)
        secure_directory(self.path.parent)
        secure_file(self.path)
        for suffix in ('-journal', '-wal', '-shm'):
            secure_file(str(self.path) + suffix)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        info = self.path.stat()
        self._identity = (info.st_dev, info.st_ino)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA synchronous=FULL')
        try:
            self._migrate()
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def ensure_current(self):
        secure_file(self.path)
        try:
            info = self.path.stat()
        except FileNotFoundError:
            raise AppError("Baza została usunięta. Zamknij aplikację i otwórz ją po instalacji.") from None
        if (info.st_dev, info.st_ino) != self._identity:
            raise AppError("Baza została zastąpiona. Otwórz aplikację ponownie.")

    @contextlib.contextmanager
    def transaction(self):
        self.ensure_current()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def _migrate(self):
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version > self.VERSION:
            raise AppError("Baza pochodzi z nowszej wersji aplikacji.")
        if version == self.VERSION:
            return
        # Back up any existing SQLite content before a forward migration.
        if self.path.stat().st_size:
            backup = secure_file(str(self.path) + f'.v{version}.bak')
            fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            dest = sqlite3.connect(backup)
            try:
                self.db.backup(dest)
            finally:
                dest.close()
        statements = [
            'CREATE TABLE schema_version(version INTEGER NOT NULL)',
            'INSERT INTO schema_version VALUES(1)',
            '''CREATE TABLE queue_entries(id INTEGER PRIMARY KEY, position INTEGER UNIQUE NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('instance','delay')), project TEXT, instance_name TEXT,
            enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), delay_seconds INTEGER NOT NULL DEFAULT 0,
            UNIQUE(project, instance_name))''',
            'CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)',
            '''CREATE TABLE runs(id INTEGER PRIMARY KEY, boot_id TEXT NOT NULL, started_at TEXT NOT NULL,
            ended_at TEXT, status TEXT NOT NULL)''',
            '''CREATE TABLE run_events(id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id),
            position INTEGER, project TEXT, instance_name TEXT, action TEXT NOT NULL, status TEXT NOT NULL,
            timestamp TEXT NOT NULL, duration_ms INTEGER NOT NULL, error TEXT NOT NULL)''',
            'PRAGMA user_version=1',
        ]
        with self.transaction():
            for sql in statements:
                self.db.execute(sql)

    @property
    def revision(self):
        return int(self.get_setting('queue_revision', 0))

    def load_queue(self):
        self.ensure_current()
        rows = [dict(r) for r in self.db.execute('SELECT * FROM queue_entries ORDER BY position')]
        for r in rows:
            r['enabled'] = bool(r['enabled'])
        validate_queue(rows)
        return rows

    def _replace_queue(self, entries):
        self.db.execute('DELETE FROM queue_entries')
        for i, r in enumerate(entries):
            self.db.execute('INSERT INTO queue_entries(position,kind,project,instance_name,enabled,delay_seconds) VALUES(?,?,?,?,?,?)',
                            (i, r['kind'], r.get('project'), r.get('instance_name'), int(r.get('enabled', True)), r.get('delay_seconds') or 0))
        self.set_setting('queue_revision', self.revision + 1)

    def save_queue(self, entries, expected_revision=None):
        validate_queue(entries)
        with self.transaction():
            self._check_revision(expected_revision)
            self._replace_queue(entries)

    def _check_revision(self, expected):
        if expected is not None and expected != self.revision:
            raise AppError("Kolejka została zmieniona w innym oknie. Odczytaj ją ponownie.")

    def stage_queue(self, entries, changes, expected_revision):
        validate_queue(entries)
        with self.transaction():
            self._check_revision(expected_revision)
            self.set_setting('pending_configuration', {'queue': entries, 'changes': changes})
            self.set_setting('config_state', 'pending')

    def commit_staged(self):
        with self.transaction():
            pending = self.get_setting('pending_configuration')
            if not pending:
                raise AppError("Brak konfiguracji do zatwierdzenia.")
            validate_queue(pending['queue'])
            self._replace_queue(pending['queue'])
            self.set_setting('pending_configuration', None)
            self.set_setting('config_state', 'ready')

    def reset_configuration(self, expected_revision):
        """Clear application data atomically, keeping schema and a stale-editor guard."""
        with self.transaction():
            self._check_revision(expected_revision)
            revision = self.revision + 1
            self.db.execute('DELETE FROM run_events')
            self.db.execute('DELETE FROM runs')
            self.db.execute('DELETE FROM queue_entries')
            self.db.execute('DELETE FROM settings')
            self.set_setting('queue_revision', revision)
        return revision

    def get_setting(self, key, default=None):
        self.ensure_current()
        r = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_setting(self, key, value):
        self.ensure_current()
        if any(word in key.lower() for word in ('password', 'token', 'secret', 'authorization')):
            raise AppError("Sekrety nie mogą być przechowywane w bazie.")
        self.db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, json.dumps(value)))

    def create_run(self, boot_id):
        return self.db.execute("INSERT INTO runs(boot_id,started_at,status) VALUES(?,?,'running')", (boot_id, utcnow())).lastrowid

    def finish_run(self, run_id, status):
        self.db.execute('UPDATE runs SET ended_at=?,status=? WHERE id=?', (utcnow(), status, run_id))

    def add_event(self, run_id, position=None, project=None, instance_name=None, action='', status='', duration_ms=0, error='', **extra):
        self.db.execute('INSERT INTO run_events(run_id,position,project,instance_name,action,status,timestamp,duration_ms,error) VALUES(?,?,?,?,?,?,?,?,?)',
                        (run_id, position, project, instance_name, action, status, utcnow(), int(duration_ms), str(error)))

    def runs(self):
        self.ensure_current()
        return [dict(r) for r in self.db.execute('SELECT * FROM runs ORDER BY id DESC LIMIT 100')]

    def events(self, run_id):
        self.ensure_current()
        return [dict(r) for r in self.db.execute('SELECT * FROM run_events WHERE run_id=? ORDER BY id', (run_id,))]
