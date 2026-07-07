#!/usr/bin/env python3
"""Linux Backup Manager GUI for Astra Linux."""

from __future__ import annotations

import datetime as _dt
import os
import queue
import shutil
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


APP_NAME = "Linux Backup Manager"
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
        self.compression_var = tk.StringVar(value="lzma,9")
        self.status_var = tk.StringVar(value="Готово")

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

        ttk.Label(settings, text="Репозиторий Borg").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(settings, textvariable=self.repo_var).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(settings, text="Выбрать", command=self.choose_repo).grid(row=0, column=2, padx=(8, 0), pady=4)

        ttk.Label(settings, text="Пароль").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(settings, textvariable=self.passphrase_var, show="*").grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(settings, text="Создать репозиторий", command=self.init_repo).grid(row=1, column=2, padx=(8, 0), pady=4)

        ttk.Label(settings, text="Сжатие").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        compression = ttk.Combobox(
            settings,
            textvariable=self.compression_var,
            values=("lzma,9", "zstd,22", "zstd,15", "zlib,9", "none"),
            state="readonly",
            width=18,
        )
        compression.grid(row=2, column=1, sticky="w", pady=4)
        ttk.Button(settings, text="Обновить список архивов", command=self.list_archives).grid(
            row=2, column=2, padx=(8, 0), pady=4
        )

        left = ttk.Frame(root)
        left.grid(row=1, column=0, sticky="nsw", pady=(10, 0), padx=(0, 10))
        left.rowconfigure(0, weight=1)

        sources_box = ttk.LabelFrame(left, text="Каталоги для копии", padding=10)
        sources_box.grid(row=0, column=0, sticky="nsew")
        sources_box.rowconfigure(0, weight=1)
        sources_box.columnconfigure(0, weight=1)

        self.sources_list = tk.Listbox(sources_box, height=12, width=38, selectmode=tk.EXTENDED)
        self.sources_list.grid(row=0, column=0, columnspan=3, sticky="nsew")
        ttk.Button(sources_box, text="Добавить", command=self.add_source).grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(sources_box, text="Удалить", command=self.remove_source).grid(row=1, column=1, sticky="ew", padx=6, pady=(8, 0))
        ttk.Button(sources_box, text="Добавить /", command=self.add_root_source).grid(row=1, column=2, sticky="ew", pady=(8, 0))

        actions = ttk.LabelFrame(left, text="Действия", padding=10)
        actions.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)

        ttk.Button(actions, text="Создать копию", command=self.create_backup).grid(row=0, column=0, sticky="ew", pady=3)
        ttk.Button(actions, text="Проверить целостность", command=self.check_repo).grid(row=0, column=1, sticky="ew", padx=(8, 0), pady=3)
        ttk.Button(actions, text="Восстановить", command=self.restore_archive).grid(row=1, column=0, sticky="ew", pady=3)
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

        right = ttk.Frame(root)
        right.grid(row=1, column=1, sticky="nsew", pady=(10, 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        self.deps_label = ttk.Label(right, text="")
        self.deps_label.grid(row=0, column=0, sticky="ew")

        log_box = ttk.LabelFrame(right, text="Журнал", padding=10)
        log_box.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        log_box.columnconfigure(0, weight=1)
        log_box.rowconfigure(0, weight=1)

        self.log = tk.Text(log_box, wrap="word", state="disabled", height=24)
        self.log.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_box, orient="vertical", command=self.log.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scrollbar.set)

        status = ttk.Frame(root)
        status.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        status.columnconfigure(0, weight=1)
        ttk.Label(status, textvariable=self.status_var).grid(row=0, column=0, sticky="w")

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
            self.log.configure(state="normal")
            self.log.insert("end", line + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        self.after(100, self.flush_log)

    def command_finished(self, code: int) -> None:
        if code == 0:
            self.enqueue_log("Готово.")
            self.status_var.set("Готово")
        else:
            self.enqueue_log(f"Команда завершилась с кодом {code}.")
            self.status_var.set("Ошибка выполнения")

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
        self.status_var.set("Выполняется...")

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
            messagebox.showwarning(APP_NAME, "Выберите каталог репозитория.")
            return
        Path(repo).mkdir(parents=True, exist_ok=True)
        encryption = "repokey-blake2" if self.passphrase_var.get() else "none"
        self.run_borg(["init", "--encryption", encryption, repo])

    def create_backup(self) -> None:
        repo = self.repo_var.get().strip()
        if not repo:
            messagebox.showwarning(APP_NAME, "Выберите репозиторий Borg.")
            return
        if not self.sources:
            messagebox.showwarning(APP_NAME, "Добавьте хотя бы один каталог.")
            return

        archive_name = "backup-" + _dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        args = [
            "create",
            "--verbose",
            "--stats",
            "--show-rc",
            "--compression",
            self.compression_var.get(),
        ]
        excludes = [*DEFAULT_EXCLUDES]
        repo_path = str(Path(repo).resolve())
        if any(source == "/" or repo_path.startswith(str(Path(source).resolve())) for source in self.sources):
            excludes.append(repo_path)
        for exclude in excludes:
            args.extend(["--exclude", exclude])
        args.append(f"{repo}::{archive_name}")
        args.extend(self.sources)
        self.run_borg(args)

    def list_archives(self) -> None:
        repo = self.repo_var.get().strip()
        if not repo:
            messagebox.showwarning(APP_NAME, "Выберите репозиторий Borg.")
            return
        if not shutil.which("borg"):
            messagebox.showerror(APP_NAME, "borgbackup не установлен.")
            return

        try:
            result = subprocess.run(
                ["borg", "list", "--short", repo],
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

        archives = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        self.archive_combo.configure(values=archives)
        if archives:
            self.archive_var.set(archives[-1])
        self.enqueue_log(f"Найдено архивов: {len(archives)}")

    def check_repo(self) -> None:
        repo = self.repo_var.get().strip()
        if not repo:
            messagebox.showwarning(APP_NAME, "Выберите репозиторий Borg.")
            return
        self.run_borg(["check", "--verify-data", repo])

    def restore_archive(self) -> None:
        repo = self.repo_var.get().strip()
        archive = self.archive_var.get().strip()
        target = self.restore_target_var.get().strip()
        if not repo or not archive or not target:
            messagebox.showwarning(APP_NAME, "Укажите репозиторий, архив и каталог восстановления.")
            return

        confirm = messagebox.askyesno(
            APP_NAME,
            "Восстановить архив?\n\n"
            f"Архив: {archive}\n"
            f"Каталог: {target}\n\n"
            "Файлы будут извлечены в выбранный каталог.",
        )
        if not confirm:
            return

        Path(target).mkdir(parents=True, exist_ok=True)
        self.run_borg(["extract", "--verbose", "--numeric-owner", f"{repo}::{archive}"], cwd=target)

    def stop_command(self) -> None:
        self.runner.stop()
        self.status_var.set("Остановка...")


def main() -> None:
    app = BackupManagerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
