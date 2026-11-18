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
        from device_catalog import DeviceCatalog

        self.root, self.base_settings, self.state_dir = root, settings, state_dir
        self.settings = settings.for_speakers(())
        self.catalog = DeviceCatalog()
        self.network_info = None
        self.scan_stop = threading.Event()
        self.monitor_stop = threading.Event()
        self.scanning = self.stopping = False
        self.pending_scan = None
        self.balances = {}
        try:
            self.profiles = json.loads((state_dir / "speaker_profiles.json").read_text(encoding="utf-8-sig"))
            if not isinstance(self.profiles, dict):
                self.profiles = {}
        except (OSError, ValueError):
            self.profiles = {}
        self.events = SimpleQueue()
        self.player = self.thread = None
        self.tray = None
        self.closing = self.playing = False
        self.refreshing_levels = False
        self.mix = SpeakerMix(())
        self.rows = {}
        self.network = tk.StringVar(value="正在识别当前网络…")
        self.count = tk.StringVar()
        self.status = tk.StringVar(value="正在识别当前网络…")
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

        self.scan = ttk.Button(main, text="一键搜索本地音箱", command=self.search, style="Exit.TButton")
        self.scan.pack(fill="x", pady=(0, 7))
        self.scan.state(["disabled"])
        tk.Label(main, textvariable=self.status, bg=BG, fg=DIM, anchor="w",
                 font=(FONT, 9), wraplength=410).pack(fill="x", pady=(0, 8))
        self.speaker_list = tk.Frame(main, bg=BG)
        self.speaker_list.pack(fill="both", expand=True)
        self.render_devices()

        actions = tk.Frame(main, bg=BG)
        actions.pack(fill="x", pady=(9, 0))
        self.play = ttk.Button(actions, text="AP2 直推", command=self.start, style="Play.TButton")
        self.play.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.play.state(["disabled"])
        self.exit = ttk.Button(actions, width=11, text="退出", command=self.exit_app, style="Exit.TButton")
        self.exit.pack(side="right")
        self.show_count()
        self.refresh_levels()
        self.watch_network()
        root.update_idletasks()
        width = max(452, root.winfo_reqwidth())
        root.geometry(f"{width}x{max(260, root.winfo_reqheight())}")
        root.minsize(405, 260)
        root.after(25, self.poll)

    def render_devices(self):
        self.rows.clear()
        for widget in self.speaker_list.winfo_children():
            widget.destroy()
        for index, host in enumerate(self.settings.hosts, start=1):
            row = tk.Frame(self.speaker_list, bg=ROW, padx=11, pady=6)
            row.pack(fill="x", pady=(0, 5))
            row.columnconfigure(2, weight=1)
            speaker_icon(row).grid(row=0, column=0, padx=(0, 8))
            name = self.settings.speaker_names.get(host) or f"音箱 {index}"
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

        if not self.rows:
            tk.Label(self.speaker_list, text="尚未发现当前网络的音箱", bg=ROW, fg=DIM,
                     font=(FONT, 10), pady=14).pack(fill="x")
        self.root.update_idletasks()
        width = max(452, self.root.winfo_width(), self.root.winfo_reqwidth())
        self.root.geometry(f"{width}x{max(260, self.root.winfo_reqheight())}")

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

    def watch_network(self):
        stop = self.monitor_stop
        def watch():
            from discovery import current_network
            previous = object()
            while not stop.is_set():
                try:
                    network = current_network()
                    key = network.fingerprint
                    event = ("network", network)
                except Exception as exc:
                    key = ("error", str(exc))
                    event = ("network-error", str(exc))
                if key != previous:
                    self.events.put(event)
                    previous = key
                stop.wait(2)
        threading.Thread(target=watch, name="network-monitor", daemon=True).start()

    def remember_balance(self):
        if not self.playing:
            return
        master, gains = self.mix.snapshot()
        if master.muted or master.scalar <= 0:
            return
        for speaker in self.catalog.devices:
            if speaker.host in gains:
                self.balances[speaker.identifier] = gains[speaker.host] / master.scalar

    def clear_devices(self, network):
        self.remember_balance()
        self.scan_stop.set()
        self.catalog.invalidate(network)
        self.scanning = False
        self.settings = self.base_settings.for_speakers(())
        self.render_devices()
        self.show_count()
        self.play.state(["disabled"])

    def network_changed(self, network):
        if self.closing:
            return
        if (network is not None and self.network_info is not None
                and network.fingerprint == self.network_info.fingerprint):
            return
        self.network_info = network
        logging.info("Network changed: active_interfaces=%d; old discovery invalidated",
                     len(network.addresses) if network else 0)
        self.network.set(f"Wi-Fi · {network.ssid}" if network and network.ssid else "当前局域网")
        self.clear_devices(network)
        self.status.set("网络已变化，正在重新搜索…" if network and network.addresses else "网络未连接，请连接音箱所在的局域网。")
        self.queue_scan(network, False)

    def queue_scan(self, network, auto_start):
        if self.closing:
            return
        self.pending_scan = (network, auto_start) if network and network.addresses else None
        if self.thread and self.thread.is_alive():
            self.scan.state(["disabled"])
            self.player.stop.set()
            self.status.set("正在正常停止旧连接并恢复电脑声音…")
            return
        if self.stopping:
            return
        self.scan.state(["!disabled"])
        if self.pending_scan is not None:
            network, auto_start = self.pending_scan
            self.pending_scan = None
            self.launch_scan(network, auto_start)

    def search(self, auto_start=False):
        if self.closing:
            return
        self.clear_devices(self.network_info)
        self.queue_scan(self.network_info, auto_start)

    def launch_scan(self, network, auto_start):
        self.scanning = True
        token = self.catalog.begin(network)
        self.scan_stop = stop = threading.Event()
        self.scan.state(["disabled"])
        self.play.configure(text="正在确认音箱…" if auto_start else "AP2 直推")
        self.status.set("正在搜索本地 AirPlay 2 音箱…")
        self.count.set("搜索中…")
        logging.info("Searching local receivers: generation=%d", token)
        def run():
            from discovery import discover_speakers
            try:
                devices, error = discover_speakers(network, stop), None
            except Exception as exc:
                devices, error = (), str(exc)
            self.events.put(("scan-result", (token, network, devices, error, auto_start)))
        threading.Thread(target=run, name="speaker-discovery", daemon=True).start()

    def scan_result(self, result):
        token, network, devices, error, auto_start = result
        if self.closing or token != self.catalog.generation:
            return
        self.scanning = False
        self.scan.state(["!disabled"])
        self.play.configure(text="AP2 直推")
        try:
            if error:
                raise RuntimeError(error)
            if self.network_info is None or network.fingerprint != self.network_info.fingerprint:
                raise RuntimeError("网络已变化，请重新搜索。")
            if not self.catalog.apply(token, network, devices):
                return
            self.settings = self.base_settings.for_speakers(self.catalog.devices, self.profiles)
            logging.info("Discovery complete: generation=%d receivers=%d", token, len(self.settings.hosts))
            from engine import SpeakerMix
            self.mix = SpeakerMix(self.settings.hosts)
            self.mix.restore_ratios({speaker.host: self.balances.get(speaker.identifier, 1.0)
                                    for speaker in self.catalog.devices})
            self.render_devices()
            self.show_count()
            self.refresh_levels()
            self.status.set(f"发现 {len(self.rows)} 台音箱，点击 AP2 直推开始播放。" if self.rows
                            else "未发现本地音箱，请确认处于同一局域网且已开启 AirPlay。")
            if self.catalog.can_play(self.network_info):
                self.play.state(["!disabled"])
                if auto_start:
                    self.begin_playback(network)
        except Exception as exc:
            logging.warning("Discovery rejected: %s", exc)
            self.catalog.fail(token)
            self.settings = self.base_settings.for_speakers(())
            self.render_devices()
            self.show_count()
            self.play.state(["disabled"])
            self.status.set(f"搜索未完成：{exc}")

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
        if self.closing or self.scanning or self.stopping or (self.thread and self.thread.is_alive()):
            return
        # Reconfirm every endpoint before connecting, even on the same SSID.
        self.search(auto_start=True)

    def begin_playback(self, network):
        from engine import Player

        if (self.closing or self.stopping or self.scanning
                or (self.thread and self.thread.is_alive())
                or not self.catalog.can_play(network)
                or not self.catalog.can_play(self.network_info)):
            return
        self.play.configure(text="正在连接…")
        self.play.state(["disabled"])
        self.status.set("正在连接本次搜索到的音箱…")
        self.player = Player(self.settings, self.state_dir, self.emit, self.mix, network)
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
        self.pending_scan = None
        self.scan_stop.set()
        self.monitor_stop.set()
        self.catalog.invalidate()
        if not self.thread or not self.thread.is_alive():
            self.root.destroy()
            return
        self.refresh_levels()
        self.play.state(["disabled"])
        self.exit.configure(text="正在退出…")
        self.exit.state(["disabled"])
        self.player.stop.set()

    def finish_stop(self, issue):
        # Player emits stopped just before returning; never overlap two senders.
        if self.thread and self.thread.is_alive():
            self.root.after(50, lambda: self.finish_stop(issue))
            return
        self.stopping = False
        self.player = self.thread = None
        self.show_count()
        self.refresh_levels()
        if self.closing and not issue:
            self.root.destroy()
            return True
        was_closing = self.closing
        self.closing = False
        self.exit.configure(text="退出")
        self.exit.state(["!disabled"])
        self.play.configure(text="AP2 直推")
        self.scan.state(["!disabled"])
        self.play.state(["!disabled"] if self.catalog.can_play(self.network_info) else ["disabled"])
        if was_closing:
            self.monitor_stop = threading.Event()
            self.watch_network()
        if self.pending_scan is not None and not (self.state_dir / "restore.json").exists():
            network, auto_start = self.pending_scan
            self.pending_scan = None
            self.queue_scan(network, auto_start)
        elif issue:
            self.pending_scan = None
            self.status.set(f"播放已停止：{issue}")
            if self.root.state() == "withdrawn":
                self.show_window()
            messagebox.showerror(APP_NAME, issue, parent=self.root)
        else:
            self.status.set("已恢复电脑声音。可重新搜索音箱。")

    def poll(self):
        while True:
            try:
                event, value = self.events.get_nowait()
            except Empty:
                break
            if event == "network":
                self.network_changed(value)
            elif event == "network-error":
                self.network_changed(None)
                self.status.set(f"无法读取当前网络：{value}")
            elif event == "scan-result":
                self.scan_result(value)
            elif event == "system-volume":
                self.refresh_levels()
            elif event == "playing":
                if (self.closing or not self.catalog.can_play(self.network_info)
                        or self.player is None or self.player.stop.is_set()):
                    if self.player is not None:
                        self.player.stop.set()
                else:
                    self.playing = True
                    self.play.configure(text="AP2 播放中")
                    self.status.set("电脑音频正在全屋播放 · 系统音量等比例同步")
                    self.show_count(value)
                    self.refresh_levels()
            elif event == "stopped":
                self.playing = False
                self.stopping = True
                self.play.state(["disabled"])
                if self.finish_stop(value):
                    return
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
    mode.add_argument("--discover", action="store_true", help="read-only discovery on the current local network")
    opts = parser.parse_args()
    if opts.discover:
        from dataclasses import asdict
        from discovery import current_network, discover_speakers
        network = current_network()
        devices = discover_speakers(network, threading.Event())
        print(json.dumps({"network": asdict(network), "speakers": [asdict(s) for s in devices]},
                         ensure_ascii=False, indent=2))
        return
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
                    app.monitor_stop.set()
                    app.scan_stop.set()
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
        if any(arg in ("--check", "--restore", "--discover") for arg in sys.argv):
            raise SystemExit(str(error)) from None
        ctypes.windll.user32.MessageBoxW(None, str(error), APP_NAME, 0x10)
        raise SystemExit(1) from None

