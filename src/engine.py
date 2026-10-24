"""Single sequential startup run for a committed SQLite queue snapshot."""
import time
from pathlib import Path

from lib.common import AppError, execution_lock, validate_queue
from src.lxd import LXDTimeout


class Engine:
    def __init__(self, store, lxd, logger,
                 lock_path='/run/lxd-startup-manager/operation.lock', *,
                 monotonic=None, sleep=None, boot_id=None):
        self.store = store
        self.lxd = lxd
        self.logger = logger
        self.lock_path = lock_path
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self._boot_id = boot_id or self._read_boot_id

    @staticmethod
    def _read_boot_id():
        import uuid
        try:
            value = Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip()
            return str(uuid.UUID(value))
        except (OSError, ValueError) as exc:
            raise AppError('Nie można odczytać identyfikatora uruchomienia hosta.') from exc

    def _timeout(self, key):
        value = self.store.get_setting(key, 60)
        if type(value) is not int or not 1 <= value <= 86400:
            raise AppError('Nieprawidłowa konfiguracja limitu czasu: ' + key)
        return value

    def _event(self, run_id, action, status, row=None, began=None, error='', exit_code=None):
        data = dict(run_id=run_id, action=action, status=status,
                    duration_ms=max(0, round((self._monotonic() - began) * 1000)) if began is not None else 0,
                    error=str(error)[:2000], exit_code=exit_code)
        if row:
            data.update(position=row['position'], project=row.get('project'),
                        instance_name=row.get('instance_name'))
        self.store.add_event(**data)
        self.logger.emit(**data)

    def run(self, recover=False):
        if type(recover) is not bool:
            raise AppError('Nieprawidłowy tryb odzyskiwania.')
        with execution_lock(self.lock_path):
            if self.store.get_setting('config_state', 'ready') != 'ready':
                raise AppError('Konfiguracja LXD wymaga odzyskania przed uruchomieniem kolejki.')
            queue = self.store.load_queue()
            validate_queue(queue)
            api_timeout = self._timeout('api_timeout_seconds')
            start_timeout = self._timeout('start_timeout_seconds')
            boot_id = self._boot_id()
            if self.store.get_setting('engine_boot_id') == boot_id and not recover:
                return 'already_executed'
            with self.store.transaction():
                run_id = self.store.create_run(boot_id)
                self.store.set_setting('engine_boot_id', boot_id)
            began = self._monotonic()
            try:
                self._event(run_id, 'run', 'started', began=began)
                self.lxd.ready(api_timeout)
                for row in queue:
                    if row['kind'] == 'instance' and row['enabled'] and self.lxd.effective_autostart(row['project'], row['instance_name']) != 'false':
                        raise AppError('Natywny autostart zarządzanej instancji nie jest wyłączony: ' + row['project'] + '/' + row['instance_name'])
                result = self._run_queue(run_id, queue, start_timeout)
                self._event(run_id, 'run', result, began=began)
                self.store.finish_run(run_id, result)
                return result
            except (KeyboardInterrupt, SystemExit):
                self.store.finish_run(run_id, 'interrupted')
                self._event(run_id, 'run', 'interrupted', began=began,
                            error='Przerwano silnik; ponowienie wymaga jawnego odzyskiwania.')
                raise
            except Exception as exc:
                self.store.finish_run(run_id, 'failed')
                self._event(run_id, 'run', 'failed', began=began, error=exc)
                raise

    def _run_queue(self, run_id, queue, start_timeout):
        failures = False
        anchor = self._monotonic()
        for row in queue:
            if not row['enabled']:
                self._event(run_id, row['kind'], 'disabled', row)
                continue
            began = self._monotonic()
            if row['kind'] == 'delay':
                self._event(run_id, 'delay', 'waiting', row)
                deadline = anchor + row['delay_seconds']
                while True:
                    remaining = deadline - self._monotonic()
                    if remaining <= 0:
                        break
                    self._sleep(min(remaining, 1))
                anchor = self._monotonic()
                self._event(run_id, 'delay', 'completed', row, began)
                continue
            project, name = row['project'], row['instance_name']
            error = ''
            exit_code = None
            try:
                state = self.lxd.state(project, name)
                if state == 'STOPPED':
                    if self.lxd.has_pending_operations(project, name):
                        waiting = self._monotonic()
                        self._event(run_id, 'operations', 'waiting', row)
                        state = self.lxd.wait_for_idle(project, name, start_timeout)
                        self._event(run_id, 'operations', 'completed', row, waiting)
                    else:
                        # The instance may have changed state during the operation read.
                        state = self.lxd.state(project, name)
                if state == 'RUNNING':
                    status = 'already_running'
                elif state == 'STOPPED':
                    try:
                        self._event(run_id, 'start', 'attempting', row)
                        self.lxd.start(project, name, start_timeout)
                        status = 'started'
                        exit_code = 0
                    except LXDTimeout as exc:
                        error = str(exc)
                        self._event(run_id, 'reconcile', 'timeout', row, error=error)
                        resolution = self.lxd.reconcile_start(project, name)
                        status = {'started': 'started_after_timeout', 'failed': 'failed'}.get(resolution, 'unknown')
                    except AppError as exc:
                        # A transport failure may occur after LXD accepted the request.
                        error = str(exc)
                        exit_code = getattr(exc, 'exit_code', None)
                        self._event(run_id, 'reconcile', 'client_error', row, error=error, exit_code=exit_code)
                        resolution = self.lxd.reconcile_start(project, name)
                        status = {'started': 'started_after_error', 'failed': 'failed'}.get(resolution, 'unknown')
                else:
                    status = 'unknown'
                    error = 'LXD zgłasza niejednoznaczny stan instancji: ' + str(state)
            except AppError as exc:
                status = 'unknown'
                error = str(exc)
            # Delays count from completion, including failed attempts and reconciliation.
            anchor = self._monotonic()
            self._event(run_id, 'instance', status, row, began, error, exit_code)
            if status == 'unknown':
                return 'unknown'
            failures |= status == 'failed'
        return 'completed_with_errors' if failures else 'completed'


