# -*- coding: utf-8 -*-
"""
main.py

GC Converter - 켐스테이션 DATA 폴더 안의 .D 크로마토그램들을 한 번에
"시료번호.csv"로 변환해주는 간단한 GUI. OilScope에 넣을 CSV를 준비하는
전 단계 도구다 (켐스테이션에서 매번 우클릭 -> Export -> CSV로 하나씩
내보내던 작업을 자동화한다).
"""

from __future__ import annotations

import json
import os
import platform
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from converter import convert_all  # noqa: E402

APP_TITLE = "GC Converter — 크로마토그램 일괄 CSV 변환"
SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gcconverter_settings.json")

LEVEL_COLORS = {
    "ok": "#1B7A3D",
    "skip": "#8A7238",
    "error": "#B3261E",
    "info": "#333333",
}


def load_settings() -> dict:
    if os.path.isfile(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_settings(data: dict) -> None:
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def open_in_file_manager(path: str) -> None:
    if platform.system() == "Windows":
        os.startfile(path)  # type: ignore[attr-defined]
    elif platform.system() == "Darwin":
        subprocess.run(["open", path], check=False)
    else:
        subprocess.run(["xdg-open", path], check=False)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("780x580")
        self.minsize(640, 480)

        self.settings = load_settings()
        self._log_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._stop_flag = threading.Event()
        self._worker: threading.Thread | None = None

        self._build_widgets()
        self.after(100, self._drain_log_queue)

    # ------------------------------------------------------------------
    def _build_widgets(self) -> None:
        pad = {"padx": 10, "pady": 6}

        intro = ttk.Label(
            self,
            text=(
                "켐스테이션 DATA 폴더(예: C:\\Chem32\\1\\DATA) 아래의 모든 .D 크로마토그램을 찾아서,\n"
                "시료번호.csv 파일로 한 번에 변환합니다. 시료번호는 켐스테이션에 입력한 Notebook 값을 그대로 씁니다."
            ),
            justify="left",
        )
        intro.pack(fill="x", **pad)

        frm_paths = ttk.Frame(self)
        frm_paths.pack(fill="x", **pad)
        frm_paths.columnconfigure(1, weight=1)

        ttk.Label(frm_paths, text="켐스테이션 DATA 폴더").grid(row=0, column=0, sticky="w")
        self.var_input = tk.StringVar(value=self.settings.get("input_dir", ""))
        ttk.Entry(frm_paths, textvariable=self.var_input).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(frm_paths, text="찾아보기...", command=self._pick_input).grid(row=0, column=2)

        ttk.Label(frm_paths, text="CSV 저장 폴더").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.var_output = tk.StringVar(value=self.settings.get("output_dir", ""))
        ttk.Entry(frm_paths, textvariable=self.var_output).grid(row=1, column=1, sticky="ew", padx=6, pady=(8, 0))
        ttk.Button(frm_paths, text="찾아보기...", command=self._pick_output).grid(row=1, column=2, pady=(8, 0))

        frm_opts = ttk.Frame(self)
        frm_opts.pack(fill="x", **pad)
        self.var_overwrite = tk.BooleanVar(value=self.settings.get("overwrite", False))
        ttk.Checkbutton(
            frm_opts,
            text="이미 변환된 CSV도 새로 덮어쓰기 (기본은 그 사이 새로 쌓인 시료만 변환)",
            variable=self.var_overwrite,
        ).pack(side="left")

        frm_actions = ttk.Frame(self)
        frm_actions.pack(fill="x", **pad)
        self.btn_start = ttk.Button(frm_actions, text="변환 시작", command=self._start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(frm_actions, text="중단", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        self.btn_open_out = ttk.Button(frm_actions, text="저장 폴더 열기", command=self._open_output)
        self.btn_open_out.pack(side="left")

        self.progress = ttk.Progressbar(self, mode="indeterminate")
        self.progress.pack(fill="x", **pad)

        frm_log = ttk.Frame(self)
        frm_log.pack(fill="both", expand=True, **pad)
        self.txt_log = tk.Text(frm_log, wrap="word", state="disabled", font=("Consolas", 10))
        scrollbar = ttk.Scrollbar(frm_log, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=scrollbar.set)
        self.txt_log.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        for level, color in LEVEL_COLORS.items():
            self.txt_log.tag_config(level, foreground=color)

    # ------------------------------------------------------------------
    def _pick_input(self) -> None:
        path = filedialog.askdirectory(title="켐스테이션 DATA 폴더 선택")
        if path:
            self.var_input.set(path)

    def _pick_output(self) -> None:
        path = filedialog.askdirectory(title="CSV를 저장할 폴더 선택")
        if path:
            self.var_output.set(path)

    def _open_output(self) -> None:
        path = self.var_output.get().strip()
        if not path or not os.path.isdir(path):
            messagebox.showinfo(APP_TITLE, "먼저 CSV 저장 폴더를 지정하세요.")
            return
        open_in_file_manager(path)

    # ------------------------------------------------------------------
    def _append_log(self, level: str, message: str) -> None:
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", message + "\n", level)
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def _drain_log_queue(self) -> None:
        try:
            while True:
                level, message = self._log_queue.get_nowait()
                self._append_log(level, message)
        except queue.Empty:
            pass
        self.after(100, self._drain_log_queue)

    # ------------------------------------------------------------------
    def _start(self) -> None:
        input_dir = self.var_input.get().strip()
        output_dir = self.var_output.get().strip()
        if not input_dir or not os.path.isdir(input_dir):
            messagebox.showerror(APP_TITLE, "켐스테이션 DATA 폴더를 올바르게 지정하세요.")
            return
        if not output_dir:
            messagebox.showerror(APP_TITLE, "CSV를 저장할 폴더를 지정하세요.")
            return
        os.makedirs(output_dir, exist_ok=True)

        overwrite = self.var_overwrite.get()
        save_settings({"input_dir": input_dir, "output_dir": output_dir, "overwrite": overwrite})

        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

        self._stop_flag.clear()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.progress.start(12)

        def worker() -> None:
            def on_log(level: str, message: str) -> None:
                self._log_queue.put((level, message))

            try:
                convert_all(
                    input_dir,
                    output_dir,
                    overwrite=overwrite,
                    log_cb=on_log,
                    should_stop=self._stop_flag.is_set,
                )
            except Exception as e:  # noqa: BLE001
                self._log_queue.put(("error", f"예상치 못한 오류: {e}"))
            finally:
                self.after(0, self._finish)

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()

    def _stop(self) -> None:
        self._stop_flag.set()
        self.btn_stop.configure(state="disabled")

    def _finish(self) -> None:
        self.progress.stop()
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
