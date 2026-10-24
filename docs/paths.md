### Ścieżki systemowe po instalacji

| Ścieżka | Przeznaczenie | 
| :--- | :--- | 
| `/usr/local/bin/lxd-startup-manager` | Skrypt uruchomieniowy CLI | 
| `/usr/local/lib/lxd-startup-manager/` | Kod źródłowy aplikacji (`src/`, `lib/`, `version.txt`) |
| `/var/lib/lxd-startup-manager/state.db` |  Baza danych SQLite | 
| `/var/lib/lxd-startup-manager/installation.json` | Manifest własności plików z sumami SHA-256  |
| `/etc/lxd-startup-manager/openobserve.json` | Opcjonalna konfiguracja i poświadczenia OpenObserve |
| `/var/log/lxd-startup-manager/events.jsonl` | Lokalne logi audytowe i diagnostyczne JSONL | 
| `/run/lxd-startup-manager/operation.lock` | Plik blokady wyłącznej (`flock`) |
| `/etc/systemd/system/lxd-startup-manager.service` | Jednostka startowa `systemd` |
