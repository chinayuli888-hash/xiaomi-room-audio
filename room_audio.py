"""Room Audio, the Windows panel for Wi-Fi speakers."""
import argparse
import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
from queue import Empty, SimpleQueue
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from tray import APP_NAME, TrayIcon, show_existing
from volume_slider import VolumeSlider

ROOT = Path(__file__).resolve().parent
ICON = ROOT / "room_audio.ico"
BG, CARD, ROW = "#111A22", "#1B2934", "#18242E"
INK, DIM, FAINT = "#F3F7F9", "#A8B9C6", "#62788A"
GREEN, GREEN_INK = "#5BE2A8", "#092B20"
FONT = "Microsoft YaHei UI"


def wifi_name():
    command = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "netsh.exe"
    try:
        result = subprocess.run(
            [command, "wlan", "show", "interfaces"], capture_output=True,
            encoding="oem", errors="replace", timeout=3, check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    match = re.search(r"^[ \t]*SSID[ \t]*:[ \t]*(\S[^\r\n]*)", result.stdout, re.MULTILINE)
    return match.group(1).strip() if match else ""


def wifi_icon(parent):
    mark = tk.Canvas(parent, width=31, height=31, bg=CARD, highlightthickness=0)
    for box in [(2, 7, 29, 30), (7, 12, 24, 29), (12, 17, 19, 28)]:
        mark.create_arc(*box, start=46, extent=88, outline=GREEN, width=2, style=tk.ARC)
    mark.create_oval(14, 23, 17, 26, fill=GREEN, outline="")
    return mark


def speaker_icon(parent):
    mark = tk.Canvas(parent, width=18, height=22, bg=ROW, highlightthickness=0)
    mark.create_rectangle(3, 2, 15, 20, outline=FAINT, width=1)
    mark.create_oval(7, 5, 11, 9, fill=FAINT, outline="")
    mark.create_oval(6, 11, 12, 17, outline=FAINT, width=1)
    return mark


class Window:
    def __init__(self, root, settings, state_dir):
        from engine import SpeakerMix

        self.root, self.settings, self.state_dir = root, settings, state_dir
        self.events = SimpleQueue()
        self.player = self.thread = None
        self.tray = None
        self.closing = self.playing = False
        self.refreshing_levels = False
        self.mix = SpeakerMix(settings.hosts)
        self.rows = {}
        self.network = tk.StringVar(value="当前局域网")
        self.count = tk.StringVar()
        root.title(APP_NAME)
        root.iconbitmap(default=str(ICON))
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.hide_window)

        self.configure_styles()
        main = tk.Frame(root, bg=BG, padx=20, pady=18)
        main.pack(fill="both", expand=True)
        network = tk.Frame(main, bg=CARD, padx=13, pady=10)
        network.pack(fill="x", pady=(0, 11))
        wifi_icon(network).pack(side="left", padx=(0, 9))
        tk.Label(network, textvariable=self.network, bg=CARD, fg=INK,
                 font=(FONT, 10, "bold"), anchor="w", justify="left",
                 wraplength=220).pack(side="left", fill="x", expand=True)
        tk.Label(network, textvariable=self.count, bg=CARD, fg=GREEN,
                 font=(FONT, 10, "bold")).pack(side="right", padx=(8, 0))

        for index, host in enumerate(settings.hosts, start=1):
            row = tk.Frame(main, bg=ROW, padx=11, pady=6)
            row.pack(fill="x", pady=(0, 5))
            row.columnconfigure(2, weight=1)
            speaker_icon(row).grid(row=0, column=0, padx=(0, 8))
            name = settings.speaker_names.get(host) or f"音箱 {index}"
            tk.Label(row, text=name, bg=ROW, fg=INK, font=(FONT, 10),
                     anchor="w", justify="left", wraplength=122, width=13).grid(
                         row=0, column=1, sticky="w", padx=(0, 9))
            label = tk.StringVar(value="—")
            value = tk.DoubleVar()
            slider = VolumeSlider(row, variable=value,
                                  command=lambda amount, host=host: self.adjust(host, amount))
            slider.grid(row=0, column=2, sticky="ew")
            tk.Label(row, textvariable=label, bg=ROW, fg=INK,
                     font=("Segoe UI", 10, "bold"), anchor="e", width=5).grid(
                         row=0, column=3, sticky="e", padx=(6, 0))
            self.rows[host] = (label, value, slider)

        actions = tk.Frame(main, bg=BG)
        actions.pack(fill="x", pady=(9, 0))
        self.play = ttk.Button(actions, text="AP2 直推", command=self.start, style="Play.TButton")
        self.play.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.exit = ttk.Button(actions, width=11, text="退出", command=self.exit_app, style="Exit.TButton")
        self.exit.pack(side="right")
        self.show_count()
        self.refresh_levels()
        self.refresh_wifi()
        root.update_idletasks()
        width = max(452, root.winfo_reqwidth())
        height = root.winfo_reqheight()
        root.geometry(f"{width}x{height}")
        root.minsize(405, height)
        self.play.focus_set()
        root.after(25, self.poll)

    def configure_styles(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Play.TButton", font=(FONT, 10, "bold"), foreground=GREEN_INK,
                        background=GREEN, bordercolor=GREEN, darkcolor=GREEN,
                        lightcolor=GREEN, padding=(14, 8))
        style.map("Play.TButton", foreground=[("disabled", "#D2F7E7")],
                  background=[("disabled", "#24604D"), ("active", "#7DEEBE")],
                  bordercolor=[("disabled", "#24604D")])
        style.configure("Exit.TButton", font=(FONT, 10), foreground=INK,
                        background=CARD, bordercolor="#354958", darkcolor=CARD,
                        lightcolor=CARD, padding=(11, 8))
        style.map("Exit.TButton", foreground=[("disabled", FAINT)],
                  background=[("active", "#304657")])

    def refresh_wifi(self):
        def read():
            self.events.put(("wifi", wifi_name()))
        threading.Thread(target=read, name="wifi-name", daemon=True).start()

    def show_count(self, online=None):
        total = len(self.rows)
        self.count.set(f"{total} 台音箱" if online is None else f"{online}/{total} 台在线")

    def refresh_levels(self):
        master, gains = self.mix.snapshot()
        playing = self.playing and not self.closing
        adjustable = playing and not master.muted and master.scalar > 0
        self.refreshing_levels = True
        try:
            for host, (label, value, slider) in self.rows.items():
                amount = gains[host] * 100 if playing else 0
                value.set(amount)
                label.set(f"{round(amount)}%" if playing else "—")
                slider.state(["!disabled"] if adjustable else ["disabled"])
        finally:
            self.refreshing_levels = False

    def adjust(self, host, amount):
        if not self.playing or self.closing or self.refreshing_levels:
            return
        self.mix.set_speaker_percent(host, float(amount))
        self.refresh_levels()

    def start(self):
        from engine import Player

        if self.thread and self.thread.is_alive():
            return
        self.play.configure(text="正在连接…")
        self.play.state(["disabled"])
        self.refresh_wifi()
        self.player = Player(self.settings, self.state_dir, self.emit, self.mix)
        self.thread = threading.Thread(target=self.player.run, name="playback")
        self.thread.start()

    def emit(self, event, value):
        self.events.put((event, value))

    def show_window(self):
        if not self.closing:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()

    def hide_window(self):
        if self.closing:
            return
        if self.tray is not None and self.tray.ensure_icon():
            self.root.withdraw()
        else:
            self.root.iconify()

    def exit_app(self):
        if self.closing:
            return
        self.closing = True
        if not self.thread or not self.thread.is_alive():
            self.root.destroy()
            return
        self.refresh_levels()
        self.play.state(["disabled"])
        self.exit.configure(text="正在退出…")
        self.exit.state(["disabled"])
        self.player.stop.set()

    def poll(self):
        while True:
            try:
                event, value = self.events.get_nowait()
            except Empty:
                break
            if event == "wifi":
                self.network.set(f"Wi-Fi · {value}" if value else "当前局域网")
            elif event == "system-volume":
                self.refresh_levels()
            elif event == "playing" and not self.closing:
                self.playing = True
                self.play.configure(text="AP2 播放中")
                self.show_count(value)
                self.refresh_levels()
            elif event == "stopped":
                self.playing = False
                self.show_count()
                self.refresh_levels()
                if self.closing and not value:
                    self.root.destroy()
                    return
                self.closing = False
                self.exit.configure(text="退出")
                self.exit.state(["!disabled"])
                self.play.configure(text="AP2 直推")
                self.play.state(["!disabled"])
                if value:
                    if self.root.state() == "withdrawn":
                        self.show_window()
                    messagebox.showerror(APP_NAME, value, parent=self.root)
        self.root.after(25, self.poll)
        if self.tray is not None:
            self.tray.dispatch()


def state_dir():
    folder = Path(os.environ["LOCALAPPDATA"]) / "RoomAudio"
    folder.mkdir(parents=True, exist_ok=True)
    log = RotatingFileHandler(folder / "room-audio.log", maxBytes=256_000,
                              backupCount=1, encoding="utf-8")
    logging.basicConfig(handlers=[log], format="%(asctime)s %(levelname)s %(message)s",
                        level=logging.INFO)
    return folder


def main():
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--config", type=Path, default=ROOT / "settings.json")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="read-only endpoint and dependency check")
    mode.add_argument("--restore", action="store_true", help="restore playback endpoints after an interrupted run")
    opts = parser.parse_args()
    from engine import Settings, find_cable, recover
    from windows_audio import AlreadyRunning, MEDIA_ROLES, SingleInstance, WindowsAudio

    if opts.check:
        settings = Settings.load(opts.config)
        with WindowsAudio() as win:
            defaults = [{"role": role, "name": win.default(role).name} for role in (0, 1, 2)]
            level = win.level(win.default(MEDIA_ROLES[0]).id)
            print(json.dumps({
                "wifi": wifi_name(),
                "receivers": len(settings.hosts),
                "speaker_names": [settings.speaker_names.get(host) or f"音箱 {i}"
                                  for i, host in enumerate(settings.hosts, 1)],
                "speaker_max_db": [settings.receiver_db(host) for host in settings.hosts],
                "ffmpeg_exists": settings.ffmpeg.is_file(),
                "render_defaults": defaults,
                "microphone_default": win.default(flow=1).name,
                "cable": find_cable(win).name,
                "current_system_volume": level.percent,
                "current_system_muted": level.muted,
            }, ensure_ascii=False, indent=2))
        return

    try:
        with SingleInstance():
            folder = state_dir()
            with WindowsAudio() as win:
                recover(win, folder / "restore.json")
            if opts.restore:
                print("播放输出恢复检查完成。")
                return
            settings = Settings.load(opts.config)
            root = tk.Tk()
            app = Window(root, settings, folder)
            try:
                app.tray = TrayIcon(app.show_window, app.exit_app, ICON)
            except OSError:
                logging.exception("Could not create the Windows tray icon")
            try:
                root.mainloop()
            finally:
                try:
                    if app.thread and app.thread.is_alive():
                        app.player.stop.set()
                        app.thread.join()
                finally:
                    if app.tray is not None:
                        app.tray.close()
    except AlreadyRunning:
        if opts.restore or not show_existing():
            raise


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        logging.exception("RoomAudio failed")
        if any(arg in ("--check", "--restore") for arg in sys.argv):
            raise SystemExit(str(error)) from None
        ctypes.windll.user32.MessageBoxW(None, str(error), APP_NAME, 0x10)
        raise SystemExit(1) from None

