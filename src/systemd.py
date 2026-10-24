"""Remove only the verified application unit; retain its installation manifest."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from lib.common import AppError


OWNER = 'lxd-startup-manager-owned-v1\n'
UNIT = 'lxd-startup-manager.service'
UNIT_PATH = 'etc/systemd/system/' + UNIT


class SystemdReset:
    """The caller holds execution_lock throughout prepare(), LXD reset and remove()."""

    def __init__(self, root='/', runner=None):
        self.root = Path(root)
        self.runner = runner or subprocess.run
        self.unit = self.root / UNIT_PATH
        self.data = self.root / 'var/lib/lxd-startup-manager'
        self.manifest = self.data / 'installation.json'
        self.enable_link = self.unit.parent / 'multi-user.target.wants' / UNIT
        self.systemctl = (Path('/usr/bin/systemctl') if self.root == Path('/')
                          else self.root / '.test-bin/systemctl')
        self._prepared = None

    def _safe(self, path, directory=False, required=False):
        """Validate the complete path without following any symbolic links."""
        for item in [*reversed(path.parents), path]:
            try:
                info = item.lstat()
            except FileNotFoundError:
                if item == path and required:
                    raise AppError('Brak wymaganego pliku instalacji: ' + str(path)) from None
                continue
            if stat.S_ISLNK(info.st_mode):
                raise AppError('Dowiązanie w ścieżce systemd: ' + str(item))
            allowed_uid = (0, os.geteuid()) if self.root != Path('/') else (0,)
            if info.st_uid not in allowed_uid:
                raise AppError('Obcy właściciel ścieżki systemd: ' + str(item))
            # A private marked test root may live below the shared /tmp.
            outside_test_root = self.root != Path('/') and item in self.root.parents
            sticky_parent = outside_test_root and stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX
            if info.st_mode & 0o022 and not sticky_parent:
                raise AppError('Ścieżka systemd jest zapisywalna przez innych: ' + str(item))
            if item != path or directory:
                if not stat.S_ISDIR(info.st_mode):
                    raise AppError('Oczekiwano katalogu systemd: ' + str(item))
            elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise AppError('Plik systemd musi być zwykłym plikiem bez hardlinków: ' + str(item))
            if item == path and item != self.root and os.path.ismount(item):
                raise AppError('Plik systemd nie może być punktem montowania: ' + str(item))

    def _read(self, path):
        self._safe(path, required=True)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            actual = path.lstat()
            if ((info.st_dev, info.st_ino) != (actual.st_dev, actual.st_ino)
                    or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_mode & 0o022):
                raise AppError('Plik instalacji zmienił się podczas kontroli: ' + str(path))
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                content = stream.read()
            return content, (info.st_dev, info.st_ino)
        finally:
            os.close(fd)

    def _inspect(self):
        if not self.root.is_absolute() or self.root.resolve() != self.root:
            raise AppError('Wymagany bezpieczny bezwzględny katalog instalacji.')
        self._safe(self.root, directory=True, required=True)
        if self.root != Path('/'):
            marker, _ = self._read(self.root / '.lxd-startup-manager-test-root')
            if marker != OWNER.encode():
                raise AppError('Brak znacznika izolowanego środowiska testowego.')
        self._safe(self.data, directory=True, required=True)
        marker, _ = self._read(self.data / '.owner')
        raw, _ = self._read(self.manifest)
        if marker != OWNER.encode():
            raise AppError('Obcy znacznik własności instalacji.')
        try:
            manifest = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise AppError('Nieprawidłowy manifest instalacji.') from exc
        if not isinstance(manifest, dict) or manifest.get('owner') != OWNER or not isinstance(manifest.get('files'), dict):
            raise AppError('Nieprawidłowy manifest własności instalacji.')
        # Only this literal path can ever be removed, regardless of other entries.
        digest = manifest['files'].get(UNIT_PATH)
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise AppError('Manifest nie potwierdza własności jednostki systemd.')
        self._safe(self.unit)
        unit_identity = None
        if self.unit.exists():
            content, unit_identity = self._read(self.unit)
            if hashlib.sha256(content).hexdigest() != digest:
                raise AppError('Obca lub zmieniona jednostka systemd; niczego nie usunięto.')
        # Include transient/generated units and global service drop-ins, which
        # systemd may use even when the primary unit file is ours.
        bases = ('etc/systemd/system', 'run/systemd/system', 'usr/local/lib/systemd/system',
                 'usr/lib/systemd/system', 'lib/systemd/system', 'etc/systemd/system.control',
                 'run/systemd/system.control', 'run/systemd/transient',
                 'run/systemd/generator.early', 'run/systemd/generator', 'run/systemd/generator.late')
        for base in bases:
            directory = self.root / base
            paths = [directory / (UNIT + '.d'), directory / 'service.d']
            if base != 'etc/systemd/system':
                paths.append(directory / UNIT)
            for path in paths:
                if path.exists() or path.is_symlink():
                    raise AppError('Obcy drop-in lub konflikt jednostki systemd: ' + str(path))
        self._safe(self.enable_link.parent, directory=True)
        link_identity = None
        if self.enable_link.exists() or self.enable_link.is_symlink():
            info = self.enable_link.lstat()
            allowed_targets = {'/' + UNIT_PATH, '../' + UNIT, str(self.unit)}
            allowed_uid = (0, os.geteuid()) if self.root != Path('/') else (0,)
            if (not stat.S_ISLNK(info.st_mode) or info.st_uid not in allowed_uid
                    or info.st_nlink != 1 or os.readlink(self.enable_link) not in allowed_targets):
                raise AppError('Obcy link włączenia jednostki systemd; niczego nie usunięto.')
            link_identity = info.st_dev, info.st_ino
        # disable removes aliases too, including links made outside enable.
        # Refuse any such link except the one generated by our WantedBy.
        for base in ('etc/systemd/system', 'run/systemd/system'):
            directory = self.root / base
            if not directory.exists():
                continue
            self._safe(directory, directory=True)
            for parent, directories, files in os.walk(directory, followlinks=False):
                for name in [*directories, *files]:
                    path = Path(parent) / name
                    if path == self.unit or path == self.enable_link:
                        continue
                    if (name == UNIT or (path.is_symlink()
                            and Path(os.readlink(path)).name == UNIT)):
                        raise AppError('Obcy link lub alias własnej jednostki systemd: ' + str(path))
        self._safe(self.systemctl, required=True)
        return raw, marker, unit_identity, link_identity

    def _provenance(self, snapshot):
        if snapshot[2] is None:
            return
        state = self._run('show', UNIT, '--property=FragmentPath,DropInPaths')
        values = dict(line.split('=', 1) for line in state.splitlines() if '=' in line)
        if (values.get('FragmentPath') not in {'/' + UNIT_PATH, str(self.unit)}
                or values.get('DropInPaths') != ''):
            raise AppError('systemd używa obcej jednostki lub drop-in; niczego nie usunięto.')

    def prepare(self):
        """Read-only preflight. Conflicts block before any LXD/systemd mutation."""
        try:
            self._prepared = self._inspect()
            self._provenance(self._prepared)
        except OSError as exc:
            raise AppError('Nie można zweryfikować instalacji systemd: ' + str(exc)) from exc

    def _run(self, *args):
        try:
            result = self.runner([str(self.systemctl), *args], check=True, text=True,
                                 capture_output=True, timeout=60)
            if result.returncode != 0:
                raise AppError('systemctl ' + args[0] + ' nie zakończył się pomyślnie.')
            return result.stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise AppError('Nie udało się wykonać systemctl ' + args[0] + '; reset wymaga ponowienia.') from exc

    def remove(self):
        """Disable, stop and remove the verified unit; safe to retry on failure."""
        try:
            current = self._inspect()
            if self._prepared is not None and current != self._prepared:
                raise AppError('Instalacja systemd zmieniła się po kontroli; ponów reset.')
            self._prepared = current
            self._provenance(current)
            # Commands may remove the enable link before failing. Subsequent
            # retries must preflight that new state instead of a stale snapshot.
            self._prepared = None
            if current[2] is not None:
                self._run('disable', UNIT)
                self._run('stop', UNIT)
                state = self._run('show', UNIT, '--property=ActiveState,UnitFileState')
                values = dict(line.split('=', 1) for line in state.splitlines() if '=' in line)
                if values.get('ActiveState') not in ('inactive', 'failed') or values.get('UnitFileState') != 'disabled':
                    raise AppError('systemd nie potwierdził zatrzymania i wyłączenia własnej jednostki.')
                # Recheck all ownership/conflict conditions after systemctl calls.
                after_stop = self._inspect()
                if after_stop[:3] != current[:3] or after_stop[3] not in (None, current[3]):
                    raise AppError('Instalacja systemd zmieniła się podczas resetu.')
                directory_fd = os.open(self.unit.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    info = os.stat(self.unit.name, dir_fd=directory_fd, follow_symlinks=False)
                    if (info.st_dev, info.st_ino) != current[2]:
                        raise AppError('Jednostka systemd zmieniła się podczas resetu.')
                    os.unlink(self.unit.name, dir_fd=directory_fd)
                finally:
                    os.close(directory_fd)
            # A missing unit can leave the exact enable link created by install.
            # Never ask systemctl to disable a missing file or follow its target.
            remaining = self._inspect()
            if remaining[3] is not None:
                if remaining[3] != current[3]:
                    raise AppError('Link włączenia jednostki zmienił się podczas resetu.')
                directory_fd = os.open(self.enable_link.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    info = os.stat(self.enable_link.name, dir_fd=directory_fd, follow_symlinks=False)
                    if (info.st_dev, info.st_ino) != remaining[3]:
                        raise AppError('Link włączenia jednostki zmienił się podczas resetu.')
                    os.unlink(self.enable_link.name, dir_fd=directory_fd)
                finally:
                    os.close(directory_fd)
            # Retain the original hash in the manifest: install.sh accepts a
            # missing owned unit, recreates it and detects any foreign replacement.
            self._prepared = None
            self._run('daemon-reload')
        except OSError as exc:
            raise AppError('Nie można usunąć własnej jednostki systemd; ponów reset: ' + str(exc)) from exc
