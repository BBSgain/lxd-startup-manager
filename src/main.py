#!/usr/bin/python3
"""Linux command entry point."""
import argparse
import os
from pathlib import Path
import signal
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.common import AppError


def check_platform():
    try:
        values = {}
        for line in Path('/etc/os-release').read_text().splitlines():
            key, sep, value = line.partition('=')
            if sep:
                values[key] = value.strip('"')
    except OSError as exc:
        raise AppError("Nie można rozpoznać systemu operacyjnego.") from exc
    if values.get('ID') != 'ubuntu' or values.get('VERSION_ID') != '26.04':
        raise AppError("Obsługiwany system: wyłącznie Ubuntu Server 26.04 LTS.")
    if os.geteuid() != 0:
        raise AppError("Uruchom aplikację jako root (sudo lxd-startup-manager).")


def find_lxc():
    for candidate in ('/snap/bin/lxc', '/usr/bin/lxc'):
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise AppError("Brak klienta Canonical LXD (lxc). Zainstaluj i skonfiguruj LXD samodzielnie.")


def main(argv=None):
    parser = argparse.ArgumentParser(prog='lxd-startup-manager')
    parser.add_argument('--database', default='/var/lib/lxd-startup-manager/state.db')
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('init')
    sub.add_parser('check')
    sub.add_parser('tui')
    repair_parser = sub.add_parser('repair')
    repair_parser.add_argument('--confirm', action='store_true', required=True,
                               help='Potwierdź dokończenie oczekującego zapisu ustawień autostartu LXD.')
    run_parser = sub.add_parser('run')
    run_parser.add_argument('--recover', action='store_true')
    args = parser.parse_args(argv)
    store = logger = None
    try:
        os.umask(0o077)
        check_platform()
        from src.database import Store
        store = Store(args.database)
        if args.command == 'init':
            return 0
        from src.lxd import LXD
        lxd = LXD(executable=find_lxc())
        if args.command == 'check':
            lxd.ready(store.get_setting('api_timeout_seconds', 60))
            print("Ubuntu 26.04, baza i lokalne API LXD: dostępne.")
            return 0
        from src.observability import Logger
        logger = Logger()
        if args.command == 'run':
            from src.engine import Engine
            def terminate(signum, frame):
                raise SystemExit(128 + signum)
            signal.signal(signal.SIGTERM, terminate)
            status = Engine(store, lxd, logger).run(recover=args.recover)
            print(status)
            return 0 if status in ('completed', 'already_executed') else 1
        from src.controller import Controller
        if args.command == 'repair':
            Controller(store, lxd, logger).repair(confirmed=args.confirm)
            print("Oczekująca konfiguracja zapisana.")
            return 0
        from src.tui import run
        run(Controller(store, lxd, logger))
        return 0
    except (AppError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"Błąd: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Przerwano. Stan zapisany; ponowienie kolejki wymaga run --recover.", file=sys.stderr)
        return 130
    finally:
        if logger is not None:
            logger.close()
        if store is not None:
            store.close()


if __name__ == '__main__':
    sys.exit(main())
