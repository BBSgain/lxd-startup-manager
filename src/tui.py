"""Curses interface for editing the LXD startup queue.

The interface intentionally has no operation that starts or stops an instance.
"""
from __future__ import annotations

import copy
import curses
from datetime import datetime
from pathlib import Path
import textwrap
from typing import Any


QUEUE_SAVED = "Zmiany zostały zapisane. Kolejka utworzona"
CONFIGURATION_RESET = "Wszystkie ustawienia zostały skasowane, autostartem nie zarządza lxd-startup-manager."


def _version() -> str:
    try:
        value = (Path(__file__).resolve().parents[1] / "version.txt").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return "nieznana"
    return value if value and all(character.isprintable() for character in value) else "nieznana"


def identity(entry: dict[str, Any]) -> tuple[str, str]:
    return str(entry.get("project", "")), str(entry.get("instance_name", ""))


def merge_inventory(inventory: list[dict[str, Any]], queue: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build an editable draft; newly discovered instances stay disabled."""
    result = copy.deepcopy(queue)
    known = {identity(row) for row in result if row.get("kind") == "instance"}
    for item in inventory:
        key = identity(item)
        if key not in known:
            result.append({"id": None, "position": len(result), "kind": "instance", "project": key[0],
                           "instance_name": key[1], "enabled": False, "delay_seconds": 0})
            known.add(key)
    for i, row in enumerate(result):
        row["position"] = i
    return result


def queue_error(rows: list[dict[str, Any]]) -> str | None:
    for i, row in enumerate(rows):
        if row.get("kind") == "delay":
            if i == 0 or i == len(rows) - 1:
                return "Opóźnienie nie może być pierwszym ani ostatnim wierszem."
            if rows[i - 1].get("kind") == "delay" or rows[i + 1].get("kind") == "delay":
                return "Dwa wiersze opóźnienia nie mogą sąsiadować."
            value = row.get("delay_seconds")
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return "Czas opóźnienia musi być nieujemną liczbą całkowitą sekund."
        elif row.get("kind") != "instance":
            return "Nieznany typ wiersza kolejki."
    return None


def move_row(rows: list[dict[str, Any]], index: int, delta: int) -> tuple[list[dict[str, Any]], str | None]:
    target = index + delta
    if not 0 <= index < len(rows) or not 0 <= target < len(rows):
        return rows, "Nie można przesunąć wiersza poza kolejkę."
    candidate = copy.deepcopy(rows)
    candidate[index], candidate[target] = candidate[target], candidate[index]
    error = queue_error(candidate)
    if error:
        return rows, error
    for position, row in enumerate(candidate):
        row["position"] = position
    return candidate, None


def moved_selection(index: int, delta: int, row_count: int) -> int:
    """Keep the same row selected after swapping it with a neighbor."""
    return min(max(0, index + delta), max(0, row_count - 1))


def _wrap_lines(lines: list[str], width: int) -> list[str]:
    result: list[str] = []
    for line in lines:
        result.extend(textwrap.wrap(str(line), width=max(1, width), replace_whitespace=False,
                                    drop_whitespace=False, break_long_words=True,
                                    break_on_hyphens=False) or [""])
    return result


def _history_time(value: Any) -> str:
    if value is None:
        return "?"
    raw = str(value)
    try:
        timestamp = datetime.fromisoformat(raw)
        if timestamp.tzinfo is None:
            return raw
        local = timestamp.astimezone()
    except (ValueError, OverflowError, OSError):
        return raw
    return f"{local.isoformat(sep=' ', timespec='seconds')} {local.tzname()}"


def history_lines(records: list[dict[str, Any]], events: list[dict[str, Any]]) -> list[str]:
    lines = [f"Uruchomienie {row.get('id', row.get('run_id', '?'))}: {_history_time(row.get('started_at'))} | {row.get('status', '?')}"
             for row in records]
    if events:
        lines.append("— Zdarzenia —")
        for event in events:
            details = [str(event.get("action") or "?")]
            for field in ("status",):
                if event.get(field) is not None: details.append(str(event[field]))
            if event.get("project") or event.get("instance_name"):
                details.append(f"{event.get('project', '')}/{event.get('instance_name', '')}".strip("/"))
            if event.get("duration_ms") is not None: details.append(f"{event['duration_ms']} ms")
            if event.get("error"): details.append(f"błąd: {event['error']}")
            lines.append(f"{_history_time(event.get('timestamp'))} | " + " | ".join(details))
    return lines or ["Brak zapisanych uruchomień."]


def _safe_add(win: Any, y: int, x: int, text: str, width: int, attr: int = 0) -> None:
    if width <= 0:
        return
    try:
        win.addnstr(y, x, text, width, attr)
    except (curses.error, UnicodeError):
        pass


def _decimal_value(raw: str) -> int | None:
    if not raw or any(character < "0" or character > "9" for character in raw):
        return None
    try: return int(raw, 10)
    except ValueError: return None


def _menu_entries() -> list[str]:
    """Describe the actions and shortcuts implemented by the main loop."""
    return [
        "↑↓ / k j   Wybór wiersza",
        "+ / = / -  Zmień kolejność",
        "Enter      Dodaj / edytuj pauzę",
        "Del        Usuń pauzę",
        "Spacja     Zarządzaj autostartem",
        "i          Szczegóły instancji",
        "F2         Zapisz kolejkę",
        "h          Historia",
        "t          Ustawienia",
        "r          Zresetuj całą konfigurację",
        "q / Esc    Wyjście",
    ]


def _compact_menu_lines(width: int) -> list[str]:
    """Short footer menu for terminals that cannot fit a right-hand panel."""
    reset_exit = "h historia  t ustawienia  r Zresetuj całą konfigurację  q/Esc wyjście"
    if len(reset_exit) > width:
        reset_exit = "h hist  t ust.  r reset  q/Esc wyj"
    return _wrap_lines([
        "↑↓/k/j wybór  +/=/- kolejność",
        "Enter pauza dodaj/edytuj  Del usuń",
        "Spacja autostart  i info  F2 zapis",
        reset_exit,
    ], width)


def _draw(stdscr: Any, rows: list[dict[str, Any]], inventory: list[dict[str, Any]], selected: int,
          message: str, dirty: bool, top: int = 0, action_index: int = 0) -> int:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    if height < 7 or width < 40:
        _safe_add(stdscr, 0, 0, "Terminal za mały (min. 40x7). Zwiększ okno.", width - 1)
        stdscr.refresh()
        return top
    version = ("Wersja: " + _version())[:width - 1]
    version_x = width - 1 - len(version)
    _safe_add(stdscr, 0, 0, "LXD Startup Manager — kolejka autostartu", version_x - 1, curses.A_BOLD)
    _safe_add(stdscr, 0, version_x, version, len(version), curses.A_BOLD)
    sidebar = width >= 76 and height >= 15
    compact = height <= 12 and not sidebar
    status = "* niezapisane zmiany" if dirty else "Zmiany zapisane"
    if (not compact or not message) and message != QUEUE_SAVED:
        _safe_add(stdscr, 1, 0, status, width - 1, curses.A_STANDOUT if dirty else 0)
    if sidebar:
        menu_width = max(len(entry) for entry in _menu_entries())
        menu_x = width - menu_width - 1
        list_width = menu_x - 1
        message_lines = textwrap.wrap(message, width=max(1, list_width)) if message else []
        max_message_lines = height - 4  # Keep the selected queue row visible.
        if len(message_lines) > max_message_lines:
            message_lines = message_lines[:max_message_lines]
            message_lines[-1] = message_lines[-1][:list_width - 1] + "…"
        for offset, line in enumerate(message_lines):
            _safe_add(stdscr, 2 + offset, 0, line, list_width, curses.A_BOLD)
        list_start = 3 + len(message_lines)
        _safe_add(stdscr, 2, menu_x, "Menu główne", menu_width, curses.A_BOLD)
        menu = _menu_entries()
        menu_top = 3
        available = max(0, height - list_start)
    else:
        menu_width = width - 1
        menu_x = 0
        list_width = width - 1
        list_start = 2 if compact else 3
        menu = _compact_menu_lines(max(1, menu_width))
        menu_top = max(list_start + 1, height - len(menu))
        if compact:
            # At 40x7, preserve one queue row and use the status line for messages.
            if message:
                _safe_add(stdscr, 1, 0, message, width - 1, curses.A_BOLD)
        else:
            if message:
                _safe_add(stdscr, 2, 0, message, width - 1, curses.A_BOLD)
        available = max(0, menu_top - list_start)
    if selected < top: top = selected
    if available and selected >= top + available: top = selected - available + 1
    elif not available: top = selected
    live = {identity(x): x for x in inventory}
    if not rows and available:
        _safe_add(stdscr, list_start, 0, "Brak dostępnych serwerów", list_width, curses.A_BOLD)
    for y, idx in enumerate(range(top, min(len(rows), top + available)), start=list_start):
        row = rows[idx]
        if row.get("kind") == "delay":
            line = f"    Opóźnienie: {row.get('delay_seconds', 0)} s"
        else:
            inst = live.get(identity(row))
            typ = str((inst or {}).get("type", "?"))
            typ = "VM" if typ.lower() in ("vm", "virtual-machine") else "CONTAINER" if typ.lower() in ("container", "container-instance") else typ
            missing = "  ⚠ BRAK W LXD" if inst is None else ""
            line = f" {row.get('instance_name')} [{row.get('project')}] {typ} | autostart: {'WŁĄCZONY' if row.get('enabled') else 'wyłączony'}{missing}"
        _safe_add(stdscr, y, 0, line, list_width, curses.A_REVERSE if idx == selected else 0)
    if sidebar:
        for offset, line in enumerate(menu):
            _safe_add(stdscr, menu_top + offset, menu_x, line, menu_width)
    else:
        for offset, line in enumerate(menu):
            _safe_add(stdscr, menu_top + offset, 0, line, menu_width,
                      curses.A_BOLD if offset == 0 else 0)
    stdscr.refresh()
    return top


def _modal(stdscr: Any, title: str, fields: list[dict[str, Any]], buttons: list[str],
           message: str = "", initial_focus: int = 0,
           description: list[str] | None = None) -> tuple[str, list[str]] | None:
    """Small curses form: fields and buttons are focusable with Tab/↑/↓."""
    values = [str(field["value"]) for field in fields]
    focus = min(max(0, initial_focus), max(0, len(fields) + len(buttons) - 1))
    editing: set[int] = set()
    while True:
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        if h < 5 or w < 20:
            _safe_add(stdscr, 0, 0, "Powiększ terminal, aby kontynuować", max(0, w - 1))
            stdscr.refresh()
            try: key = stdscr.get_wch()
            except (curses.error, AttributeError): return None
            if key in ("\x1b", "q", "Q"): return None
            continue
        # Keep a visible frame while clipping every write to the current size.
        box_w = min(w, 70)
        left = max(0, (w - box_w) // 2)
        right = left + box_w - 1
        _safe_add(stdscr, 0, left, f"┌{'─' * max(0, box_w - 2)}┐", box_w, curses.A_BOLD)
        _safe_add(stdscr, 1, left + 2, title, max(1, box_w - 4), curses.A_BOLD)
        for border_y in range(1, h - 1):
            _safe_add(stdscr, border_y, left, "│", 1, curses.A_BOLD)
            _safe_add(stdscr, border_y, right, "│", 1, curses.A_BOLD)
        _safe_add(stdscr, h - 1, left, f"└{'─' * max(0, box_w - 2)}┘", box_w, curses.A_BOLD)
        inner = max(1, box_w - 4)
        description_lines = _wrap_lines(description or [], inner)
        compact_fields = (2 * len(fields) + len(description_lines)) > max(0, h - 6)
        y = 2 if compact_fields else 3
        button_y = h - 2 if compact_fields else h - 3
        for line in description_lines:
            if y >= button_y: break
            _safe_add(stdscr, y, left + 2, str(line), inner)
            y += 1
        for i, field in enumerate(fields):
            if y >= button_y: break
            label = str(field["label"])
            marker = "› " if focus == i else "  "
            if compact_fields:
                shown = f"{marker}{label}: [{values[i]}]"
                if focus == i: shown += " * pisanie zastępuje"
                _safe_add(stdscr, y, left + 2, shown, inner, curses.A_STANDOUT if focus == i else 0)
                y += 1
            else:
                _safe_add(stdscr, y, left + 2, marker + label, inner, curses.A_STANDOUT if focus == i else 0)
                y += 1
                if y >= button_y: break
                hint = " (pisanie zastępuje wartość; Backspace usuwa)" if focus == i else ""
                _safe_add(stdscr, y, left + 2, f"[{values[i]}]" + hint, inner, curses.A_STANDOUT if focus == i else 0)
                y += 1
        button_y = max(2, button_y)
        x = left + 2
        button_indices = range(len(fields), len(fields) + len(buttons))
        for j, label in zip(button_indices, buttons):
            shown = f"<{label}>" if focus == j else f" {label} "
            if x < right:
                _safe_add(stdscr, button_y, x, shown, max(0, right - x),
                          curses.A_STANDOUT if focus == j else curses.A_BOLD)
            x += len(shown) + 2
        if not compact_fields:
            footer = message or "Tab/↑↓ fokus  Enter wybierz  Esc anuluj"
            _safe_add(stdscr, h - 2, left + 2, footer, inner, curses.A_BOLD if message else 0)
        stdscr.refresh()
        try: key = stdscr.get_wch()
        except (curses.error, AttributeError): return None
        total = len(fields) + len(buttons)
        if key in ("\x1b",): return None
        if key in ("\t", curses.KEY_DOWN, curses.KEY_UP):
            step = -1 if key == curses.KEY_UP else 1
            focus = (focus + step) % max(1, total)
            continue
        if key in (curses.KEY_LEFT, curses.KEY_RIGHT) and focus >= len(fields):
            focus = (focus - 1 if key == curses.KEY_LEFT else focus + 1) % max(1, total)
            continue
        if key in ("\n", "\r", curses.KEY_ENTER, 10, 13):
            if focus >= len(fields): return buttons[focus - len(fields)], values
            continue
        if focus < len(fields):
            if key in (curses.KEY_BACKSPACE, "\b", "\x7f", 127):
                values[focus] = values[focus][:-1]
                editing.add(focus)
            elif isinstance(key, str) and len(key) == 1 and key.isprintable():
                if focus not in editing: values[focus] = ""
                values[focus] += key
                editing.add(focus)


def _confirm(stdscr: Any, text: str, warning: str = "") -> bool:
    description = [text]
    if warning: description.append(warning)
    result = _modal(stdscr, "Potwierdzenie", [], ["Tak", "Nie"],
                    initial_focus=1, description=description)
    return result is not None and result[0] == "Tak"


def _confirm_delay(stdscr: Any, seconds: int) -> bool:
    return seconds <= 300 or _confirm(
        stdscr, "Twoje opóźnienie jest dłuższe niż 300 sekund. Jesteś pewien?")


def _delay_dialog(stdscr: Any, current: int) -> tuple[str, int | None] | None:
    raw = str(current)
    while True:
        result = _modal(stdscr, "Opóźnienie", [{"label": "Czas w sekundach", "value": raw}],
                        ["Zapisz", "Usuń", "Anuluj"])
        if result is None or result[0] == "Anuluj": return None
        if result[0] == "Usuń": return "delete", None
        raw = result[1][0]
        value = _decimal_value(raw)
        if value is not None and 1 <= value <= 86400:
            if _confirm_delay(stdscr, value):
                return "save", value
            continue
        modal = _modal(stdscr, "Nieprawidłowa wartość", [], ["Wróć"],
                       description=["Podaj liczbę całkowitą od 1 do 86400 sekund"])
        if modal is None: return None


def _settings_dialog(stdscr: Any, settings: dict[str, Any]) -> dict[str, Any] | None:
    names = ("default_delay_seconds", "api_timeout_seconds", "start_timeout_seconds")
    labels = ("Domyślna pauza (s)", "Timeout API LXD (s)", "Timeout startu (s)")
    defaults = (30, 60, 60)
    fields = [{"label": label, "value": str(settings.get(name, default))}
              for name, label, default in zip(names, labels, defaults)]
    while True:
        result = _modal(stdscr, "Ustawienia", fields, ["Zapisz", "Anuluj"])
        if result is None or result[0] != "Zapisz": return None
        for field, raw in zip(fields, result[1]):
            field["value"] = raw
        values: dict[str, int] = {}
        error = None
        for name, raw in zip(names, result[1]):
            number = _decimal_value(raw)
            if number is None: number = -1
            low, high = (0, 2**63 - 1) if name == "default_delay_seconds" else (1, 86400)
            if not low <= number <= high:
                error = ("Domyślna pauza musi być liczbą całkowitą od 0 do 2^63−1."
                         if name == "default_delay_seconds" else "Timeout musi być liczbą całkowitą od 1 do 86400.")
                break
            values[name] = number
        if error is None:
            if not _confirm_delay(stdscr, values["default_delay_seconds"]):
                continue
            result_settings = copy.deepcopy(settings)
            result_settings.update(values)
            return result_settings
        modal = _modal(stdscr, "Nieprawidłowe ustawienia", [], ["Wróć"], description=[error])
        if modal is None: return None


def _show(stdscr: Any, title: str, lines: list[str]) -> Any:
    offset = 0
    while True:
        h, w = stdscr.getmaxyx()
        page_size = max(1, h - 3)
        wrapped = _wrap_lines(lines, max(1, w - 2))
        offset = min(offset, max(0, len(wrapped) - page_size))
        stdscr.erase()
        _safe_add(stdscr, 0, 0, title, w - 1, curses.A_BOLD)
        for y, line in enumerate(wrapped[offset:offset + page_size], 2): _safe_add(stdscr, y, 0, line, w - 1)
        footer = "↑/↓ przewijaj  Enter/Esc powrót" if len(wrapped) > page_size else "Enter/Esc powrót"
        _safe_add(stdscr, h - 1, 0, footer, w - 1)
        stdscr.refresh()
        try: key = stdscr.get_wch()
        except (curses.error, AttributeError): return None
        if key in (curses.KEY_UP, "k"): offset = max(0, offset - 1)
        elif key in (curses.KEY_DOWN, "j"): offset = min(max(0, len(wrapped) - page_size), offset + 1)
        elif key == curses.KEY_PPAGE: offset = max(0, offset - page_size)
        elif key == curses.KEY_NPAGE: offset = min(max(0, len(wrapped) - page_size), offset + page_size)
        elif key in (curses.KEY_HOME, "g"): offset = 0
        elif key in (curses.KEY_END, "G"): offset = max(0, len(wrapped) - page_size)
        else: return key


def _main(stdscr: Any, controller: Any) -> None:
    # Bound ncurses' wait for the rest of a function-key escape sequence.
    curses.set_escdelay(100)
    try: curses.curs_set(0)
    except curses.error: pass
    stdscr.keypad(True)
    original = copy.deepcopy(controller.load_queue())
    try:
        inventory = controller.inventory()
        inventory_available = True
        startup_message = ""
    except Exception as exc:
        inventory = []
        inventory_available = False
        startup_message = f"Inwentarz LXD niedostępny: {exc}. Kolejka i historia dostępne; unikaj zapisu."
    draft = merge_inventory(inventory, original)
    selected, top = 0, 0
    dirty = draft != original
    message = startup_message or ("Nowe instancje dopisano jako niezarządzane; autostart menedżera wyłączony." if dirty else "")
    while True:
        top = _draw(stdscr, draft, inventory, selected, message, dirty, top)
        message = ""
        try: key = stdscr.get_wch()
        except curses.error: continue
        if key in ("q", "Q", "\x1b"):
            warning = "Niezapisane zmiany zostaną odrzucone." if dirty else ""
            if not _confirm(stdscr, "Wyjść z aplikacji?", warning): message = "Pozostajesz w edytorze."; continue
            return
        if key in (curses.KEY_UP, "k"):
            selected = moved_selection(selected, -1, len(draft))
        elif key in (curses.KEY_DOWN, "j"):
            selected = moved_selection(selected, 1, len(draft))
        elif key in ("+", "="):
            draft, message = move_row(draft, selected, -1)
            if not message: selected = moved_selection(selected, -1, len(draft))
            dirty = draft != original
        elif key == "-":
            draft, message = move_row(draft, selected, 1)
            if not message: selected = moved_selection(selected, 1, len(draft))
            dirty = draft != original
        elif key == curses.KEY_F2:
            if not inventory_available:
                message = "Zapis zablokowany: nie udało się odczytać inwentarza LXD."
                continue
            error = queue_error(draft)
            if error: message = error; continue
            try: changes = controller.preview(copy.deepcopy(draft))
            except Exception as exc: message = f"Nie można przygotować podsumowania: {exc}"; continue
            _show(stdscr, "Zapis kolejki - podsumowanie zmian", list(changes) or ["Brak zmian w podsumowaniu."])
            if _confirm(stdscr, "Zapisać tę konfigurację?"):
                try:
                    controller.save_queue(copy.deepcopy(draft), confirmed=True)
                    original = copy.deepcopy(draft); dirty = False; message = QUEUE_SAVED
                except Exception as exc: message = f"Zapis nieudany: {exc}"
        elif key in ("h", "H"):
            try:
                records = controller.history()
                events = []
                if records:
                    run_id = records[0].get("id", records[0].get("run_id"))
                    if run_id is not None:
                        events = controller.events(run_id)
                _show(stdscr, "Historia uruchamiania", history_lines(records, events))
            except Exception as exc: message = f"Nie można wczytać historii: {exc}"
        elif key in ("t", "T"):
            try:
                settings = controller.get_settings()
                edited = _settings_dialog(stdscr, settings)
                if edited is not None:
                    controller.save_settings(edited); message = "Ustawienia zapisane."
            except Exception as exc: message = f"Nie można zapisać ustawień: {exc}"
        elif key in ("r", "R"):
            if not _confirm(stdscr, "Wszystkie ustawienia zostaną skasowane. Jesteś pewien?"):
                message = "Reset konfiguracji anulowany."
                continue
            try:
                controller.reset_configuration(confirmed=True)
            except Exception as exc:
                message = f"Reset konfiguracji nieudany: {exc}"
                continue
            # The controller has reset persistent state. Drop the old draft now
            # so a later read error cannot leave stale unsaved edits available.
            draft, original = [], []
            selected, top, dirty = 0, 0, False
            try:
                reset_queue = copy.deepcopy(controller.load_queue())
                try:
                    reset_inventory = controller.inventory()
                    inventory_available = True
                except Exception as exc:
                    reset_inventory = inventory
                    inventory_available = False
                    message = f"Konfiguracja zresetowana, ale nie można odświeżyć inwentarza: {exc}"
                reset_draft = merge_inventory(reset_inventory, reset_queue)
                for row in reset_draft:
                    if row.get("kind") == "instance":
                        row["enabled"] = False
                draft = reset_draft
                original = copy.deepcopy(draft)
                inventory = reset_inventory
                if inventory_available:
                    message = "Konfiguracja zresetowana."
                    _show(stdscr, "Konfiguracja zresetowana", [CONFIGURATION_RESET])
            except Exception as exc:
                message = f"Konfiguracja zresetowana, ale nie można wczytać kolejki: {exc}"
        elif key in (" ",) and draft and draft[selected].get("kind") == "instance":
            draft[selected]["enabled"] = not bool(draft[selected].get("enabled")); dirty = draft != original
        elif key in ("i", "I") and draft and draft[selected].get("kind") == "instance":
            row = draft[selected]
            item = next((x for x in inventory if identity(x) == identity(row)), {})
            _show(stdscr, "Szczegóły instancji", [f"Nazwa: {row.get('instance_name')}", f"Projekt: {row.get('project')}",
                f"Typ: {item.get('type', 'brak w LXD')}", f"Stan: {item.get('status', 'nieznany')}",
                f"Autostart menedżera: {'włączony' if row.get('enabled') else 'wyłączony'}"])
        elif key in ("\n", "\r", curses.KEY_ENTER, 10, 13) and draft:
            row = draft[selected]
            if row.get("kind") == "delay":
                result = _delay_dialog(stdscr, row.get("delay_seconds", 0))
                if result and result[0] == "save": row["delay_seconds"] = result[1]; dirty = draft != original
                elif result and result[0] == "delete":
                    if _confirm(stdscr, "Usunąć zaznaczone opóźnienie?"):
                        del draft[selected]; selected = min(selected, max(0, len(draft) - 1))
                        for n, item in enumerate(draft): item["position"] = n
                        dirty = draft != original
            elif selected < len(draft):
                candidate = copy.deepcopy(draft); i = selected + 1
                try: settings = controller.get_settings()
                except Exception as exc:
                    message = f"Nie można odczytać domyślnego opóźnienia: {exc}"
                    continue
                if not isinstance(settings, dict):
                    message = "Nieprawidłowe ustawienia; pauza nie została dodana."
                    continue
                default_delay = settings.get("default_delay_seconds", 30)
                if (isinstance(default_delay, bool) or not isinstance(default_delay, int)
                        or not 0 <= default_delay <= 2**63 - 1):
                    message = "Nieprawidłowa wartość default_delay_seconds; pauza nie została dodana."
                    continue
                candidate.insert(i, {"id": None, "position": i, "kind": "delay", "project": None,
                                     "instance_name": None, "enabled": True, "delay_seconds": default_delay})
                for n, item in enumerate(candidate): item["position"] = n
                error = queue_error(candidate)
                if error: message = error
                elif _confirm_delay(stdscr, default_delay):
                    draft = candidate; selected = i; dirty = draft != original
        elif key in (curses.KEY_DC, 127, "\x7f") and draft and draft[selected].get("kind") == "delay":
            if _confirm(stdscr, "Usunąć zaznaczone opóźnienie?"):
                del draft[selected]; selected = min(selected, max(0, len(draft) - 1))
                for i, row in enumerate(draft): row["position"] = i
                dirty = draft != original


def run(controller: Any) -> None:
    """Start the curses UI. Initial inventory and queue reads are read-only."""
    curses.wrapper(_main, controller)
