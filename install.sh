#!/bin/bash
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export LSM_SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec /usr/bin/python3 - "$@" <<'PY'
import hashlib
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

NAME = 'lxd-startup-manager'
OWNER = 'lxd-startup-manager-owned-v1\n'
os.umask(0o077)
SOURCE = Path(os.environ.get('LSM_SOURCE_DIR', '.')).absolute()

def fail(message):
    raise SystemExit('Instalacja przerwana: ' + message)

args = sys.argv[1:]
root = Path('/')
testing = False
if args:
    if len(args) != 2 or args[0] != '--test-root':
        fail('użycie: install.sh [--test-root IZOLowany_KATALOG]')
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

def run(command, **kw):
    return subprocess.run(command, check=True, text=True, **kw)

def tool(name):
    if testing:
        path = root / '.test-bin' / name
        safe(path)
        if not path.is_file():
            fail('brak mocka: ' + name)
        return str(path)
    return '/usr/bin/' + name

safe(root, True)
release = target('/etc/os-release')
if release.is_symlink():
    resolved_release = release.resolve()
    if resolved_release != target('/usr/lib/os-release'):
        fail('nieoczekiwane dowiązanie /etc/os-release')
    release = resolved_release
safe(release)
if not release.is_file():
    fail('brak /etc/os-release')
values = {}
for line in release.read_text().splitlines():
    if '=' in line:
        key, value = line.split('=', 1)
        values[key] = value.strip('"\'')
if values.get('ID') != 'ubuntu' or values.get('VERSION_ID') != '26.04':
    fail('wymagany Ubuntu Server 26.04 LTS')
if not testing and not target('/run/systemd/system').is_dir():
    fail('wymagany działający systemd')
import curses, sqlite3  # Validate the required Ubuntu Python standard library.
systemctl = tool('systemctl')
analyze = tool('systemd-analyze')
lxc = tool('lxc') if testing else ('/snap/bin/lxc' if Path('/snap/bin/lxc').exists() else '/usr/bin/lxc')
if not Path(lxc).is_file():
    fail('zainstaluj i skonfiguruj Canonical LXD; instalator go nie instaluje')
try:
    response = json.loads(run([lxc, '--force-local', 'query', '/1.0'], capture_output=True, timeout=30).stdout)
    metadata = response.get('metadata', response)
    if metadata.get('environment', {}).get('server') != 'lxd':
        fail('lokalne API nie jest Canonical LXD')
except (subprocess.SubprocessError, ValueError) as exc:
    fail('lokalne API LXD jest niedostępne: ' + type(exc).__name__)

code = target('/usr/local/lib/' + NAME)
data = target('/var/lib/' + NAME)
config = target('/etc/' + NAME)
logs = target('/var/log/' + NAME)
unit = target('/etc/systemd/system/' + NAME + '.service')
launcher = target('/usr/local/bin/' + NAME)
manifest_path = data / 'installation.json'
dirs = (code, data, config, logs)
for directory in dirs:
    safe(directory, True)
    if directory.exists() and any(directory.iterdir()):
        marker = directory / '.owner'
        safe(marker)
        if not marker.is_file() or marker.read_text() != OWNER:
            fail('obcy katalog: ' + str(directory))
safe(manifest_path)
manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {'owner': OWNER, 'files': {}}
manifest_snapshot = manifest_path.read_bytes() if manifest_path.exists() else None
if manifest.get('owner') != OWNER or not isinstance(manifest.get('files'), dict):
    fail('nieprawidłowy manifest')
legacy_modules = ['common.py', 'database.py', 'engine.py', 'lxd.py', 'controller.py', 'observability.py', 'tui.py', 'main.py']
packages = {
    'src': ['__init__.py', 'database.py', 'engine.py', 'lxd.py', 'controller.py', 'observability.py', 'tui.py', 'main.py', 'systemd.py'],
    'lib': ['__init__.py', 'common.py'],
}
modules = ['version.txt', *(package + '/' + name for package, names in packages.items() for name in names)]
payload = {}
for relative in ('src', 'lib', 'config', 'config/systemd'):
    source_directory = SOURCE / relative
    if not source_directory.is_dir() or source_directory.is_symlink():
        fail('brak katalogu źródłowego: ' + relative)
for module in modules:
    source = SOURCE / module
    if not source.is_file() or source.is_symlink():
        fail('brak pliku źródłowego: ' + module)
    payload[code / module] = source.read_bytes()
source_unit = SOURCE / 'config/systemd' / (NAME + '.service')
if not source_unit.is_file() or source_unit.is_symlink():
    fail('brak własnej jednostki systemd')
payload[unit] = source_unit.read_bytes()
payload[launcher] = b'#!/bin/sh\n# Managed by lxd-startup-manager\nexport PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin\nexec /usr/bin/python3 -B /usr/local/lib/lxd-startup-manager/src/main.py "$@"\n'
# Legacy paths are accepted only with a matching ownership-manifest hash.
# This permits an upgrade without treating arbitrary old Python files as ours.
legacy_paths = [code / name for name in legacy_modules]
allowed = {str(path.relative_to(root)): path for path in [*payload, *legacy_paths]}
if set(manifest['files']) - set(allowed):
    fail('obce ścieżki w manifeście')
for path in allowed.values():
    safe(path)
    if path.exists():
        old_hash = manifest['files'].get(str(path.relative_to(root)))
        if old_hash != hashlib.sha256(path.read_bytes()).hexdigest():
            fail('obcy lub zmieniony plik: ' + str(path))
if code.exists() and {p.name for p in code.iterdir()} - {'.owner', 'version.txt', *legacy_modules, *packages}:
    fail('obce pliki w katalogu aplikacji')
for package, names in packages.items():
    directory = code / package
    safe(directory, True)
    if directory.exists() and {p.name for p in directory.iterdir()} - set(names):
        fail('obce pliki w pakiecie aplikacji: ' + str(directory))
dropins = unit.with_name(unit.name + '.d')
safe(dropins, True)
if dropins.exists():
    fail('wykryto drop-in jednostki; wymagana ręczna kontrola')
for base in ('/run/systemd/system', '/usr/lib/systemd/system', '/lib/systemd/system'):
    other = target(base + '/' + NAME + '.service')
    if other.exists() or other.is_symlink() or other.with_name(other.name + '.d').exists():
        fail('konflikt jednostki: ' + str(other))
for path in (data / 'state.db', data / 'state.db-wal', data / 'state.db-shm', data / 'state.db-journal', data / 'state.db.v0.bak', config / 'openobserve.json'):
    safe(path)
# Validate syntax/dependencies before changes. The real launcher does not exist
# on a fresh host yet, so use an existing interpreter for this offline check.
with tempfile.TemporaryDirectory(prefix=NAME + '-verify-') as staging:
    staged_unit = Path(staging) / unit.name
    unit_text = source_unit.read_text()
    original_exec = 'ExecStart=/usr/local/bin/lxd-startup-manager run'
    if unit_text.count(original_exec) != 1:
        fail('nieoczekiwany ExecStart jednostki')
    staged_unit.write_text(unit_text.replace(original_exec, 'ExecStart=/usr/bin/python3 -c pass'))
    run([analyze, 'verify', '--man=no', '--generators=no', str(staged_unit)])
# Use the same inode as the engine and controller; never unlink this lock.
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
if (manifest_path.read_bytes() if manifest_path.exists() else None) != manifest_snapshot:
    fail('stan instalacji zmienił się podczas kontroli; ponów polecenie')

def prepare_logs():
    # /var/log is shared, so pin both directories and never follow a replacement
    # symlink when creating/chmodding our directory or its ownership marker.
    logs.parent.mkdir(parents=True, exist_ok=True)
    safe(logs.parent, True)
    parent_fd = os.open(logs.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.mkdir(logs.name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        directory_fd = os.open(logs.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            info = os.fstat(directory_fd)
            if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                fail('niebezpieczny katalog logów: ' + str(logs))
            existing = bool(os.listdir(directory_fd))
            flags = os.O_RDONLY if existing else os.O_WRONLY | os.O_CREAT | os.O_EXCL
            marker_fd = os.open('.owner', flags | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
            try:
                info = os.fstat(marker_fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_mode & 0o022:
                    fail('niebezpieczny znacznik katalogu logów')
                if existing:
                    if os.read(marker_fd, len(OWNER) + 1) != OWNER.encode():
                        fail('obcy katalog logów: ' + str(logs))
                else:
                    os.write(marker_fd, OWNER.encode())
                os.fchmod(marker_fd, 0o600)
            finally:
                os.close(marker_fd)
            os.fchmod(directory_fd, 0o700)
        finally:
            os.close(directory_fd)
    finally:
        os.close(parent_fd)

try:
    prepare_logs()
except OSError:
    fail('nie można bezpiecznie utworzyć katalogu logów; sprawdź właściciela i dowiązania: ' + str(logs))
for directory in dirs:
    if directory == logs:
        continue
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    (directory / '.owner').write_text(OWNER)
    (directory / '.owner').chmod(0o600)
for package in packages:
    directory = code / package
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
for path, content in payload.items():
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.installing')
    if temporary.exists() or temporary.is_symlink():
        fail('pozostałość instalacji: ' + str(temporary))
    with temporary.open('xb') as stream:
        stream.write(content)
    temporary.chmod(0o755 if path == launcher else 0o644)
    temporary.replace(path)
for path in legacy_paths:
    path.unlink(missing_ok=True)
manifest = {'owner': OWNER, 'files': {str(path.relative_to(root)): hashlib.sha256(content).hexdigest() for path, content in payload.items()}}
manifest_path.write_text(json.dumps(manifest, sort_keys=True))
manifest_path.chmod(0o600)
run(['/usr/bin/python3', '-B', str(code / 'src/main.py'), '--database', str(data / 'state.db'), 'init'])
for path in (data / 'state.db', data / 'state.db-wal', data / 'state.db-shm'):
    if path.exists():
        safe(path)
        path.chmod(0o600)
run([systemctl, 'daemon-reload'])
run([analyze, 'verify', '--man=no', '--generators=no', str(unit)])
run([systemctl, 'enable', NAME + '.service'])
run([systemctl, 'show', NAME + '.service', '--property=LoadState,UnitFileState,ActiveState,SubState'])
print('Zainstalowano. Usługa włączona na start hosta; instalacja nie uruchamia kolejki. Baza, sekrety i logi zachowane.')
PY
