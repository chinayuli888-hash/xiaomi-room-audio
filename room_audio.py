"""Room Audio, the Windows panel for Wi-Fi speakers."""
import argparse
import ctypes
import json
import logging
import math
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
from speaker_preferences import MAX_RATIO, PreferencesError

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
        from speaker_preferences import SpeakerPreferences

        self.root, self.base_settings, self.state_dir = root, settings, state_dir
        self.settings = settings.for_speakers(())
        self.catalog = DeviceCatalog()
        self.network_info = None
        self.scan_stop = threading.Event()
        self.monitor_stop = threading.Event()
        self.scanning = self.stopping = False
        self.pending_scan = None
        self.balances = {}
        self.preferences = SpeakerPreferences(state_dir / "speaker_profiles.json")
        self.profiles = self.preferences.profiles
        self.selection, self.choice_vars = {}, {}
        self.requested_ids = None
        self.save_after = None
        self.preferences_failed = False
        self.balance_save_failed = False
        self.preferences_issue = ""
        self.mix_devices = ()
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
        root.bind("<Control-q>", lambda event: self.exit_app())

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
        list_area = tk.Frame(main, bg=BG)
        list_area.pack(fill="both", expand=True)
        self.list_canvas = tk.Canvas(list_area, bg=BG, height=47, highlightthickness=0)
        self.list_canvas.pack(side="left", fill="both", expand=True)
        self.list_scroll = ttk.Scrollbar(list_area, orient="vertical", command=self.list_canvas.yview)
        self.list_canvas.configure(yscrollcommand=self.list_scroll.set)
        self.speaker_list = tk.Frame(self.list_canvas, bg=BG)
        list_window = self.list_canvas.create_window((0, 0), window=self.speaker_list, anchor="nw")
        self.speaker_list.bind("<Configure>", lambda _: self.list_canvas.configure(scrollregion=self.list_canvas.bbox("all")))
        self.list_canvas.bind("<Configure>", lambda event: self.list_canvas.itemconfigure(list_window, width=event.width))
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
        self.choice_vars.clear()
        for widget in self.speaker_list.winfo_children():
            widget.destroy()
        for speaker in self.catalog.devices:
            host = speaker.host
            row = tk.Frame(self.speaker_list, bg=ROW, padx=11, pady=6)
            row.pack(fill="x", pady=(0, 5))
            row.columnconfigure(2, weight=1)
            choice = tk.BooleanVar(value=self.selection.get(speaker.identifier, False))
            check = ttk.Checkbutton(row, variable=choice, style="Speaker.TCheckbutton",
                                    command=lambda identity=speaker.identifier: self.choice_changed(identity))
            check.grid(row=0, column=0, padx=(0, 8))
            self.choice_vars[speaker.identifier] = (choice, check)
            profile = self.profiles.get(speaker.identifier, {})
            name = (profile.get("name") if isinstance(profile, dict) else None) or speaker.name
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
            for widget in row.winfo_children():
                widget.bind("<MouseWheel>", self.scroll_devices)
            row.bind("<MouseWheel>", self.scroll_devices)

        if not self.rows:
            tk.Label(self.speaker_list, text="尚未发现当前网络的音箱", bg=ROW, fg=DIM,
                     font=(FONT, 10), pady=14).pack(fill="x")
        self.root.update_idletasks()
        content_height = self.speaker_list.winfo_reqheight()
        children = self.speaker_list.winfo_children()
        first_six = children[5].winfo_y() + children[5].winfo_height() + 5 if len(self.rows) > 6 else content_height
        visible_height = min(6 * 56, max(47, first_six))
        self.list_canvas.configure(height=visible_height)
        if content_height > visible_height:
            self.list_scroll.pack(side="right", fill="y")
        else:
            self.list_scroll.pack_forget()
        self.root.update_idletasks()
        width = max(452, self.root.winfo_width(), self.root.winfo_reqwidth())
        self.root.geometry(f"{width}x{max(260, self.root.winfo_reqheight())}")
        logging.info("Panel rendered: rows=%d selected=%d", len(self.rows), len(self.selected_speakers()))

    def scroll_devices(self, event):
        if self.speaker_list.winfo_reqheight() > self.list_canvas.winfo_height():
            self.list_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

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
        style.configure("Speaker.TCheckbutton", background=ROW, foreground=GREEN,
                        indicatorbackground=BG, indicatorforeground=GREEN)
        style.map("Speaker.TCheckbutton", background=[("active", ROW)],
                  indicatorbackground=[("selected", GREEN), ("disabled", ROW)])

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
        try:
            ratios = self.mix.ratios()
            updates = {speaker.identifier: ratios[speaker.host] for speaker in self.mix_devices
                       if speaker.host in ratios}
            # Validate the whole snapshot before mutating any saved device record.
            if any(isinstance(ratio, bool) or not math.isfinite(ratio) or not 0 <= ratio <= MAX_RATIO
                   for ratio in updates.values()):
                raise ValueError(f"当前比例超出可保存范围 0–{MAX_RATIO:g}，请降低单台比例后重试。")
            for identity in updates:
                if not isinstance(self.profiles.get(identity, {}), dict):
                    raise PreferencesError("设备档案格式无法安全修改，原有内容已保留。")
            for identity, ratio in updates.items():
                self.balances[identity] = ratio
                self.preferences.set_ratio(identity, ratio)
            self.balance_save_failed = False
            return True
        except (ValueError, TypeError, PreferencesError) as exc:
            self.balance_save_failed = True
            self.preference_failure(exc)
            return False

    def preference_failure(self, error):
        self.preferences_failed = True
        self.preferences_issue = str(error)
        logging.error("Could not save speaker preferences: %s", error)
        self.status.set(f"设置未保存：{error}")
        self.play.state(["disabled"])

    def save_preferences(self):
        if self.save_after is not None:
            try:
                self.root.after_cancel(self.save_after)
            except tk.TclError:
                pass
            self.save_after = None
        if getattr(self, "balance_save_failed", False):
            return False
        try:
            self.preferences.save()
            self.preferences_failed = False
            self.preferences_issue = ""
            return True
        except (OSError, ValueError, PreferencesError) as exc:
            self.preference_failure(exc)
            return False

    def schedule_preferences_save(self):
        if self.save_after is not None:
            self.root.after_cancel(self.save_after)
        self.save_after = self.root.after(400, self.save_preferences)

    def selected_speakers(self):
        return tuple(speaker for speaker in self.catalog.devices if self.selection.get(speaker.identifier, False))

    def update_play_state(self):
        available = (not self.closing and not self.scanning and not self.stopping
                     and not (self.thread and self.thread.is_alive())
                     and not self.preferences_failed and self.catalog.can_play(self.network_info)
                     and 1 <= len(self.selected_speakers()) <= 8)
        self.play.state(["!disabled"] if available else ["disabled"])

    def apply_selection(self):
        from engine import SpeakerMix
        devices = self.selected_speakers()
        if len(devices) > 8:
            devices = ()
        self.settings = self.base_settings.for_speakers(devices, self.profiles)
        self.mix_devices = tuple(devices)
        self.mix = SpeakerMix(self.settings.hosts)
        self.mix.restore_ratios({speaker.host: self.preferences.ratio(speaker.identifier) for speaker in devices})
        self.balance_save_failed = False
        self.update_play_state()

    def choice_changed(self, identifier, selected=None):
        if self.closing or self.stopping or self.scanning or (self.thread and self.thread.is_alive()):
            return
        if identifier not in {speaker.identifier for speaker in self.catalog.devices}:
            return
        if selected is None:
            selected = self.choice_vars[identifier][0].get()
        self.selection[identifier] = bool(selected)
        self.preferences.set_selected(identifier, bool(selected))
        self.apply_selection()
        if self.save_preferences():
            self.update_play_state()
        self.show_count()
        self.refresh_levels()
        if not self.preferences_failed:
            count = len(self.selected_speakers())
            self.status.set("请勾选参与播放的音箱。" if not count else
                            "一次最多选择 8 台，请取消多余勾选。" if count > 8 else
                            f"已选择 {count} 台，点击 AP2 直推开始播放。")

    def clear_devices(self, network):
        self.remember_balance()
        self.save_preferences()
        self.scan_stop.set()
        self.catalog.invalidate(network)
        self.scanning = False
        self.settings = self.base_settings.for_speakers(())
        self.selection.clear()
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
        self.requested_ids = None
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
            self.selection = {speaker.identifier: (speaker.identifier in (self.requested_ids or ()) if auto_start
                              else self.preferences.selected(speaker.identifier)) for speaker in self.catalog.devices}
            for identity, selected in self.selection.items():
                self.preferences.set_selected(identity, selected)
            self.apply_selection()
            logging.info("Discovery complete: generation=%d receivers=%d selected=%d",
                         token, len(self.catalog.devices), len(self.selected_speakers()))
            self.render_devices()
            self.show_count()
            self.refresh_levels()
            selected = len(self.selected_speakers())
            self.status.set("未发现本地音箱，请确认处于同一局域网且已开启 AirPlay。" if not self.rows else
                            "请勾选参与播放的音箱。" if not selected else
                            "一次最多选择 8 台，请取消多余勾选。" if selected > 8 else
                            f"已选择 {selected} 台，点击 AP2 直推开始播放。")
            if self.save_preferences():
                self.update_play_state()
                if auto_start and 1 <= selected <= 8:
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
        total, selected = len(self.rows), len(self.selected_speakers())
        self.count.set(f"已选 {selected}/{total} 台" if online is None else f"{online}/{selected} 台播放")

    def refresh_levels(self):
        master, gains = self.mix.snapshot()
        playing = self.playing and not self.closing
        adjustable = playing and not master.muted and master.scalar > 0
        selection_locked = self.closing or self.scanning or self.stopping or (self.thread and self.thread.is_alive())
        for choice, check in self.choice_vars.values():
            check.state(["disabled"] if selection_locked else ["!disabled"])
        self.refreshing_levels = True
        try:
            for host, (label, value, slider) in self.rows.items():
                amount = gains.get(host, 0) * 100 if playing else 0
                value.set(amount)
                label.set(f"{round(amount)}%" if playing and host in gains else "—")
                slider.state(["!disabled"] if adjustable and host in gains else ["disabled"])
        finally:
            self.refreshing_levels = False

    def adjust(self, host, amount):
        if not self.playing or self.closing or self.refreshing_levels:
            return
        self.mix.set_speaker_percent(host, float(amount))
        self.remember_balance()
        self.schedule_preferences_save()
        self.refresh_levels()

    def start(self):
        if self.closing or self.scanning or self.stopping or (self.thread and self.thread.is_alive()):
            return
        devices = self.selected_speakers()
        if not 1 <= len(devices) <= 8:
            return
        self.requested_ids = frozenset(speaker.identifier for speaker in devices)
        # Reconfirm every endpoint before connecting, even on the same SSID.
        self.search(auto_start=True)

    def begin_playback(self, network):
        from engine import Player

        if (self.closing or self.stopping or self.scanning
                or (self.thread and self.thread.is_alive())
                or not self.catalog.can_play(network)
                or not self.catalog.can_play(self.network_info)
                or self.preferences_failed or not 1 <= len(self.settings.hosts) <= 8):
            return
        self.play.configure(text="正在连接…")
        self.play.state(["disabled"])
        self.status.set("正在连接本次搜索到的音箱…")
        self.player = Player(self.settings, self.state_dir, self.emit, self.mix, network)
        self.thread = threading.Thread(target=self.player.run, name="playback")
        self.thread.start()
        self.refresh_levels()

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
        self.remember_balance()
        self.save_preferences()
        self.closing = True
        self.pending_scan = None
        self.scan_stop.set()
        self.monitor_stop.set()
        self.catalog.invalidate()
        if not self.thread or not self.thread.is_alive():
            self.warn_unsaved()
            self.root.destroy()
            return
        self.refresh_levels()
        self.play.state(["disabled"])
        self.exit.configure(text="正在退出…")
        self.exit.state(["disabled"])
        self.player.stop.set()

    def warn_unsaved(self):
        if self.preferences_failed:
            messagebox.showwarning(APP_NAME, "本次音箱设置未能保存；已有档案已保留。\n" +
                                   getattr(self, "preferences_issue", ""), parent=self.root)

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
            self.warn_unsaved()
            self.root.destroy()
            return True
        was_closing = self.closing
        self.closing = False
        self.exit.configure(text="退出")
        self.exit.state(["!disabled"])
        self.play.configure(text="AP2 直推")
        self.scan.state(["!disabled"])
        self.update_play_state()
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
                self.remember_balance()
                self.save_preferences()
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

