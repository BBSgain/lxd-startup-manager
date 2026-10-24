"""Local LXD CLI boundary; never changes daemon or unrelated instances."""
import json
import math
import subprocess
import time
from urllib.parse import unquote, urlsplit

from lib.common import AppError, validate_name


class LXDTimeout(AppError):
    """The client deadline elapsed; the server operation may still be active."""


class LXD:
    def __init__(self, executable='lxc', command_timeout=15, runner=None,
                 sleep=None, monotonic=None):
        if executable != 'lxc' and not (isinstance(executable, str) and executable.startswith('/')):
            raise AppError('Nieprawidłowa ścieżka programu lxc.')
        self.executable = executable
        self.command_timeout = self._timeout(command_timeout)
        self._runner = runner or subprocess.run
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic

    @staticmethod
    def _timeout(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise AppError('Nieprawidłowy limit czasu LXD.')
        return value

    def _run(self, args, project='default', timeout=None):
        validate_name(project)
        argv = [self.executable, '--force-local', '--project', project, *args]
        try:
            result = self._runner(argv, capture_output=True, text=True,
                                  timeout=self._timeout(timeout or self.command_timeout),
                                  check=False, shell=False, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as exc:
            raise LXDTimeout('Upłynął limit czasu klienta LXD; wynik operacji wymaga sprawdzenia.') from exc
        except OSError as exc:
            raise AppError('Nie można uruchomić lokalnego klienta LXD: ' + str(exc)) from exc
        if result.returncode:
            message = (result.stderr or result.stdout or 'błąd klienta LXD').strip()
            error = AppError(message[:2000])
            error.exit_code = result.returncode
            raise error
        return result.stdout

    def _json(self, args, project='default', timeout=None):
        try:
            return json.loads(self._run(args, project, timeout) or 'null')
        except (ValueError, TypeError) as exc:
            raise AppError('Nieprawidłowa odpowiedź JSON lokalnego LXD.') from exc

    def ready(self, timeout=60):
        deadline = self._monotonic() + self._timeout(timeout)
        last_error = None
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise AppError('Lokalne API LXD nie jest dostępne w wyznaczonym czasie.') from last_error
            try:
                result = self._json(['project', 'list', '--format', 'json'], timeout=min(self.command_timeout, remaining))
                if not isinstance(result, list) or not result or any(not isinstance(p, dict) or not p.get('name') for p in result):
                    raise AppError('Lokalne API LXD nie potwierdziło uwierzytelnienia.')
                return
            except AppError as exc:
                last_error = exc
                remaining = deadline - self._monotonic()
                if remaining > 0:
                    self._sleep(min(1, remaining))

    def inventory(self):
        projects = self._json(['project', 'list', '--format', 'json'])
        if not isinstance(projects, list):
            raise AppError('Nieprawidłowa lista projektów LXD.')
        result = []
        for project in projects:
            if not isinstance(project, dict):
                raise AppError('Nieprawidłowy projekt LXD.')
            name = validate_name(project.get('name'))
            instances = self._json(['list', '--format', 'json'], name)
            if not isinstance(instances, list):
                raise AppError('Nieprawidłowa lista instancji LXD.')
            for item in instances:
                if not isinstance(item, dict):
                    raise AppError('Nieprawidłowa instancja LXD.')
                instance = validate_name(item.get('name'))
                if not isinstance(item.get('config'), dict) or not isinstance(item.get('expanded_config'), dict):
                    raise AppError('Nieprawidłowa konfiguracja instancji LXD.')
                result.append(dict(project=name, instance_name=instance,
                                   status=str(item.get('status', '')).upper(),
                                   type=item.get('type'), config=item['config'],
                                   expanded_config=item['expanded_config']))
        return result

    def _instance(self, project, name, timeout=None):
        validate_name(project)
        validate_name(name)
        result = self._json(['list', '--format', 'json', name], project, timeout)
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise AppError('Nieprawidłowe dane instancji LXD.')
        matches = [item for item in result if item.get('name') == name]
        if len(matches) != 1:
            raise AppError('LXD nie potwierdził istnienia wskazanej instancji.')
        return matches[0]

    def state(self, project, name, timeout=None):
        result = self._instance(project, name, timeout)
        if not isinstance(result.get('status'), str):
            raise AppError('LXD nie potwierdził stanu instancji.')
        return result['status'].upper()

    def start(self, project, name, timeout=60):
        validate_name(project)
        validate_name(name)
        self._run(['start', name], project, self._timeout(timeout))

    def get_autostart(self, project, name):
        config = self._instance(project, name).get('config')
        if not isinstance(config, dict):
            raise AppError('Nieprawidłowa konfiguracja LXD.')
        return config.get('boot.autostart')

    def effective_autostart(self, project, name):
        config = self._instance(project, name).get('expanded_config')
        if not isinstance(config, dict):
            raise AppError('Nieprawidłowa konfiguracja efektywna LXD.')
        return config.get('boot.autostart', '')

    def set_autostart(self, project, name, value):
        validate_name(project)
        validate_name(name)
        if value is None:
            self._run(['config', 'unset', name, 'boot.autostart'], project)
        elif value in ('true', 'false', '') and isinstance(value, str):
            self._run(['config', 'set', name, 'boot.autostart=' + value], project)
        else:
            raise AppError('Nieprawidłowa wartość boot.autostart.')

    def has_pending_operations(self, project, name, timeout=None):
        validate_name(project)
        validate_name(name)
        operations = self._json(['operation', 'list', '--format', 'json'], project, timeout)
        if not isinstance(operations, list):
            raise AppError('Nie można potwierdzić listy operacji LXD.')
        for operation in operations:
            if not isinstance(operation, dict) or type(operation.get('status_code')) is not int:
                raise AppError('Nie można potwierdzić stanu operacji LXD.')
            code = operation['status_code']
            if code in (200, 400, 401):
                continue
            if not 100 <= code < 200:
                raise AppError('Nieznany stan operacji LXD.')
            resources = operation.get('resources')
            if not isinstance(resources, dict) or not resources:
                return True
            for kind, urls in resources.items():
                if not isinstance(urls, list) or any(not isinstance(url, str) for url in urls):
                    raise AppError('Nie można ustalić zasobów operacji LXD.')
                for url in urls:
                    path = unquote(urlsplit(url).path)
                    if kind in ('instances', 'containers') and path.split('/')[1:4] in (['1.0', 'instances', name], ['1.0', 'containers', name]):
                        return True
        return False

    def wait_for_idle(self, project, name, timeout=60):
        """Wait for existing operations, then read the instance state anew."""
        timeout = self._timeout(timeout)
        deadline = self._monotonic() + timeout

        def remaining_timeout():
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise AppError(f'Upłynął limit {timeout:g} s oczekiwania na zakończenie '
                               'operacji LXD i potwierdzenie stanu instancji; start jest wstrzymany.')
            return min(self.command_timeout, remaining)

        while self.has_pending_operations(project, name, timeout=remaining_timeout()):
            self._sleep(min(1, remaining_timeout()))
        state = self.state(project, name, timeout=remaining_timeout())
        remaining_timeout()
        return state

    def reconcile_start(self, project, name, attempts=3):
        if type(attempts) is not int or not 1 <= attempts <= 10:
            raise AppError('Nieprawidłowa liczba prób odczytu stanu LXD.')
        stopped = 0
        for attempt in range(attempts):
            try:
                # Operations first, state second: STOPPED alone is never sufficient.
                pending = self.has_pending_operations(project, name)
                state = self.state(project, name)
                if not pending and state == 'RUNNING':
                    return 'started'
                if not pending and state == 'STOPPED':
                    stopped += 1
                else:
                    stopped = 0
            except AppError:
                stopped = 0
            if attempt + 1 < attempts:
                self._sleep(1)
        return 'failed' if stopped == attempts else 'unknown'


