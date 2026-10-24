```text
lxd-startup-manager [--database ŚCIEŻKA] {init,check,tui,repair,run}
```

| Polecenie | Opis |
| :--- | :--- |
| `sudo lxd-startup-manager` *(lub `tui`)* | Uruchamia interaktywny edytor kolejki w terminalu (`curses`). |
| `sudo lxd-startup-manager check` | Weryfikuje platformę, dostęp do bazy SQLite oraz gotowość lokalnego API LXD. |
| `sudo lxd-startup-manager run` | Uruchamia sekwencję startową kolejki (wywoływane przy starcie systemu przez `systemd`). Jeśli kolejka została już wykonana dla bieżącego `boot_id`, kończy się statusem `already_executed`. |
| `sudo lxd-startup-manager run --recover` | Wymusza ponowne wykonanie kolejki w ramach tego samego uruchomienia hosta (np. po wcześniejszym przerwaniu sygnałem lub błędzie). |
| `sudo lxd-startup-manager repair --confirm` | Dokańcza przerwany dwufazowy zapis konfiguracji (`pending_configuration`), wyłączając natywny `boot.autostart` dla zatwierdzonych instancji i odblokowując silnik. |
| `sudo lxd-startup-manager init` | Inicjalizuje lub migruje schemat bazy danych SQLite bez otwierania TUI ani łączenia z LXD. |

Opcja globalna:
- `--database ŚCIEŻKA` — wskazuje alternatywną ścieżkę do pliku bazy SQLite (domyślnie `/var/lib/lxd-startup-manager/state.db`).