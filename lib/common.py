"""Shared validation and local resource protection."""
import contextlib
import fcntl
import os
from pathlib import Path
import re
import stat


class AppError(Exception):
    pass


def validate_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", value):
        raise AppError("Nieprawidłowa nazwa projektu lub instancji.")
    return value


def validate_queue(entries):
    if not isinstance(entries, list) or len(entries) > 10000:
        raise AppError("Nieprawidłowa kolejka.")
    seen = set()
    for i, row in enumerate(entries):
        if not isinstance(row, dict) or type(row.get('enabled', True)) is not bool:
            raise AppError("Nieprawidłowy wiersz kolejki.")
        if row.get('kind') == 'instance':
            key = (validate_name(row.get('project')), validate_name(row.get('instance_name')))
            if key in seen or row.get('delay_seconds', 0) not in (0, None):
                raise AppError("Powtórzona instancja lub błędne opóźnienie.")
            seen.add(key)
        elif row.get('kind') == 'delay':
            seconds = row.get('delay_seconds')
            if type(seconds) is not int or not 0 <= seconds <= 86400:
                raise AppError("Pauza musi wynosić od 0 do 86400 sekund.")
            if row.get('project') or row.get('instance_name'):
                raise AppError("Wiersz pauzy nie może wskazywać instancji.")
            if i == 0 or i == len(entries)-1 or entries[i-1].get('kind') != 'instance' or entries[i+1].get('kind') != 'instance':
                raise AppError("Pauza musi znajdować się pomiędzy instancjami.")
        else:
            raise AppError("Nieznany rodzaj wiersza.")
    active = [r for r in entries if r.get('enabled', True)]
    for i, row in enumerate(active):
        if row['kind'] == 'delay' and (i == 0 or i == len(active)-1 or active[i-1]['kind'] != 'instance' or active[i+1]['kind'] != 'instance'):
            raise AppError("Aktywna pauza musi oddzielać dwie aktywne instancje.")


def secure_directory(path):
    path = Path(path)
    if not path.is_absolute():
        raise AppError("Wymagana jest ścieżka bezwzględna.")
    for parent in [*reversed(path.parents), path]:
        if parent.is_symlink():
            raise AppError("Dowiązania symboliczne nie są dozwolone.")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise AppError("Katalog aplikacji wymaga właściciela procesu i uprawnień 0700.")
    return path


def secure_file(path):
    path = Path(path)
    if path.is_symlink():
        raise AppError("Plik aplikacji nie może być dowiązaniem.")
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise AppError("Plik aplikacji wymaga właściciela procesu i uprawnień 0600.")
    return path


@contextlib.contextmanager
def execution_lock(path):
    path = Path(path)
    secure_directory(path.parent)
    secure_file(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AppError("Trwa wykonywanie kolejki lub zapis konfiguracji.") from None
        yield
    finally:
        os.close(fd)
