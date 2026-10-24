"""Explicit, recoverable transfer of native autostart to the queue."""
import copy

from lib.common import AppError, execution_lock, validate_name, validate_queue
from src.systemd import SystemdReset


class Controller:
    def __init__(self, store, lxd, logger, lock_path='/run/lxd-startup-manager/operation.lock', *, systemd=None):
        self.store, self.lxd, self.logger = store, lxd, logger
        self.lock_path = lock_path
        self.systemd = systemd if systemd is not None else SystemdReset()
        self._revision = store.revision
        self._preview = None

    def inventory(self):
        return self.lxd.inventory()

    def load_queue(self):
        self._revision = self.store.revision
        return self.store.load_queue()

    @staticmethod
    def _canonical(entries):
        return [{k: r.get(k, True if k == 'enabled' else 0 if k == 'delay_seconds' else None)
                 for k in ('kind', 'project', 'instance_name', 'enabled', 'delay_seconds')} for r in entries]

    def _plan(self, entries):
        validate_queue(entries)
        if self.store.get_setting('config_state') == 'reset_pending':
            raise AppError("Reset nie został ukończony. Ponów opcję Zresetuj całą konfigurację w TUI.")
        if self.store.get_setting('config_state', 'ready') != 'ready':
            raise AppError("Poprzedni zapis wymaga naprawy; po sprawdzeniu konfiguracji użyj: lxd-startup-manager repair --confirm.")
        if self.store.revision != self._revision:
            raise AppError("Konfiguracja zmieniła się w innym oknie. Otwórz edytor ponownie.")
        live = {(x['project'], x['instance_name']): x for x in self.lxd.inventory()}
        changes = []
        for row in entries:
            if row['kind'] != 'instance' or not row.get('enabled', True):
                continue
            key = (row['project'], row['instance_name'])
            if key not in live:
                raise AppError(f"Brak zarządzanej instancji w LXD: {key[0]}/{key[1]}.")
            if self.lxd.has_pending_operations(*key):
                raise AppError(f"Trwa operacja LXD dla {key[0]}/{key[1]}. Poczekaj na jej zakończenie.")
            old = self.lxd.get_autostart(*key)
            effective = self.lxd.effective_autostart(*key)
            changes.append(dict(project=key[0], instance_name=key[1], before=old,
                                effective_before=effective, applied=False))
        return changes

    def preview(self, entries):
        changes = self._plan(entries)
        self._preview = (self._canonical(entries), copy.deepcopy(changes))
        previous = self.store.load_queue()
        old_instances = {(r['project'], r['instance_name']): r
                         for r in previous if r['kind'] == 'instance'}
        new_instances = {(r['project'], r['instance_name']): r
                         for r in entries if r['kind'] == 'instance'}
        lines = []
        for c in changes:
            key = (c['project'], c['instance_name'])
            native_change = c['before'] != 'false' or c['effective_before'] != 'false'
            if not old_instances.get(key, {}).get('enabled', False) or native_change:
                native = ''
                if native_change:
                    value = c['before'] if c['before'] is not None else '(dziedziczone / ostatni stan)'
                    native = f"boot.autostart {value} → false; "
                lines.append(f"{key[0]}/{key[1]}: {native}autostart zarządzany przez lxd-startup-manager.")
        for key, row in old_instances.items():
            if row['enabled'] and not new_instances.get(key, {}).get('enabled', False):
                lines.append(f"{key[0]}/{key[1]}: wyłączono zarządzanie przez lxd-autostart-manager; natywny autostart pozostaje wyłączony.")
        lines.extend(self._layout_changes(previous, entries))
        return lines

    @staticmethod
    def _layout_changes(previous, entries):
        def instances(rows):
            return [(r['project'], r['instance_name']) for r in rows if r['kind'] == 'instance']

        def pauses(rows):
            return {((rows[i - 1]['project'], rows[i - 1]['instance_name']),
                     (rows[i + 1]['project'], rows[i + 1]['instance_name'])): r['delay_seconds']
                    for i, r in enumerate(rows) if r['kind'] == 'delay'}

        before, after = instances(previous), instances(entries)
        old_positions = {key: i + 1 for i, key in enumerate(before)}
        new_positions = {key: i + 1 for i, key in enumerate(after)}
        lines = []
        for key in before:
            if key not in new_positions:
                lines.append(f"{key[0]}/{key[1]}: usunięto wiersz z kolejki.")
        for key in after:
            if key not in old_positions:
                lines.append(f"{key[0]}/{key[1]}: dodano wiersz, pozycja instancji: {new_positions[key]}.")
        # Match only shared instances so insertion/removal does not report
        # unchanged rows as moved. Delay positions are compared by their gap.
        common_before = [key for key in before if key in new_positions]
        common_after = [key for key in after if key in old_positions]
        common_positions = {key: i for i, key in enumerate(common_before)}
        for i, key in enumerate(common_after):
            if common_positions[key] != i:
                detail = (f"pozycja instancji {old_positions[key]} → {new_positions[key]}"
                          if old_positions[key] != new_positions[key]
                          else 'zmieniono kolejność względem pozostałych instancji')
                lines.append(f"{key[0]}/{key[1]}: zmiana kolejności, {detail}.")
        old_pauses, new_pauses = pauses(previous), pauses(entries)
        for gap in dict.fromkeys([*old_pauses, *new_pauses]):
            old, new = old_pauses.get(gap), new_pauses.get(gap)
            if old == new:
                continue
            left, right = gap
            label = f"Opóźnienie {left[0]}/{left[1]} → {right[0]}/{right[1]}"
            if old is None:
                lines.append(f"{label}: dodano {new} s.")
            elif new is None:
                lines.append(f"{label}: usunięto {old} s.")
            else:
                lines.append(f"{label}: {old} s → {new} s.")
        return lines

    def save_queue(self, entries, confirmed=False):
        if confirmed is not True:
            raise AppError("Zapis wymaga świadomego potwierdzenia wyłączenia natywnego autostartu LXD.")
        with execution_lock(self.lock_path):
            changes = self._plan(entries)
            if self._preview != (self._canonical(entries), changes):
                raise AppError("Podsumowanie utraciło aktualność. Sprawdź zmiany i potwierdź zapis ponownie.")
            self.store.stage_queue(entries, changes, self._revision)
            self._apply_pending()
            self._revision = self.store.revision
            self._preview = None

    def _apply_pending(self):
        self.store.ensure_current()
        pending = self.store.get_setting('pending_configuration')
        if not isinstance(pending, dict):
            raise AppError("Brak oczekującej konfiguracji.")
        try:
            for c in pending['changes']:
                project, name = c['project'], c['instance_name']
                if self.lxd.has_pending_operations(project, name):
                    raise AppError(f"Trwa operacja LXD dla {project}/{name}.")
                actual = self.lxd.get_autostart(project, name)
                if actual != 'false':
                    if actual != c['before'] or self.lxd.effective_autostart(project, name) != c['effective_before']:
                        raise AppError(f"Konflikt: zmieniono boot.autostart {project}/{name} poza aplikacją. Sprawdź konfigurację LXD.")
                    self.lxd.set_autostart(project, name, 'false')
                if self.lxd.get_autostart(project, name) != 'false' or self.lxd.effective_autostart(project, name) != 'false':
                    raise AppError(f"LXD nie potwierdził wyłączenia autostartu {project}/{name}.")
                c['applied'] = True
                self.store.set_setting('pending_configuration', pending)
                self.logger.emit(action='configuration', status='native_autostart_disabled', project=project, instance_name=name)
            self.store.commit_staged()
            self.logger.emit(action='configuration', status='committed')
        except Exception as exc:
            self.logger.emit(action='configuration', status='pending', error=str(exc))
            raise AppError("Zapis nie został ukończony. Silnik jest zablokowany; po sprawdzeniu konfiguracji użyj: lxd-startup-manager repair --confirm. " + str(exc)) from exc

    def repair(self, confirmed=False):
        if confirmed is not True:
            raise AppError("Naprawa wymaga świadomego potwierdzenia.")
        with execution_lock(self.lock_path):
            if self.store.get_setting('config_state') == 'reset_pending':
                raise AppError("Reset nie został ukończony. Ponów opcję Zresetuj całą konfigurację w TUI.")
            if self.store.get_setting('config_state', 'ready') == 'ready':
                raise AppError("Brak konfiguracji wymagającej naprawy.")
            self._apply_pending()
            self._revision = self.store.revision
            self._preview = None

    def reset_configuration(self, confirmed=False):
        if confirmed is not True:
            raise AppError("Reset wymaga świadomego potwierdzenia.")
        with execution_lock(self.lock_path):
            self.store.ensure_current()
            if self.store.revision != self._revision:
                raise AppError("Konfiguracja zmieniła się w innym oknie. Otwórz edytor ponownie.")
            # Validate ownership before changing either LXD or the database.
            self.systemd.prepare()
            rows = self.store.load_queue()
            pending = self.store.get_setting('pending_configuration')
            if pending is not None:
                if not isinstance(pending, dict):
                    raise AppError("Nieprawidłowa oczekująca konfiguracja.")
                validate_queue(pending['queue'])
                rows += pending['queue']
                rows += [dict(kind='instance', project=c['project'],
                              instance_name=c['instance_name']) for c in pending['changes']]
            # Include disabled registered rows and partially applied saves; never
            # take ownership of unrelated instances merely discovered by the UI.
            targets = {(r['project'], r['instance_name']) for r in rows if r['kind'] == 'instance'}
            live = {(r['project'], r['instance_name']) for r in self.lxd.inventory()}
            targets &= live  # Deleted instances have no native autostart to disable.
            changes = []
            for project, name in sorted(targets):
                validate_name(project)
                validate_name(name)
                if self.lxd.has_pending_operations(project, name):
                    raise AppError(f"Trwa operacja LXD dla {project}/{name}. Poczekaj na jej zakończenie.")
                changes.append((project, name, self.lxd.get_autostart(project, name),
                                self.lxd.effective_autostart(project, name)))
            with self.store.transaction():
                self.store.set_setting('config_state', 'reset_pending')
            self._preview = None
            try:
                for project, name, before, effective in changes:
                    if self.lxd.has_pending_operations(project, name):
                        raise AppError(f"Trwa operacja LXD dla {project}/{name}.")
                    actual = self.lxd.get_autostart(project, name)
                    expanded = self.lxd.effective_autostart(project, name)
                    if (actual, expanded) != (before, effective):
                        raise AppError(f"Konflikt ustawień autostartu {project}/{name}. Ponów reset po sprawdzeniu LXD.")
                    if actual != 'false':
                        self.lxd.set_autostart(project, name, 'false')
                    if (self.lxd.get_autostart(project, name) != 'false'
                            or self.lxd.effective_autostart(project, name) != 'false'):
                        raise AppError(f"LXD nie potwierdził wyłączenia autostartu {project}/{name}.")
                    self.logger.emit(action='reset', status='native_autostart_disabled',
                                     project=project, instance_name=name)
                self.systemd.remove()
                # Logging can fail (for example, a full filesystem). Keep all
                # fallible external work before the final database commit so
                # the UI never retains an old draft after a successful reset.
                self.logger.emit(action='reset', status='systemd_removed')
                self._revision = self.store.reset_configuration(self._revision)
            except Exception as exc:
                raise AppError("Reset nie został ukończony; część zmian mogła zostać zastosowana. "
                               "Ponów opcję Zresetuj całą konfigurację. " + str(exc)) from exc

    def history(self):
        return self.store.runs()

    def events(self, run_id):
        return self.store.events(run_id)

    def get_settings(self):
        return {
            'api_timeout_seconds': self.store.get_setting('api_timeout_seconds', 60),
            'start_timeout_seconds': self.store.get_setting('start_timeout_seconds', 60),
            'default_delay_seconds': self.store.get_setting('default_delay_seconds', 30),
        }

    def save_settings(self, settings):
        timeout_keys = {'api_timeout_seconds', 'start_timeout_seconds'}
        setting_keys = timeout_keys | {'default_delay_seconds'}
        if not isinstance(settings, dict) or set(settings) not in (timeout_keys, setting_keys):
            raise AppError("Ustawienia muszą zawierać oba timeouty i opcjonalnie domyślne opóźnienie.")
        if any(type(settings[k]) is not int or not 1 <= settings[k] <= 86400 for k in timeout_keys):
            raise AppError("Timeouty muszą być liczbami całkowitymi od 1 do 86400 sekund.")
        if 'default_delay_seconds' in settings and (
                type(settings['default_delay_seconds']) is not int
                or not 0 <= settings['default_delay_seconds'] <= 2**63 - 1):
            raise AppError("Domyślne opóźnienie musi być liczbą całkowitą od 0 do 2**63-1 sekund.")
        with execution_lock(self.lock_path), self.store.transaction():
            if self.store.revision != self._revision:
                raise AppError("Konfiguracja zmieniła się w innym oknie. Otwórz edytor ponownie.")
            if self.store.get_setting('config_state', 'ready') != 'ready':
                raise AppError("Najpierw dokończ oczekujący zapis lub reset konfiguracji.")
            for k, v in settings.items():
                self.store.set_setting(k, v)
