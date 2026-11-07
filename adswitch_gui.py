"""AdSwitch 1.1 — коли в десктопному Spotify реклама, грає SoundCloud. Windows."""
import asyncio, glob, json, os, queue, re, shutil, socket, subprocess, sys, threading, time, urllib.request, webbrowser
import customtkinter as ctk

APP, VERSION = "AdSwitch", "1.1.0"
# Для автооновлень: URL файлу JSON вигляду {"version": "1.2.0", "url": "https://...AdSwitch-Setup.exe"}
UPDATE_URL = ""
DATA = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP)
BIN, CFG = os.path.join(DATA, "bin"), os.path.join(DATA, "settings.json")
PIPE = r"\\.\pipe\adswitch"
NOWIN = 0x08000000
YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
DEFAULTS = {"sc": "", "sp": "", "artists": "spotify", "titles": "advertisement, spotify, spotify free, реклама",
            "mode": "fade", "fade": 2.0, "max": 180, "autostart": False}
MODES = {"Плавний перехід": "fade", "Доіграти пісню SoundCloud": "finish"}

BG, CARD, TXT, DIM = "#0e1318", "#171e25", "#e8eef2", "#7d8c98"
COL = {"idle": "#5b6874", "wait": "#e0a800", "ok": "#1db954", "ad": "#ff6a1a"}


# ---------- компоненти ----------
def _env(k): return os.environ.get(k, "")


def find_exe(name, extra):
    p = shutil.which(name)
    if p:
        return p
    for pat in extra:
        h = glob.glob(pat, recursive=True)
        if h:
            return h[0]


def find_mpv():
    return find_exe("mpv", [os.path.join(BIN, "mpv.exe"),
                            _env("LOCALAPPDATA") + r"\Microsoft\WinGet\Links\mpv.exe",
                            _env("LOCALAPPDATA") + r"\Microsoft\WinGet\Packages\**\mpv.exe",
                            _env("ProgramFiles") + r"\mpv*\mpv.exe",
                            os.path.expanduser("~") + r"\scoop\apps\mpv\current\mpv.exe"])


def find_ytdlp():
    return find_exe("yt-dlp", [os.path.join(BIN, "yt-dlp.exe"),
                               _env("LOCALAPPDATA") + r"\Microsoft\WinGet\Packages\**\yt-dlp.exe",
                               os.path.join(os.path.dirname(sys.executable), "Scripts", "yt-dlp.exe")])


def ensure_components(log):
    own = os.path.join(BIN, "yt-dlp.exe")
    if not find_ytdlp():
        log("Завантажую yt-dlp…")
        os.makedirs(BIN, exist_ok=True)
        urllib.request.urlretrieve(YTDLP_URL, own)
    elif os.path.exists(own):  # раз на добу оновлюємо власну копію: SoundCloud часто ламає старі версії
        mark = os.path.join(DATA, "ytdlp_checked")
        if not os.path.exists(mark) or time.time() - os.path.getmtime(mark) > 86400:
            subprocess.run([own, "-U"], capture_output=True, creationflags=NOWIN, timeout=90)
            open(mark, "w").close()
    if not find_mpv():
        log("Встановлюю mpv через winget (до хвилини)…")
        subprocess.run(["winget", "install", "--id", "shinchiro.mpv", "--source", "winget", "-e",
                        "--accept-source-agreements", "--accept-package-agreements"],
                       capture_output=True, creationflags=NOWIN)
        if not find_mpv():
            raise RuntimeError("Не вдалося поставити mpv. Виконай вручну: winget install mpv")
    return find_mpv(), find_ytdlp()


# ---------- Spotify (звук і керування) ----------
def _sp_sessions():
    from pycaw.pycaw import AudioUtilities
    for s in AudioUtilities.GetAllSessions():
        try:
            if s.Process and s.Process.name().lower() == "spotify.exe":
                yield s.SimpleAudioVolume
        except Exception:
            pass


def sp_mute(m):
    for v in _sp_sessions(): v.SetMute(1 if m else 0, None)


def sp_vol(level):
    for v in _sp_sessions(): v.SetMasterVolume(max(0.0, min(1.0, level)), None)


def sp_get_vol():
    for v in _sp_sessions(): return v.GetMasterVolume()
    return 1.0


async def _smtc():
    from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as M
    mgr = await M.request_async()
    for s in mgr.get_sessions():
        if "spotify" in s.source_app_user_model_id.lower():
            return s


async def sp_now():
    s = await _smtc()
    if not s:
        return None
    p = await s.try_get_media_properties_async()
    return p.artist or "", p.title or ""


async def sp_control(action):
    s = await _smtc()
    if s:
        await (s.try_pause_async() if action == "pause" else s.try_play_async())


class Mpv:
    def __init__(self): self.f, self.rid = None, 0

    def open(self):
        for _ in range(50):
            try:
                self.f = open(PIPE, "r+b", buffering=0)
                return True
            except OSError:
                time.sleep(0.2)
        return False

    def cmd(self, *c):
        self.rid += 1
        try:
            self.f.write((json.dumps({"command": list(c), "request_id": self.rid}) + "\n").encode())
            while True:
                line = self.f.readline()
                if not line:
                    return None
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("request_id") == self.rid:
                    return d
        except OSError:
            return None

    def set(self, k, v): self.cmd("set_property", k, v)

    def get(self, k):
        d = self.cmd("get_property", k)
        return d.get("data") if d and d.get("error") == "success" else None


class Engine:
    def __init__(self, log, status):
        self.log, self.status = log, status
        self.stop_ev, self.thread, self.proc, self.mpv, self.task, self.vol = threading.Event(), None, None, None, None, 1.0

    def running(self): return bool(self.thread and self.thread.is_alive())

    def start(self, cfg):
        self.stop_ev.clear()
        self.thread = threading.Thread(target=self._thread, args=(cfg,), daemon=True)
        self.thread.start()

    def stop(self): self.stop_ev.set()

    def _thread(self, cfg):
        import comtypes
        comtypes.CoInitialize()
        try:
            asyncio.run(self._run(cfg))
        except Exception as e:
            self.log(f"Помилка: {e}")
        finally:
            try:
                sp_mute(False)
                sp_vol(self.vol)
                if self.proc and self.proc.poll() is None:
                    self.proc.terminate()
            except Exception:
                pass
            self.status("idle", "Зупинено")
            comtypes.CoUninitialize()

    async def _ramp_in(self, sec):
        self.mpv.set("volume", 0)
        self.mpv.set("pause", False)
        n = max(1, int(sec * 10))
        for i in range(1, n + 1):
            self.mpv.set("volume", round(100 * i / n))
            await asyncio.sleep(0.1)

    async def _crossfade(self, sec):
        n = max(1, int(sec * 10))
        for i in range(1, n + 1):
            k = i / n
            self.mpv.set("volume", round(100 * (1 - k)))
            sp_vol(self.vol * k)
            await asyncio.sleep(0.1)
        self.mpv.set("pause", True)

    async def _after_ad(self, cfg):
        sec = float(cfg["fade"])
        sp_vol(0)
        sp_mute(False)
        if cfg["mode"] == "finish":
            await sp_control("pause")
            self.status("ad", "Доігруємо пісню SoundCloud…")
            waited = 0.0
            while not self.stop_ev.is_set() and waited < float(cfg["max"]):
                tr = await asyncio.to_thread(self.mpv.get, "time-remaining")
                if not isinstance(tr, (int, float)) or tr <= sec:
                    break
                await asyncio.sleep(0.5)
                waited += 0.5
            await sp_control("play")
        await self._crossfade(sec)
        self.status("ok", "Грає Spotify")

    def _spawn(self, coro):
        if self.task and not self.task.done():
            self.task.cancel()
        self.task = asyncio.create_task(coro)

    async def _run(self, cfg):
        csv = lambda t: {x.strip().lower() for x in t.split(",") if x.strip()}
        artists, titles = csv(cfg["artists"]), csv(cfg["titles"])
        is_ad = lambda a, t: t.strip().lower() in titles or a.strip().lower() in artists
        self.status("wait", "Готую компоненти…")
        mpv, yt = await asyncio.to_thread(ensure_components, self.log)
        if cfg["sp"]:
            m = re.search(r"playlist/([A-Za-z0-9]+)", cfg["sp"])
            os.startfile(f"spotify:playlist:{m.group(1)}" if m else cfg["sp"])
        self.proc = subprocess.Popen(
            [mpv, "--no-video", "--pause", "--idle=yes", "--keep-open=yes", "--loop-playlist=inf", "--shuffle",
             f"--script-opts=ytdl_hook-ytdl_path={yt}", f"--input-ipc-server={PIPE}", cfg["sc"]],
            creationflags=NOWIN)
        self.mpv = Mpv()
        if not await asyncio.to_thread(self.mpv.open):
            raise RuntimeError("mpv не запустився. Перевір, що плейліст SoundCloud публічний.")
        self.mpv.set("volume", 0)
        self.vol = sp_get_vol()
        in_ad, last = False, None
        self.status("ok", "Стежу за Spotify")
        while not self.stop_ev.is_set():
            info = await sp_now()
            if info is None:
                self.status("wait", "Spotify не запущено")
            else:
                if info != last:
                    self.log(f"{info[0] or '—'} — {info[1] or '—'}")
                    last = info
                ad = is_ad(*info)
                if ad and not in_ad:
                    if self.task and not self.task.done():
                        self.task.cancel()
                    self.vol = sp_get_vol() or self.vol
                    if self.vol < 0.05:
                        self.vol = 1.0
                    self.status("ad", "Реклама → грає SoundCloud")
                    self._spawn(self._ramp_in(1.5))
                if ad:
                    sp_mute(True)
                if not ad and in_ad:
                    self._spawn(self._after_ad(cfg))
                in_ad = ad
            await asyncio.sleep(0.5)


# ---------- автозапуск, оновлення ----------
def set_autostart(on):
    if not getattr(sys, "frozen", False):
        return False
    import winreg
    k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
    if on:
        winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, f'"{sys.executable}" --tray')
    else:
        try:
            winreg.DeleteValue(k, APP)
        except FileNotFoundError:
            pass
    return True


def check_update(q):
    if not UPDATE_URL:
        return
    try:
        d = json.load(urllib.request.urlopen(UPDATE_URL, timeout=8))
        t = lambda v: tuple(int(x) for x in v.split("."))
        if t(d["version"]) > t(VERSION):
            q.put(("update", d))
    except Exception:
        pass


# ---------- інтерфейс ----------
def tray_img(color):
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((4, 4, 60, 60), fill=color)
    d.polygon([(26, 20), (26, 44), (46, 32)], fill="white")
    return im


class App(ctk.CTk):
    def __init__(self, tray_mode):
        super().__init__(fg_color=BG)
        ctk.set_appearance_mode("dark")
        self.title(APP)
        self.geometry("480x700")
        self.minsize(440, 640)
        self.q = queue.Queue()
        self.engine = Engine(lambda m: self.q.put(("log", m)), lambda k, t: self.q.put(("st", (k, t))))
        self.cfg = dict(DEFAULTS)
        try:
            self.cfg.update(json.load(open(CFG, encoding="utf-8")))
        except Exception:
            pass
        self._ui()
        self._tray()
        self.protocol("WM_DELETE_WINDOW", self.withdraw)
        self.after(150, self._pump)
        threading.Thread(target=check_update, args=(self.q,), daemon=True).start()
        if tray_mode:
            self.withdraw()
            if self.cfg["sc"]:
                self.after(500, self._toggle)

    def _card(self, parent, title):
        c = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=16)
        c.pack(fill="x", pady=8)
        ctk.CTkLabel(c, text=title, text_color=DIM, font=("Segoe UI", 12)).pack(anchor="w", padx=16, pady=(12, 0))
        return c

    def _entry(self, parent, key, ph):
        e = ctk.CTkEntry(parent, placeholder_text=ph, height=38, corner_radius=10, fg_color=BG, border_width=0)
        e.pack(fill="x", padx=16, pady=(6, 12))
        if self.cfg.get(key):
            e.insert(0, self.cfg[key])
        return e

    def _ui(self):
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(20, 4))
        ctk.CTkLabel(head, text="AdSwitch", font=("Segoe UI Semibold", 26), text_color=TXT).pack(side="left")
        ctk.CTkLabel(head, text="v" + VERSION, text_color=DIM).pack(side="left", padx=8, pady=(10, 0))

        pill = ctk.CTkFrame(self, fg_color=CARD, corner_radius=22)
        pill.pack(fill="x", padx=20, pady=(8, 4))
        self.dot = ctk.CTkLabel(pill, text="●", font=("Segoe UI", 22), text_color=COL["idle"])
        self.dot.pack(side="left", padx=(18, 8), pady=12)
        self.stl = ctk.CTkLabel(pill, text="Зупинено", font=("Segoe UI Semibold", 16), text_color=TXT)
        self.stl.pack(side="left")

        self.upd = ctk.CTkButton(self, text="", fg_color="#2a3a2f", hover_color="#34493a", height=34, corner_radius=10)

        tabs = ctk.CTkTabview(self, fg_color="transparent", segmented_button_selected_color="#2b6df0")
        tabs.pack(fill="both", expand=True, padx=14, pady=4)
        main, adv, logt = tabs.add("Головне"), tabs.add("Додатково"), tabs.add("Журнал")

        self.e = {}
        c = self._card(main, "Плейліст SoundCloud (публічний)")
        self.e["sc"] = self._entry(c, "sc", "https://soundcloud.com/…/sets/…")
        c = self._card(main, "Плейліст Spotify (необов'язково)")
        self.e["sp"] = self._entry(c, "sp", "https://open.spotify.com/playlist/…")

        c = self._card(main, "Коли реклама закінчується")
        self.mode = ctk.CTkSegmentedButton(c, values=list(MODES), command=lambda v: None)
        self.mode.pack(fill="x", padx=16, pady=(8, 4))
        self.mode.set(next(k for k, v in MODES.items() if v == self.cfg["mode"]))
        self.fl = ctk.CTkLabel(c, text="", text_color=DIM)
        self.fl.pack(anchor="w", padx=16)
        self.fade = ctk.CTkSlider(c, from_=0.5, to=6, number_of_steps=11, command=self._fl)
        self.fade.pack(fill="x", padx=16, pady=(0, 14))
        self.fade.set(float(self.cfg["fade"]))
        self._fl(self.fade.get())

        self.btn = ctk.CTkButton(self, text="Запустити", height=48, corner_radius=14, font=("Segoe UI Semibold", 16),
                                 fg_color="#1db954", hover_color="#18a349", text_color="#06210f", command=self._toggle)
        self.btn.pack(fill="x", padx=20, pady=(4, 6))
        ctk.CTkLabel(self, text="Закриття вікна згортає програму в трей", text_color=DIM,
                     font=("Segoe UI", 11)).pack(pady=(0, 12))

        c = self._card(adv, "Автор реклами (через кому)")
        self.e["artists"] = self._entry(c, "artists", "spotify")
        c = self._card(adv, "Назва реклами (через кому)")
        self.e["titles"] = self._entry(c, "titles", "advertisement, spotify")
        c = self._card(adv, "Система")
        self.auto = ctk.CTkSwitch(c, text="Запускати разом з Windows (у треї)")
        self.auto.pack(anchor="w", padx=16, pady=(10, 14))
        if self.cfg["autostart"]:
            self.auto.select()

        self.log = ctk.CTkTextbox(logt, fg_color=CARD, corner_radius=12, text_color=DIM, wrap="word")
        self.log.pack(fill="both", expand=True, pady=6)
        self.log.configure(state="disabled")

    def _fl(self, v):
        self.fl.configure(text=f"Тривалість переходу: {float(v):.1f} с")

    def _tray(self):
        self.tray = None
        try:
            import pystray
            m = pystray.Menu(pystray.MenuItem("Відкрити", lambda: self.q.put(("cmd", "show")), default=True),
                             pystray.MenuItem("Старт / Стоп", lambda: self.q.put(("cmd", "toggle"))),
                             pystray.MenuItem("Вихід", lambda: self.q.put(("cmd", "quit"))))
            self.tray = pystray.Icon(APP, tray_img(COL["idle"]), APP, m)
            self.tray.run_detached()
        except Exception as e:
            self._log(f"Трей недоступний: {e}")

    def _toggle(self):
        if self.engine.running():
            self.engine.stop()
            return
        cfg = {k: v.get().strip() for k, v in self.e.items()}
        if not cfg["sc"].startswith("http"):
            self._log("Встав посилання на плейліст SoundCloud.")
            self.deiconify()
            return
        cfg.update(sc=cfg["sc"].split("?")[0], mode=MODES[self.mode.get()], fade=round(self.fade.get(), 1),
                   max=self.cfg["max"], autostart=bool(self.auto.get()))
        os.makedirs(DATA, exist_ok=True)
        json.dump(cfg, open(CFG, "w", encoding="utf-8"), ensure_ascii=False)
        self.cfg = cfg
        try:
            set_autostart(cfg["autostart"])
        except Exception as e:
            self._log(f"Автозапуск: {e}")
        self.btn.configure(text="Зупинити", fg_color="#3a4651", hover_color="#46535f", text_color=TXT)
        self.engine.start(cfg)

    def _log(self, m):
        self.log.configure(state="normal")
        self.log.insert("end", m + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _pump(self):
        try:
            while True:
                kind, v = self.q.get_nowait()
                if kind == "log":
                    self._log(v)
                elif kind == "st":
                    self.dot.configure(text_color=COL[v[0]])
                    self.stl.configure(text=v[1])
                    if self.tray:
                        self.tray.icon = tray_img(COL[v[0]])
                    if v[0] == "idle":
                        self.btn.configure(text="Запустити", fg_color="#1db954", hover_color="#18a349", text_color="#06210f")
                elif kind == "update":
                    self.upd.configure(text=f"Доступна версія {v['version']} — завантажити",
                                       command=lambda u=v["url"]: webbrowser.open(u))
                    self.upd.pack(fill="x", padx=20, pady=4, before=self.btn)
                elif kind == "cmd":
                    if v == "show":
                        self.deiconify(); self.lift()
                    elif v == "toggle":
                        self._toggle()
                    elif v == "quit":
                        self.engine.stop()
                        if self.tray:
                            self.tray.stop()
                        self.after(500, self.destroy)
        except queue.Empty:
            pass
        self.after(150, self._pump)


if __name__ == "__main__":
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", 47821))  # лише один екземпляр програми
    except OSError:
        sys.exit(0)
    App("--tray" in sys.argv).mainloop()
