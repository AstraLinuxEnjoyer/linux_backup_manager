#!/usr/bin/env python3
"""Linux Backup Manager GUI for Astra Linux."""

from __future__ import annotations

import datetime as _dt
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


APP_NAME = "Linux Backup Manager"
COMPRESSION_PRESETS = {
    "Максимальное сжатие (очень медленно)": "lzma,9",
    "Сильное сжатие (быстрее)": "zstd,22",
    "Баланс скорости и размера": "zstd,15",
    "Быстрое сжатие": "zlib,9",
    "Без сжатия": "none",
}
BORG_PROGRESS_RE = re.compile(
    r"^\s*(?P<original>\S+\s+\S+)\s+O\s+"
    r"(?P<compressed>\S+\s+\S+)\s+C\s+"
    r"(?P<deduplicated>\S+\s+\S+)\s+D\s+"
    r"(?P<files>\d+)\s+N\s+(?P<path>.*)$"
)
DEFAULT_EXCLUDES = [
    "/proc",
    "/sys",
    "/dev",
    "/run",
    "/tmp",
    "/mnt",
    "/media",
    "/lost+found",
]


def format_bytes(size: int) -> str:
    units = ("Б", "КБ", "МБ", "ГБ", "ТБ")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ТБ"


class CommandRunner:
    def __init__(self, log_callback, done_callback):
        self.log_callback = log_callback
        self.done_callback = done_callback
        self.process: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()

    def run(self, command: list[str], cwd: str | None = None) -> None:
        if self.running():
            raise RuntimeError("Команда уже выполняется")

        def worker() -> None:
            code = -1
            try:
                self.log_callback("$ " + " ".join(command))
                self.process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                assert self.process.stdout is not None
                for line in self.process.stdout:
                    self.log_callback(line.rstrip())
                code = self.process.wait()
            except FileNotFoundError as exc:
                self.log_callback(f"Ошибка: команда не найдена: {exc.filename}")
            except Exception as exc:  # noqa: BLE001 - GUI must show operational errors.
                self.log_callback(f"Ошибка: {exc}")
            finally:
                self.done_callback(code)

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()


class BackupManagerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1080x720")
        self.minsize(900, 620)

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.sources: list[str] = []
        self.runner = CommandRunner(self.enqueue_log, self.command_finished)

        self.repo_var = tk.StringVar()
        self.passphrase_var = tk.StringVar()
        self.archive_var = tk.StringVar()
        self.restore_target_var = tk.StringVar(value="/")
        self.compression_var = tk.StringVar(value="Максимальное сжатие (очень медленно)")
        self.restore_metadata_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Готово")
        self.elapsed_var = tk.StringVar(value="")
        self.current_operation = ""
        self.current_operation_target = ""
        self.operation_started_at: float | None = None

        self._build_ui()
        self._refresh_dependency_state()
        self.after(100, self.flush_log)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        root = ttk.Frame(self, padding=12)
        root.grid(row=0, column=0, sticky="nsew")
        root.columnconfigure(0, weight=0)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(1, weight=1)

        settings = ttk.LabelFrame(root, text="Настройки", padding=10)
        settings.grid(row=0, column=0, columnspan=2, sticky="ew")
        settings.columnconfigure(1, weight=1)

        ttk.Label(settings, text="Хранилище копий").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(settings, textvariable=self.repo_var).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(settings, text="Выбрать", command=self.choose_repo).grid(row=0, column=2, padx=(8, 0), pady=4)

        ttk.Label(settings, text="Пароль").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(settings, textvariable=self.passphrase_var, show="*").grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(settings, text="Подготовить хранилище", command=self.init_repo).grid(row=1, column=2, padx=(8, 0), pady=4)

        ttk.Label(settings, text="Сжатие").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        compression = ttk.Combobox(
            settings,
            textvariable=self.compression_var,
            values=tuple(COMPRESSION_PRESETS.keys()),
            state="readonly",
            width=34,
        )
        compression.grid(row=2, column=1, sticky="w", pady=4)
        ttk.Button(settings, text="Обновить список архивов", command=self.list_archives).grid(
            row=2, column=2, padx=(8, 0), pady=4
        )

        left = ttk.Frame(root)
        left.grid(row=1, column=0, sticky="nsw", pady=(10, 0), padx=(0, 10))
        left.rowconfigure(0, weight=1)

        sources_box = ttk.LabelFrame(left, text="Дополнительные каталоги", padding=10)
        sources_box.grid(row=0, column=0, sticky="nsew")
        sources_box.rowconfigure(0, weight=1)
        sources_box.columnconfigure(0, weight=1)

        self.sources_list = tk.Listbox(sources_box, height=12, width=38, selectmode=tk.EXTENDED)
        self.sources_list.grid(row=0, column=0, columnspan=3, sticky="nsew")
        ttk.Button(sources_box, text="Добавить", command=self.add_source).grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(sources_box, text="Удалить", command=self.remove_source).grid(row=1, column=1, sticky="ew", padx=6, pady=(8, 0))
        ttk.Button(sources_box, text="Добавить /", command=self.add_root_source).grid(row=1, column=2, sticky="ew", pady=(8, 0))

        actions = ttk.LabelFrame(left, text="Основные действия", padding=10)
        actions.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)

        ttk.Button(actions, text="Полная копия системы", command=self.create_system_backup).grid(row=0, column=0, sticky="ew", pady=3)
        ttk.Button(actions, text="Проверить целостность", command=self.check_repo).grid(row=0, column=1, sticky="ew", padx=(8, 0), pady=3)
        ttk.Button(actions, text="Копия выбранных каталогов", command=self.create_backup).grid(row=1, column=0, sticky="ew", pady=3)
        ttk.Button(actions, text="Остановить", command=self.stop_command).grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=3)

        restore_box = ttk.LabelFrame(left, text="Восстановление", padding=10)
        restore_box.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        restore_box.columnconfigure(1, weight=1)

        ttk.Label(restore_box, text="Архив").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        self.archive_combo = ttk.Combobox(restore_box, textvariable=self.archive_var, state="normal")
        self.archive_combo.grid(row=0, column=1, sticky="ew", pady=4)

        ttk.Label(restore_box, text="Куда").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(restore_box, textvariable=self.restore_target_var).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(restore_box, text="Выбрать", command=self.choose_restore_target).grid(row=1, column=2, padx=(8, 0), pady=4)
        ttk.Button(restore_box, text="Восстановить выбранную копию", command=self.restore_archive).grid(
            row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0)
        )
        ttk.Checkbutton(
            restore_box,
            text="Восстанавливать ACL/xattrs/метки безопасности",
            variable=self.restore_metadata_var,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

        right = ttk.Frame(root)
        right.grid(row=1, column=1, sticky="nsew", pady=(10, 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)

        self.deps_label = ttk.Label(right, text="")
        self.deps_label.grid(row=0, column=0, sticky="ew")

        archives_box = ttk.LabelFrame(right, text="Менеджер архивов", padding=10)
        archives_box.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        archives_box.columnconfigure(0, weight=1)

        columns = ("name", "time", "original", "compressed", "deduplicated")
        self.archives_tree = ttk.Treeview(archives_box, columns=columns, show="headings", height=7)
        self.archives_tree.heading("name", text="Архив")
        self.archives_tree.heading("time", text="Создан")
        self.archives_tree.heading("original", text="Исходно")
        self.archives_tree.heading("compressed", text="Сжато")
        self.archives_tree.heading("deduplicated", text="В хранилище")
        self.archives_tree.column("name", width=210)
        self.archives_tree.column("time", width=150)
        self.archives_tree.column("original", width=90, anchor="e")
        self.archives_tree.column("compressed", width=90, anchor="e")
        self.archives_tree.column("deduplicated", width=90, anchor="e")
        self.archives_tree.grid(row=0, column=0, columnspan=3, sticky="ew")
        self.archives_tree.bind("<<TreeviewSelect>>", self.on_archive_selected)
        ttk.Button(archives_box, text="Информация", command=self.show_archive_info).grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(archives_box, text="Удалить архив", command=self.delete_archive).grid(row=1, column=1, sticky="ew", padx=8, pady=(8, 0))
        ttk.Button(archives_box, text="Обновить", command=self.list_archives).grid(row=1, column=2, sticky="ew", pady=(8, 0))

        log_box = ttk.LabelFrame(right, text="Журнал", padding=10)
        log_box.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        log_box.columnconfigure(0, weight=1)
        log_box.rowconfigure(0, weight=1)

        self.log = tk.Text(log_box, wrap="word", state="disabled", height=24)
        self.log.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_box, orient="vertical", command=self.log.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scrollbar.set)

        status = ttk.Frame(root)
        status.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        status.columnconfigure(0, weight=0)
        status.columnconfigure(1, weight=1)
        status.columnconfigure(2, weight=0)
        ttk.Label(status, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(status, mode="indeterminate")
        self.progress.grid(row=0, column=1, sticky="ew", padx=12)
        ttk.Label(status, textvariable=self.elapsed_var).grid(row=0, column=2, sticky="e")

    def _refresh_dependency_state(self) -> None:
        borg = shutil.which("borg")
        if borg:
            self.deps_label.configure(text=f"Borg найден: {borg}")
        else:
            self.deps_label.configure(
                text="Borg не найден. Установите: sudo apt install borgbackup",
                foreground="red",
            )

    def borg_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.passphrase_var.get():
            env["BORG_PASSPHRASE"] = self.passphrase_var.get()
        return env

    def enqueue_log(self, line: str) -> None:
        self.log_queue.put(line)

    def flush_log(self) -> None:
        while not self.log_queue.empty():
            line = self.log_queue.get_nowait()
            self.update_borg_progress(line)
            self.log.configure(state="normal")
            self.log.insert("end", line + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        self.after(100, self.flush_log)

    def update_borg_progress(self, line: str) -> None:
        match = BORG_PROGRESS_RE.match(line)
        if not match:
            return
        self.status_var.set(
            "Файлов: {files} | исходно: {original} | сжато: {compressed} | в архив: {deduplicated}".format(
                **match.groupdict()
            )
        )

    def command_finished(self, code: int) -> None:
        self.after(0, self.finish_operation, code)

    def start_operation(self, label: str = "Выполняется...") -> None:
        self.operation_started_at = time.monotonic()
        self.status_var.set(label)
        self.elapsed_var.set("00:00")
        self.progress.start(12)
        self.after(1000, self.update_elapsed)

    def update_elapsed(self) -> None:
        if self.operation_started_at is None:
            return
        elapsed = int(time.monotonic() - self.operation_started_at)
        minutes, seconds = divmod(elapsed, 60)
        self.elapsed_var.set(f"{minutes:02d}:{seconds:02d}")
        self.after(1000, self.update_elapsed)

    def finish_operation(self, code: int) -> None:
        self.operation_started_at = None
        self.progress.stop()
        if code == 0:
            self.enqueue_log("Готово.")
            self.status_var.set("Готово")
            if self.current_operation == "check":
                self.enqueue_log(
                    "Отчет проверки: целостность подтверждена, Borg завершился с кодом 0."
                )
                messagebox.showinfo(
                    APP_NAME,
                    "Проверка целостности завершена успешно.\n\n"
                    f"Проверено: {self.current_operation_target}",
                )
            self.current_operation = ""
            self.current_operation_target = ""
        elif code == 1:
            self.enqueue_log("Готово с предупреждениями. Проверьте журнал Borg выше.")
            self.status_var.set("Готово с предупреждениями")
            if self.current_operation == "check":
                self.enqueue_log(
                    "Отчет проверки: Borg завершил проверку с предупреждениями, код 1."
                )
                messagebox.showwarning(
                    APP_NAME,
                    "Проверка завершена с предупреждениями.\n\n"
                    "Посмотрите журнал: Borg сообщил код 1.",
                )
            self.current_operation = ""
            self.current_operation_target = ""
        else:
            self.enqueue_log(f"Команда завершилась с кодом {code}.")
            self.status_var.set("Ошибка выполнения")
            if self.current_operation == "check":
                self.enqueue_log(
                    f"Отчет проверки: обнаружена ошибка проверки, код {code}."
                )
                messagebox.showerror(
                    APP_NAME,
                    f"Проверка целостности завершилась ошибкой.\n\nКод Borg: {code}",
                )
            self.current_operation = ""
            self.current_operation_target = ""

    def rollback_failed_create(self, command: list[str]) -> None:
        if len(command) < 3 or command[0:2] != ["borg", "create"]:
            return
        archive_ref = next((arg for arg in command if "::" in arg and not arg.startswith("--")), "")
        if not archive_ref:
            return
        repo = archive_ref.split("::", 1)[0]
        self.enqueue_log(f"Rollback: удаляю неполный архив {archive_ref}")
        delete_cmd = ["borg", "delete", "--force", archive_ref]
        compact_cmd = ["borg", "compact", repo]
        for cleanup_cmd in (delete_cmd, compact_cmd):
            self.enqueue_log("$ " + " ".join(cleanup_cmd))
            subprocess.run(
                cleanup_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=self.borg_env(),
                check=False,
            )

    def run_borg(self, args: list[str], cwd: str | None = None) -> None:
        if not shutil.which("borg"):
            messagebox.showerror(APP_NAME, "borgbackup не установлен. Выполните: sudo apt install borgbackup")
            return
        repo = self.repo_var.get().strip()
        if not repo:
            messagebox.showwarning(APP_NAME, "Выберите репозиторий Borg.")
            return
        if self.runner.running():
            messagebox.showinfo(APP_NAME, "Дождитесь завершения текущей операции.")
            return

        command = ["borg", *args]
        self.start_operation()

        def worker() -> None:
            code = -1
            try:
                self.enqueue_log("$ " + " ".join(command))
                self.runner.process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=self.borg_env(),
                )
                assert self.runner.process.stdout is not None
                for line in self.runner.process.stdout:
                    self.enqueue_log(line.rstrip())
                code = self.runner.process.wait()
            except Exception as exc:  # noqa: BLE001 - visible GUI error.
                self.enqueue_log(f"Ошибка: {exc}")
            finally:
                self.command_finished(code)

        self.runner._thread = threading.Thread(target=worker, daemon=True)
        self.runner._thread.start()

    def run_borg_sequence(self, commands: list[list[str]], cwd: str | None = None) -> None:
        if not shutil.which("borg"):
            messagebox.showerror(APP_NAME, "borgbackup не установлен. Выполните: sudo apt install borgbackup")
            return
        if self.runner.running():
            messagebox.showinfo(APP_NAME, "Дождитесь завершения текущей операции.")
            return
        self.start_operation()

        def worker() -> None:
            final_code = 0
            for command in commands:
                code = -1
                try:
                    self.enqueue_log("$ " + " ".join(command))
                    self.runner.process = subprocess.Popen(
                        command,
                        cwd=cwd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                        env=self.borg_env(),
                    )
                    assert self.runner.process.stdout is not None
                    for line in self.runner.process.stdout:
                        self.enqueue_log(line.rstrip())
                    code = self.runner.process.wait()
                except Exception as exc:  # noqa: BLE001 - visible GUI error.
                    self.enqueue_log(f"Ошибка: {exc}")
                if code != 0:
                    if code >= 2:
                        self.rollback_failed_create(command)
                    final_code = code
                    break
            self.command_finished(final_code)

        self.runner._thread = threading.Thread(target=worker, daemon=True)
        self.runner._thread.start()

    def choose_repo(self) -> None:
        path = filedialog.askdirectory(title="Выберите или создайте каталог репозитория")
        if path:
            self.repo_var.set(path)

    def choose_restore_target(self) -> None:
        path = filedialog.askdirectory(title="Куда восстановить архив")
        if path:
            self.restore_target_var.set(path)

    def add_source(self) -> None:
        path = filedialog.askdirectory(title="Выберите каталог для резервного копирования")
        if path and path not in self.sources:
            self.sources.append(path)
            self.sources_list.insert("end", path)

    def add_root_source(self) -> None:
        if "/" not in self.sources:
            self.sources.append("/")
            self.sources_list.insert("end", "/")

    def remove_source(self) -> None:
        selected = list(self.sources_list.curselection())
        for index in reversed(selected):
            self.sources_list.delete(index)
            del self.sources[index]

    def init_repo(self) -> None:
        repo = self.repo_var.get().strip()
        if not repo:
            messagebox.showwarning(APP_NAME, "Выберите хранилище копий.")
            return
        Path(repo).mkdir(parents=True, exist_ok=True)
        encryption = "repokey-blake2" if self.passphrase_var.get() else "none"
        self.run_borg(["init", "--encryption", encryption, repo])

    def borg_init_command(self, repo: str) -> list[str]:
        encryption = "repokey-blake2" if self.passphrase_var.get() else "none"
        return ["borg", "init", "--encryption", encryption, repo]

    def repo_is_initialized(self, repo: str) -> bool:
        return Path(repo, "config").exists()

    def build_create_args(self, repo: str, sources: list[str], archive_prefix: str) -> list[str]:
        archive_name = archive_prefix + _dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        args = [
            "borg",
            "create",
            "--verbose",
            "--stats",
            "--progress",
            "--show-rc",
            "--numeric-owner",
            "--compression",
            COMPRESSION_PRESETS.get(self.compression_var.get(), "lzma,9"),
        ]
        excludes = [*DEFAULT_EXCLUDES]
        repo_path = str(Path(repo).resolve())
        if any(source == "/" or repo_path.startswith(str(Path(source).resolve())) for source in sources):
            excludes.append(repo_path)
        for exclude in excludes:
            args.extend(["--exclude", exclude])
        args.append(f"{repo}::{archive_name}")
        args.extend(sources)
        return args

    def run_backup(self, sources: list[str], archive_prefix: str) -> None:
        repo = self.repo_var.get().strip()
        if not repo:
            messagebox.showwarning(APP_NAME, "Выберите хранилище копий.")
            return
        if not sources:
            messagebox.showwarning(APP_NAME, "Добавьте хотя бы один каталог.")
            return

        Path(repo).mkdir(parents=True, exist_ok=True)
        if not self.confirm_storage_capacity(repo, sources):
            return
        commands = []
        if not self.repo_is_initialized(repo):
            commands.append(self.borg_init_command(repo))
        commands.append(self.build_create_args(repo, sources, archive_prefix))
        self.run_borg_sequence(commands)

    def confirm_storage_capacity(self, repo: str, sources: list[str]) -> bool:
        try:
            repo_usage = shutil.disk_usage(repo)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Не удалось проверить свободное место: {exc}")
            return False

        if repo_usage.free < 1024**3:
            messagebox.showerror(
                APP_NAME,
                "В хранилище меньше 1 ГБ свободного места.\n\n"
                "Резервная копия почти наверняка не поместится. Операция отменена.",
            )
            return False

        if sources == ["/"]:
            try:
                root_usage = shutil.disk_usage("/")
            except OSError:
                return True
            used = root_usage.used
            if repo_usage.free < used:
                return messagebox.askyesno(
                    APP_NAME,
                    "Свободного места меньше, чем занято в системе до сжатия.\n\n"
                    f"Занято в системе: {format_bytes(used)}\n"
                    f"Свободно в хранилище: {format_bytes(repo_usage.free)}\n\n"
                    "Сжатие и дедупликация могут уменьшить архив, но места может не хватить. Продолжить?",
                )
        return True

    def create_system_backup(self) -> None:
        confirm = messagebox.askyesno(
            APP_NAME,
            "Создать полную копию системы?\n\n"
            "Будет скопирован корневой каталог / с исключением служебных каталогов "
            "/proc, /sys, /dev, /run, /tmp, /mnt, /media и самого хранилища копий.",
        )
        if confirm:
            self.run_backup(["/"], "system-")

    def create_backup(self) -> None:
        self.run_backup(self.sources, "backup-")

    def list_archives(self) -> None:
        repo = self.repo_var.get().strip()
        if not repo:
            messagebox.showwarning(APP_NAME, "Выберите хранилище копий.")
            return
        if not shutil.which("borg"):
            messagebox.showerror(APP_NAME, "borgbackup не установлен.")
            return

        try:
            result = subprocess.run(
                ["borg", "list", "--json", repo],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=self.borg_env(),
            )
        except subprocess.CalledProcessError as exc:
            self.enqueue_log(exc.stdout)
            messagebox.showerror(APP_NAME, "Не удалось получить список архивов. Подробности в журнале.")
            return

        data = json.loads(result.stdout)
        archives = [item["name"] for item in data.get("archives", [])]
        self.archive_combo.configure(values=archives)
        self.archives_tree.delete(*self.archives_tree.get_children())
        for item in data.get("archives", []):
            info = self.get_archive_info(repo, item["name"])
            self.archives_tree.insert(
                "",
                "end",
                values=(
                    item["name"],
                    item.get("time", "")[:19].replace("T", " "),
                    info.get("original", ""),
                    info.get("compressed", ""),
                    info.get("deduplicated", ""),
                ),
            )
        if archives:
            self.archive_var.set(archives[-1])
        self.enqueue_log(f"Найдено архивов: {len(archives)}")

    def get_archive_info(self, repo: str, archive: str) -> dict[str, str]:
        try:
            result = subprocess.run(
                ["borg", "info", "--json", f"{repo}::{archive}"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=self.borg_env(),
            )
            data = json.loads(result.stdout)
        except Exception as exc:  # noqa: BLE001 - visible GUI error.
            self.enqueue_log(f"Не удалось прочитать информацию об архиве {archive}: {exc}")
            return {}

        stats = data.get("archives", [{}])[0].get("stats", {})
        return {
            "original": format_bytes(int(stats.get("original_size", 0))),
            "compressed": format_bytes(int(stats.get("compressed_size", 0))),
            "deduplicated": format_bytes(int(stats.get("deduplicated_size", 0))),
        }

    def on_archive_selected(self, _event=None) -> None:
        selected = self.archives_tree.selection()
        if not selected:
            return
        values = self.archives_tree.item(selected[0], "values")
        if values:
            self.archive_var.set(values[0])

    def selected_archive(self) -> str:
        selected = self.archives_tree.selection()
        if selected:
            values = self.archives_tree.item(selected[0], "values")
            if values:
                return str(values[0])
        return self.archive_var.get().strip()

    def show_archive_info(self) -> None:
        repo = self.repo_var.get().strip()
        archive = self.selected_archive()
        if not repo or not archive:
            messagebox.showwarning(APP_NAME, "Выберите хранилище и архив.")
            return
        try:
            result = subprocess.run(
                ["borg", "info", f"{repo}::{archive}"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=self.borg_env(),
            )
        except subprocess.CalledProcessError as exc:
            self.enqueue_log(exc.stdout)
            messagebox.showerror(APP_NAME, "Не удалось получить информацию об архиве.")
            return
        self.enqueue_log(result.stdout.strip())
        messagebox.showinfo(APP_NAME, f"Информация об архиве выведена в журнал:\n{archive}")

    def delete_archive(self) -> None:
        repo = self.repo_var.get().strip()
        archive = self.selected_archive()
        if not repo or not archive:
            messagebox.showwarning(APP_NAME, "Выберите хранилище и архив.")
            return
        if not messagebox.askyesno(APP_NAME, f"Удалить архив?\n\n{archive}"):
            return
        self.run_borg_sequence(
            [
                ["borg", "delete", "--force", f"{repo}::{archive}"],
                ["borg", "compact", repo],
            ]
        )

    def check_repo(self) -> None:
        repo = self.repo_var.get().strip()
        if not repo:
            messagebox.showwarning(APP_NAME, "Выберите хранилище копий.")
            return
        self.current_operation = "check"
        self.current_operation_target = repo
        self.run_borg(["check", "--verify-data", "--progress", repo])

    def restore_archive(self) -> None:
        repo = self.repo_var.get().strip()
        archive = self.selected_archive()
        target = self.restore_target_var.get().strip()
        if not repo or not archive or not target:
            messagebox.showwarning(APP_NAME, "Укажите репозиторий, архив и каталог восстановления.")
            return

        confirm = messagebox.askyesno(
            APP_NAME,
            "Восстановить архив?\n\n"
            f"Архив: {archive}\n"
            f"Каталог: {target}\n\n"
            "Файлы будут извлечены в выбранный каталог. Для восстановления всей системы "
            "лучше запускать программу из LiveUSB или другой установленной системы, "
            "а целевой системный раздел заранее смонтировать.",
        )
        if not confirm:
            return

        Path(target).mkdir(parents=True, exist_ok=True)
        args = ["extract", "--verbose", "--progress", "--numeric-owner"]
        if not self.restore_metadata_var.get():
            args.extend(["--noacls", "--noxattrs"])
        args.append(f"{repo}::{archive}")
        self.run_borg(args, cwd=target)

    def stop_command(self) -> None:
        self.runner.stop()
        self.status_var.set("Остановка...")


def main() -> None:
    app = BackupManagerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
