#!/bin/bash
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
exec /usr/bin/python3 - "$@" <<'PY'
import hashlib
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

NAME = 'lxd-startup-manager'
OWNER = 'lxd-startup-manager-owned-v1\n'
os.umask(0o077)

def fail(message):
    raise SystemExit('Deinstalacja przerwana: ' + message)

args = sys.argv[1:]
force = '--force' in args
if force:
    args.remove('--force')
root = Path('/')
testing = False
if args:
    if len(args) != 2 or args[0] != '--test-root':
        fail('użycie: uninstall.sh [--force] [--test-root IZOLowany_KATALOG]')
    root = Path(args[1])
    if not root.is_absolute() or root == Path('/') or root.resolve() != root:
        fail('niebezpieczny katalog testowy')
    marker = root / '.lxd-startup-manager-test-root'
    if not marker.is_file() or marker.is_symlink() or marker.read_text() != OWNER:
        fail('brak znacznika izolowanego środowiska testowego')
    testing = True
elif os.geteuid() != 0:
    fail('wymagany root')

def target(path):
    return root / path.lstrip('/')

def safe(path, directory=False):
    for item in [path, *path.parents]:
        if not item.exists() and not item.is_symlink():
            continue
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode):
            fail('symlink: ' + str(item))
        if info.st_uid not in ((0, os.geteuid()) if testing else (0,)):
            fail('obcy właściciel: ' + str(item))
        # Ubuntu may share /var/log with the syslog group. Only this parent
        # may be group-writable; the private application directory stays strict.
        log_parent = item == root / 'var' / 'log'
        trusted_log_parent = (log_parent and stat.S_ISDIR(info.st_mode)
                              and info.st_uid == (os.geteuid() if testing else 0)
                              and not info.st_mode & 0o002)
        sticky_directory = (not log_parent and stat.S_ISDIR(info.st_mode)
                            and info.st_mode & stat.S_ISVTX)
        if info.st_mode & 0o022 and not (trusted_log_parent or sticky_directory):
            fail('zapisywalna przez innych ścieżka: ' + str(item))
    if path.exists():
        info = path.stat()
        if directory and not stat.S_ISDIR(info.st_mode):
            fail('oczekiwano katalogu: ' + str(path))
        if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            fail('oczekiwano zwykłego pliku bez hardlinków: ' + str(path))
        if os.path.ismount(path) and path != root:
            fail('punkt montowania: ' + str(path))

safe(root, True)
code = target('/usr/local/lib/' + NAME)
data = target('/var/lib/' + NAME)
config = target('/etc/' + NAME)
logs = target('/var/log/' + NAME)
unit = target('/etc/systemd/system/' + NAME + '.service')
launcher = target('/usr/local/bin/' + NAME)
manifest_path = data / 'installation.json'
legacy_modules = ['common.py', 'database.py', 'engine.py', 'lxd.py', 'controller.py', 'observability.py', 'tui.py', 'main.py']
packages = {
    'src': ['__init__.py', 'database.py', 'engine.py', 'lxd.py', 'controller.py', 'observability.py', 'tui.py', 'main.py', 'systemd.py'],
    'lib': ['__init__.py', 'common.py'],
}
modules = ['version.txt', *legacy_modules, *(package + '/' + name for package, names in packages.items() for name in names)]
allowed = {str(p.relative_to(root)): p for p in [unit, launcher, *(code / name for name in modules)]}
for directory in (code, data, config, logs):
    safe(directory, True)
    if directory.exists():
        marker = directory / '.owner'
        safe(marker)
        if not marker.is_file() or marker.read_text() != OWNER:
            fail('obcy katalog: ' + str(directory))
safe(manifest_path)
if not manifest_path.exists():
    if any(path.exists() or path.is_symlink() for path in allowed.values()) or any(p.exists() for p in (code, data, config, logs)):
        fail('brak manifestu własności; niczego nie usunięto')
    print('Aplikacja jest już odinstalowana.')
    raise SystemExit(0)
manifest = json.loads(manifest_path.read_text())
manifest_snapshot = manifest_path.read_bytes()
if manifest.get('owner') != OWNER or not isinstance(manifest.get('files'), dict) or set(manifest['files']) - set(allowed):
    fail('nieprawidłowy manifest własności')
for key, path in allowed.items():
    safe(path)
    if path.exists() and manifest['files'].get(key) != hashlib.sha256(path.read_bytes()).hexdigest():
        fail('obcy lub zmieniony plik: ' + str(path))
if code.exists() and {p.name for p in code.iterdir()} - {'.owner', 'version.txt', *legacy_modules, *packages}:
    fail('obce pliki aplikacji')
for package, names in packages.items():
    directory = code / package
    safe(directory, True)
    if directory.exists() and {p.name for p in directory.iterdir()} - set(names):
        fail('obce pliki w pakiecie aplikacji: ' + str(directory))
dropins = unit.with_name(unit.name + '.d')
safe(dropins, True)
if dropins.exists():
    fail('drop-in wymaga ręcznej kontroli')
for base in ('/run/systemd/system', '/usr/lib/systemd/system', '/lib/systemd/system'):
    other = target(base + '/' + NAME + '.service')
    if other.exists() or other.is_symlink() or other.with_name(other.name + '.d').exists():
        fail('konflikt jednostki: ' + str(other))

log_names = {'.owner', 'events.lock', 'events.jsonl', *(f'events.jsonl.{i}' for i in range(1, 6))}

def validate_logs():
    safe(logs, True)
    if not logs.exists():
        return None
    info = logs.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        fail('katalog logów wymaga właściciela procesu i uprawnień 0700')
    if {p.name for p in logs.iterdir()} - log_names:
        fail('obce pliki w katalogu logów: ' + str(logs))
    marker = logs / '.owner'
    safe(marker)
    if not marker.is_file() or marker.read_text() != OWNER:
        fail('obcy katalog: ' + str(logs))
    for path in logs.iterdir():
        safe(path)
    return info.st_dev, info.st_ino

log_identity = validate_logs()
purge = []
if force:
    for directory, names in ((data, {'.owner', 'installation.json', 'state.db', 'state.db-wal', 'state.db-shm', 'state.db-journal', 'state.db.v0.bak'}), (config, {'.owner', 'openobserve.json'})):
        if directory.exists():
            if {p.name for p in directory.iterdir()} - names:
                fail('obce pliki w ' + str(directory))
            for name in names:
                path = directory / name
                safe(path)
                if path.exists():
                    purge.append(path)
systemctl = root / '.test-bin/systemctl' if testing else Path('/usr/bin/systemctl')
safe(systemctl)
if not systemctl.is_file():
    fail('brak systemctl')
def run(*args):
    subprocess.run([str(systemctl), *args], check=True)
# Shared with queue execution/TUI saves. Keep this inode even after --force.
lock_directory = target('/run/' + NAME)
lock_path = lock_directory / 'operation.lock'
safe(lock_directory, True)
safe(lock_path)
if lock_directory.exists() and (lock_directory.stat().st_uid != os.geteuid() or stat.S_IMODE(lock_directory.stat().st_mode) != 0o700):
    fail('katalog blokady wymaga właściciela procesu i uprawnień 0700')
if lock_path.exists() and (lock_path.stat().st_uid != os.geteuid() or stat.S_IMODE(lock_path.stat().st_mode) != 0o600):
    fail('plik blokady wymaga właściciela procesu i uprawnień 0600')
lock_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
try:
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    fail('trwa wykonywanie kolejki, zapis konfiguracji lub instalacja; zaczekaj na zakończenie i ponów polecenie')
if not manifest_path.exists() or manifest_path.read_bytes() != manifest_snapshot:
    fail('stan instalacji zmienił się podczas kontroli; ponów polecenie')
if validate_logs() != log_identity:
    fail('katalog logów zmienił się podczas kontroli; ponów polecenie')
# Pin the verified directory: never traverse a replacement under shared /var/log
# when unlinking files. No recursive removal and no following links.
log_fd = None
if log_identity is not None:
    log_fd = os.open(logs, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(log_fd)
    if (info.st_dev, info.st_ino) != log_identity:
        fail('katalog logów zmienił się podczas kontroli; ponów polecenie')
if unit.exists():
    run('stop', NAME + '.service')
    run('disable', NAME + '.service')
if log_fd is not None:
    if validate_logs() != log_identity:
        fail('katalog logów zmienił się podczas deinstalacji; ponów polecenie')
    for name in sorted(log_names - {'.owner'}):
        try:
            os.unlink(name, dir_fd=log_fd)
        except FileNotFoundError:
            pass
    os.unlink('.owner', dir_fd=log_fd)
    logs.rmdir()
    os.close(log_fd)
for path in [launcher, *(code / name for name in modules)]:
    path.unlink(missing_ok=True)
if code.exists():
    for package in packages:
        directory = code / package
        if directory.exists():
            directory.rmdir()
    (code / '.owner').unlink()
    code.rmdir()
if force:
    unit.unlink(missing_ok=True)
    for path in purge:
        path.unlink(missing_ok=True)
    for directory in (config, data):
        if directory.exists():
            directory.rmdir()
    run('daemon-reload')
print('Odinstalowano. ' + ('Usunięto własną bazę, sekrety i unit. ' if force else 'Zachowano bazę, sekrety i wyłączony unit. ') + 'Usunięto logi i katalog logów aplikacji.')
PY
