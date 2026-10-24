#!/bin/bash
set -euo pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd -- "$project_root"
for script in install.sh uninstall.sh scripts/run.sh scripts/check.sh; do
    bash -n "$script"
done
exec /usr/bin/python3 -B -W error::ResourceWarning -m unittest discover -s tests "$@"
