#!/usr/bin/env python3
"""Track Player GUI — a thin Tkinter shell over the trackplayer core.

pip-distributed (no exe). Launch with `trackplayer-gui` or `trackplayer gui`.
All blocking work (device detection, playback, clear) runs in worker threads;
the UI thread only runs the Tk mainloop. Core log lines are marshalled back to
the UI thread via root.after(0, ...). Closing the window while playing stops
playback and restores real location before exiting.
"""

import sys
import threading
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext
    HAS_TK = True
except ImportError:
    HAS_TK = False

import trackprep
import trackplayer as tp
import trackplayer_config as config

MAX_LOG_LINES = 2000
IDLE, PLAYING = 'idle', 'playing'


class App:
    def __init__(self, root):
        self.root = root
        self.state = IDLE
        self.devices = []
        self.stop_event = None
        self.worker = None
        self._alive = True

        root.title('Track Player')
        root.geometry('640x600')
        root.minsize(560, 520)
        root.protocol('WM_DELETE_WINDOW', self.on_close)

        pad = {'padx': 8, 'pady': 4}
        main = ttk.Frame(root)
        main.pack(fill='both', expand=True)

        # --- device ---
        dev = ttk.LabelFrame(main, text='设备')
        dev.pack(fill='x', **pad)
        row = ttk.Frame(dev)
        row.pack(fill='x', padx=6, pady=6)
        self.refresh_btn = ttk.Button(row, text='刷新设备', command=self.on_refresh)
        self.refresh_btn.pack(side='left')
        self.device_combo = ttk.Combobox(row, state='readonly', width=52)
        self.device_combo.pack(side='left', padx=8, fill='x', expand=True)
        self.device_hint = ttk.Label(
            dev, foreground='#a00',
            text='未检测到 iPhone：装 iTunes(usbmux 服务) → 开开发者模式 → USB 连接并「信任此电脑」(iOS 17.4+)。')
        self.device_hint.pack(fill='x', padx=6, pady=(0, 6))

        # --- track ---
        trk = ttk.LabelFrame(main, text='轨迹')
        trk.pack(fill='x', **pad)
        row = ttk.Frame(trk)
        row.pack(fill='x', padx=6, pady=6)
        self.path_var = tk.StringVar()
        self.path_entry = ttk.Entry(row, textvariable=self.path_var)
        self.path_entry.pack(side='left', fill='x', expand=True)
        ttk.Button(row, text='浏览…', command=self.on_browse).pack(side='left', padx=8)
        self.summary_var = tk.StringVar(value='未选择文件')
        ttk.Label(trk, textvariable=self.summary_var, foreground='#060').pack(fill='x', padx=6, pady=(0, 6))

        # --- params ---
        par = ttk.LabelFrame(main, text='参数')
        par.pack(fill='x', **pad)
        cfg = config.load_config()
        preset_row = ttk.Frame(par)
        preset_row.pack(fill='x', padx=6, pady=(6, 0))
        ttk.Label(preset_row, text='速度预设').pack(side='left')
        self.preset_combo = ttk.Combobox(preset_row, state='readonly', width=22)
        self.preset_combo.pack(side='left', padx=8)
        self.preset_combo.bind('<<ComboboxSelected>>', self.on_preset)
        ttk.Label(preset_row, text='选预设自动填入定速，可再微调', foreground='#666').pack(side='left')
        self._load_presets(cfg)
        grid = ttk.Frame(par)
        grid.pack(fill='x', padx=6, pady=6)
        self.mode_var = tk.StringVar(value='speed')
        ttk.Radiobutton(grid, text='定速 (m/s)', variable=self.mode_var, value='speed',
                        command=self.on_mode).grid(row=0, column=0, sticky='w')
        self.speed_var = tk.StringVar(value=f"{cfg.get('default_speed_mps', 3.0):g}")
        self.speed_spin = ttk.Spinbox(grid, from_=0.5, to=20, increment=0.5, width=8,
                                      textvariable=self.speed_var, command=self.refresh_summary)
        self.speed_spin.grid(row=0, column=1, sticky='w', padx=4)
        ttk.Radiobutton(grid, text='倍速 (×)', variable=self.mode_var, value='scale',
                        command=self.on_mode).grid(row=0, column=2, sticky='w', padx=(16, 0))
        self.scale_var = tk.StringVar(value=f"{cfg.get('default_scale', 2.0):g}")
        self.scale_spin = ttk.Spinbox(grid, from_=0.1, to=20, increment=0.5, width=8,
                                      textvariable=self.scale_var, command=self.refresh_summary)
        self.scale_spin.grid(row=0, column=3, sticky='w', padx=4)

        ttk.Label(grid, text='循环次数').grid(row=1, column=0, sticky='w', pady=4)
        self.repeat_var = tk.StringVar(value=f"{cfg.get('default_repeat', 1):g}")
        ttk.Spinbox(grid, from_=1, to=100, increment=1, width=8,
                    textvariable=self.repeat_var, command=self.refresh_summary).grid(row=1, column=1, sticky='w', padx=4)
        self.gcj_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(grid, text='源是 GCJ-02（高德/腾讯）', variable=self.gcj_var,
                        command=self.refresh_summary).grid(row=1, column=2, columnspan=2, sticky='w', padx=(16, 0))
        ttk.Label(grid, text='时长 (秒，空=手动停止)').grid(row=2, column=0, columnspan=2, sticky='w', pady=4)
        self.duration_var = tk.StringVar()
        ttk.Entry(grid, textvariable=self.duration_var, width=10).grid(row=2, column=2, sticky='w', padx=(16, 0))
        self.on_mode()

        # --- controls ---
        ctl = ttk.Frame(main)
        ctl.pack(fill='x', **pad)
        self.play_btn = ttk.Button(ctl, text='播放', command=self.on_play)
        self.play_btn.pack(side='left')
        self.stop_btn = ttk.Button(ctl, text='停止', command=self.on_stop, state='disabled')
        self.stop_btn.pack(side='left', padx=8)
        self.restore_btn = ttk.Button(ctl, text='恢复真实定位', command=self.on_restore)
        self.restore_btn.pack(side='left')
        self.status_var = tk.StringVar(value='空闲')
        ttk.Label(ctl, textvariable=self.status_var, font=('', 10, 'bold')).pack(side='right')

        # --- log ---
        logf = ttk.LabelFrame(main, text='日志')
        logf.pack(fill='both', expand=True, **pad)
        self.log = scrolledtext.ScrolledText(logf, height=12, state='disabled', wrap='word')
        self.log.pack(fill='both', expand=True, padx=6, pady=6)

        self.on_refresh()

    # ---- threading helpers ----
    def log_async(self, line):
        if not self._alive:
            return
        try:
            self.root.after(0, lambda: self._append_log(line))
        except (tk.TclError, RuntimeError):
            pass

    def _append_log(self, line):
        self.log.configure(state='normal')
        self.log.insert('end', line + '\n')
        n = int(self.log.index('end-1c').split('.')[0])
        if n > MAX_LOG_LINES:
            self.log.delete('1.0', f'{n - MAX_LOG_LINES}.0')
        self.log.see('end')
        self.log.configure(state='disabled')

    def ui(self, fn):
        if not self._alive:
            return
        try:
            self.root.after(0, fn)
        except (tk.TclError, RuntimeError):
            pass

    # ---- state ----
    def set_state(self, state, status=None):
        self.state = state
        playing = state == PLAYING
        self.play_btn.configure(state='disabled' if playing else 'normal')
        self.stop_btn.configure(state='normal' if playing else 'disabled')
        self.restore_btn.configure(state='disabled' if playing else 'normal')
        if status:
            self.status_var.set(status)

    # ---- devices ----
    def selected_udid(self):
        i = self.device_combo.current()
        if 0 <= i < len(self.devices):
            return self.devices[i].get('Identifier')
        return None

    def on_refresh(self):
        self._load_presets()  # hot-reload speed presets from config on each refresh
        self.refresh_btn.configure(state='disabled')
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self):
        try:
            devs = tp.list_devices()
        except Exception as e:
            self.log_async(str(e))
            devs = []
        self.ui(lambda: self._set_devices(devs))

    def _set_devices(self, devs):
        self.devices = devs
        values = [f"{d.get('Identifier', '?')} — {d.get('DeviceName', '')} — iOS {d.get('ProductVersion', '')}"
                  for d in devs]
        self.device_combo['values'] = values
        if values:
            self.device_combo.current(0)
            self.device_hint.pack_forget()
        else:
            self.device_combo.set('')
            self.device_hint.pack(fill='x', padx=6, pady=(0, 6))
        self.refresh_btn.configure(state='normal')

    # ---- track / params ----
    def on_browse(self):
        path = filedialog.askopenfilename(
            title='选择轨迹文件', filetypes=[('GPX/KML 轨迹', '*.gpx *.kml'), ('所有文件', '*.*')])
        if path:
            self.path_var.set(path)
            self.refresh_summary()

    def _load_presets(self, cfg=None):
        cfg = cfg if cfg is not None else config.load_config()
        self._presets = config.speed_presets(cfg)
        values = [f"{p.get('label', '?')} ({p['kmh']:g} km/h)" for p in self._presets]
        values.append('自定义…')
        self.preset_combo['values'] = values
        if cfg.get('_error'):
            self.log_async(cfg['_error'])

    def on_preset(self, event=None):
        i = self.preset_combo.current()
        self.mode_var.set('speed')
        if 0 <= i < len(self._presets):
            self.speed_var.set(f"{config.preset_speed_mps(self._presets[i]):.2f}")
        self.on_mode()

    def on_mode(self):
        speed = self.mode_var.get() == 'speed'
        self.speed_spin.configure(state='normal' if speed else 'disabled')
        self.scale_spin.configure(state='disabled' if speed else 'normal')
        self.refresh_summary()

    def _params(self):
        speed = scale = None
        if self.mode_var.get() == 'speed':
            speed = float(self.speed_var.get())
        else:
            scale = float(self.scale_var.get())
        repeat = int(self.repeat_var.get())
        dur_text = self.duration_var.get().strip()
        duration = float(dur_text) if dur_text else None
        return speed, scale, repeat, duration

    def refresh_summary(self):
        path = self.path_var.get().strip()
        if not path:
            self.summary_var.set('未选择文件')
            return
        try:
            speed, scale, repeat, _ = self._params()
            points = trackprep.prepare(path, speed, scale, repeat, self.gcj_var.get())
            dist = sum(trackprep.haversine(a[0], a[1], b[0], b[1]) for a, b in zip(points, points[1:]))
            dur = (points[-1][2] - points[0][2]).total_seconds()
            self.summary_var.set(f'{len(points)} 点 | {dist / 1000:.2f} km | {dur:.0f} s 播放')
        except SystemExit as e:
            self.summary_var.set(f'解析失败：{e}')
        except Exception as e:
            self.summary_var.set(f'解析失败：{e}')

    # ---- play / stop / restore ----
    def on_play(self):
        if self.state != IDLE:
            return
        path = self.path_var.get().strip()
        if not path:
            messagebox.showwarning('Track Player', '请先选择轨迹文件。')
            return
        if not Path(path).exists():
            messagebox.showerror('Track Player', f'文件不存在：{path}')
            return
        try:
            speed, scale, repeat, duration = self._params()
        except ValueError:
            messagebox.showerror('Track Player', '参数有误：速度/倍速/时长需为数字，循环次数需为整数。')
            return
        udid = self.selected_udid()
        self.stop_event = threading.Event()
        self.set_state(PLAYING, '播放中')
        self.worker = threading.Thread(
            target=self._play_worker,
            args=(path, speed, scale, repeat, duration, udid), daemon=True)
        self.worker.start()

    def _play_worker(self, path, speed, scale, repeat, duration, udid):
        ok = False
        try:
            tp.play_track(path, speed=speed, scale=scale, repeat=repeat,
                          from_gcj02=self.gcj_var.get(), udid=udid, duration=duration,
                          stop_event=self.stop_event, on_log=self.log_async,
                          notice='playing... (点击「停止」提前结束并恢复)')
            ok = True
        except (ValueError, RuntimeError) as e:
            self.log_async(str(e))
            self.ui(lambda: messagebox.showerror('Track Player', str(e)))
        except Exception as e:
            self.log_async(f'error: {e}')
            self.ui(lambda: messagebox.showerror('Track Player', f'error: {e}'))
        finally:
            self.ui(lambda: self.set_state(IDLE, '已恢复' if ok else '空闲'))

    def on_stop(self):
        if self.state == PLAYING and self.stop_event:
            self.status_var.set('正在恢复')
            self.stop_event.set()

    def on_restore(self):
        if self.state != IDLE:
            return
        udid = self.selected_udid()
        self.status_var.set('正在恢复')
        self.restore_btn.configure(state='disabled')
        threading.Thread(target=self._restore_worker, args=(udid,), daemon=True).start()

    def _restore_worker(self, udid):
        self.log_async('restoring real location...')
        try:
            tp.restore(udid)
            self.log_async('real location restored.')
        except Exception as e:
            self.log_async(f'error: {e}')
        self.ui(lambda: self.set_state(IDLE, '已恢复'))

    # ---- close ----
    def on_close(self):
        # Disable UI marshalling first: the worker's finally calls on_log/ui,
        # which use root.after and would block once the mainloop stops here.
        self._alive = False
        if self.state == PLAYING and self.stop_event:
            self.stop_event.set()
            if self.worker:
                self.worker.join(timeout=8)
        self.root.destroy()


def main():
    if not HAS_TK:
        sys.exit('error: tkinter is not available. Install Python from python.org '
                 '(the Windows installer includes tcl/tk).')
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()