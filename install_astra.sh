#!/usr/bin/env bash
set -euo pipefail

missing=()

if ! command -v python3 >/dev/null 2>&1; then
  missing+=("python3")
fi

if ! python3 - <<'PY' >/dev/null 2>&1
import tkinter
PY
then
  missing+=("python3-tk")
fi

if ! command -v borg >/dev/null 2>&1; then
  missing+=("borgbackup")
fi

chmod +x linux-backup-manager

if ((${#missing[@]} > 0)); then
  echo "Нужно установить пакеты: ${missing[*]}"
  echo "Команда для Astra/Debian:"
  echo "  sudo apt update && sudo apt install ${missing[*]}"
  exit 1
fi

echo "Готово. Запуск:"
echo "  ./linux-backup-manager"

