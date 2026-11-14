# -*- coding: utf-8 -*-
"""
DingDone v3 —— AI 任务完成提醒器（推倒重来的正式版）
架构：PyQt5 原生窗口（任务栏常驻按钮=存在感）+ 已验证业务逻辑平移
教训应用：软件生产规范 v1.3 全 17 条；pywebview 层全部淘汰
"""
import os, sys, io, json, time, re, threading, subprocess, tempfile
import hashlib, socket

# ── 路径（通用化：exe/脚本所在目录即数据目录——任何人电脑可用）──
APP_DIR = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
           else os.path.dirname(os.path.abspath(__file__)))
os.makedirs(APP_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
LOG_DIR = os.path.join(APP_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
CC0_DIR = os.path.join(APP_DIR, "soundpack_cc0")
TRIGGER_PATH = os.path.join(APP_DIR, "trigger.json")

import faulthandler
faulthandler.enable(open(os.path.join(APP_DIR, "crash.log"), "w", encoding="utf-8"), all_threads=True)

def log(msg):
    try:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        with io.open(os.path.join(LOG_DIR, f"ding-{time.strftime('%Y%m%d')}.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ── 配置 ──
DEFAULT_CONFIG = {
    "sentinels": [],
    "sounds": {
        "task_done": {"enabled": True, "mode": "sound", "file": os.path.join(CC0_DIR, "11_Cheer_欢呼.wav")},
        "chat_done": {"enabled": True, "mode": "sound", "file": os.path.join(CC0_DIR, "07_Tech_Blip_科技点.wav")},
        "heartbeat": {"enabled": True, "mode": "sound", "file": os.path.join(CC0_DIR, "12_Long_Alert_长警报.wav")},
    },
    "hb_minutes": 20,
    "poll_interval": 10,
    "quiet_enabled": False,
    "quiet_hours": ["23:00", "08:00"],
    "switches": {"task_done": True, "chat_done": True, "heartbeat": True},
    "lang": "zh",
    "last_event": "",
}

def load_config():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            cfg = json.load(io.open(CONFIG_PATH, encoding="utf-8"))
        except Exception as e:
            log(f"config 解析失败，用默认: {e}")
            cfg = {}
    # 与默认合并（缺的补默认；file 指向不存在的音效 → 迁移）
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    for k, v in cfg.items():
        if k == "sounds":
            for ek, ev in v.items():
                if ek in merged["sounds"]:
                    f = ev.get("file", "")
                    f_abs = f if os.path.isabs(f) else os.path.join(APP_DIR, f)
                    if f and os.path.exists(f_abs):
                        merged["sounds"][ek].update(ev)
                    else:
                        merged["sounds"][ek]["enabled"] = ev.get("enabled", merged["sounds"][ek]["enabled"])
                        merged["sounds"][ek]["mode"] = ev.get("mode", merged["sounds"][ek]["mode"])
        elif k == "switches":
            merged["switches"].update(v)
        else:
            merged[k] = v
    # file 指向不存在的 → 重置为默认音效（禁静默回退到系统声）
    for k, v in merged["sounds"].items():
        f = v.get("file", "")
        f_abs = f if os.path.isabs(f) else os.path.join(APP_DIR, f)
        if not f or not os.path.exists(f_abs):
            v["file"] = DEFAULT_CONFIG["sounds"][k]["file"]
    io.open(CONFIG_PATH, "w", encoding="utf-8").write(json.dumps(merged, ensure_ascii=False, indent=2))
    return merged

CFG = load_config()
CFG_LOCK = threading.Lock()

def save_cfg(new_cfg):
    with CFG_LOCK:
        io.open(CONFIG_PATH, "w", encoding="utf-8").write(json.dumps(new_cfg, ensure_ascii=False, indent=2))

# ── 声音引擎（MCI——失败留日志不静默）──
import ctypes
winmm = ctypes.WinDLL("winmm")

def play_file(path):
    try:
        f = path if os.path.isabs(path) else os.path.join(APP_DIR, path)
        if not os.path.exists(f):
            log(f"音效文件不存在: {f}")
            return
        winmm.mciSendStringW('close dingplaysnd', None, 0, None, 0)
        r = winmm.mciSendStringW(f'open "{f}" type waveaudio alias dingplaysnd', None, 0, None, 0)
        if r != 0:
            log(f"MCI open 失败({r})")
            return
        winmm.mciSendStringW('play dingplaysnd', None, 0, None, 0)
    except Exception as e:
        log(f"播放异常: {e}")

def speak(text):
    try:
        ps = tempfile.NamedTemporaryFile(suffix=".ps1", delete=False, mode="w", encoding="utf-8-sig")
        safe = text.replace('"', "'")[:120]
        ps.write(f'Add-Type -AssemblyName System.Speech; '
                 f'$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; '
                 f'$s.SetOutputToDefaultAudioDevice(); $s.Speak("{safe}")')
        ps.close()
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                          "-File", ps.name], creationflags=0x08000000)
        threading.Timer(8, lambda: os.unlink(ps.name) if os.path.exists(ps.name) else None).start()
    except Exception as e:
        log(f"TTS 失败: {e}")

# ── 免打扰 ──
def quiet_now():
    if not CFG.get("quiet_enabled", False):
        return False
    q = CFG.get("quiet_hours", ["23:00", "08:00"])
    try:
        now = time.strftime("%H:%M")
        a, b = q[0], q[1]
        return (a <= now < b) if a < b else (now >= a or now < b)
    except Exception:
        return False

# ── 对话状态机（WorkBuddy sessions 表——已验证）──
def detect_chat_db():
    c = os.path.join(os.path.expanduser("~"), ".workbuddy", "workbuddy.db")
    return c if os.path.exists(c) else None

def poll_chat_sessions(db):
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT id, status FROM sessions WHERE deleted_at IS NULL AND status IN ('working','completed','error')")
        r = {x[0]: x[1] for x in cur.fetchall()}
        conn.close()
        return r
    except Exception as e:
        log(f"chat 轮询失败: {e}")
        return {}

# ── MCI TTS 之外的播报核心 ──
def fire(event, text=""):
    meta = {"task_done": "监控的目录有变化", "chat_done": "AI 回复完成", "heartbeat": "任务异常提醒"}
    if event not in meta:
        return
    with CFG_LOCK:
        sw = CFG["switches"].get(event, True)
        conf = CFG["sounds"].get(event, {})
    if not sw:
        return
    mode = conf.get("mode", "sound")
    if mode == "off":
        return
    if quiet_now():
        return
    name = meta[event]
    body = (text + "。" if text else "") + name
    if mode in ("sound", "both"):
        f = conf.get("file", "")
        play_file(f)
    if mode in ("tts", "both") and text:
        if mode == "both":
            threading.Thread(target=lambda: (time.sleep(0.5), speak(body[:100])), daemon=True).start()
        else:
            speak(body[:100])
    with CFG_LOCK:
        CFG["last_event"] = f"{time.strftime('%H:%M')} {body[:60]}"
    global _today_alert_count
    _today_alert_count += 1
    log(f"播报完成 [{event}/{mode}]: {body[:80]}")

# ── 监控循环（业务逻辑平移，已验证）──
def dir_fingerprint(path):
    """递归全树指纹（文件数, 总字节, 最新修改时间）——子目录/改名/内容修改全部可感知"""
    files = 0; total = 0; latest = 0.0
    try:
        for root, ds, fs in os.walk(path):
            for f in fs:
                try:
                    st = os.stat(os.path.join(root, f))
                    files += 1
                    total += st.st_size
                    if st.st_mtime > latest:
                        latest = st.st_mtime
                except OSError:
                    pass
    except Exception as e:
        log(f"指纹扫描失败 {path}: {e}")
    return (files, total, latest)

_last_change = time.time()
_last_hb = time.time()
_last_cfg_mt = 0
_last_trig = 0
CHAT_DB = detect_chat_db()
_prev_working = set()
_chat_poll_at = 0
_today_alert_count = 0
error_alerted = set()  # 已提醒过异常的会话（每个会话只提醒一次）
ui_show_event = __import__("threading").Event()  # 主线程唤回事件（flag tick → Qt 主线程）

def _chat_tick():
    """对话状态机轻拍（1 秒节拍——AI 回复完成 ≤1 秒内提醒）"""
    global _prev_working, _chat_poll_at
    try:
        if time.time() - _chat_poll_at < 1:
            return
        _chat_poll_at = time.time()
        states = poll_chat_sessions(CHAT_DB)
        now_w = {sid for sid, st in states.items() if st == "working"}
        for sid in _prev_working - now_w:
            fire("chat_done", "AI 回复完成")
        _prev_working = now_w
    except Exception as e:
        log(f"chat tick 异常: {e}")

def _flag_tick():
    """show.flag/exit.flag 轻拍（1 秒节拍——双击图标 ≤1 秒内唤回）"""
    try:
        fp = os.path.join(APP_DIR, "show.flag")
        if os.path.exists(fp):
            os.remove(fp)
            log("show.flag：窗口唤回请求")
            ui_show_event.set()
        ep = os.path.join(APP_DIR, "exit.flag")
        if os.path.exists(ep):
            os.remove(ep)
            log("exit.flag：退出")
            os._exit(0)
    except Exception as e:
        log(f"flag tick 异常: {e}")

def monitor_loop(refresh_ui):
    global _last_change, _last_hb, _last_cfg_mt, _last_trig, _prev_working, _chat_poll_at
    log(f"监控启动（哨兵 {len(CFG['sentinels'])} 个 + 触发器 + 对话状态机）")
    if CHAT_DB:
        _prev_working = {sid for sid, st in poll_chat_sessions(CHAT_DB).items() if st == "working"}
        log(f"对话监控启用（状态机）当前 working {len(_prev_working)}")
    while True:
        try:
            # show.flag / exit.flag（唤回与退出信号）
            fp = os.path.join(APP_DIR, "show.flag")
            if os.path.exists(fp):
                os.remove(fp)
                refresh_ui()
            ep = os.path.join(APP_DIR, "exit.flag")
            if os.path.exists(ep):
                os.remove(ep)
                log("exit.flag 收到，退出")
                os._exit(0)

            # 哨兵
            sentinels = CFG["sentinels"]
            prev = {}
            for sn in sentinels:
                path = sn.get("path", "")
                if os.path.isfile(path):
                    st = os.stat(path); prev[sn["name"]] = (st.st_mtime, st.st_size)
                elif os.path.isdir(path):
                    prev[sn["name"]] = ("dir", dir_fingerprint(path))
            # 1 秒轻拍 ×poll_interval 次：对话状态机/唤醒信号实时响应（不拖哨兵节拍）
            for _tick_i in range(max(1, CFG.get("poll_interval", 10))):
                time.sleep(1)
                if CHAT_DB:
                    _chat_tick()
                _flag_tick()
            for sn in sentinels:
                path = sn.get("path", "")
                cur = None
                if os.path.isfile(path):
                    st = os.stat(path); cur = (st.st_mtime, st.st_size)
                elif os.path.isdir(path):
                    cur = ("dir", dir_fingerprint(path))
                if cur is not None and prev.get(sn["name"]) != cur:
                    _last_change = time.time()
                    if CFG["switches"].get("task_done", True):
                        fire("task_done", f"{sn['name']}")
                    break
            # 任务异常检测（系统级信号判定——不是时长猜测）：
            # 会话状态翻转到 error = AI 遇到了错误（报错/网络问题/任务失败）→ 异常警报
            states = poll_chat_sessions(CHAT_DB) if CHAT_DB else {}
            now_w = {sid for sid, st in states.items() if st == "working"}
            if CFG["switches"].get("heartbeat", True):
                for sid in _prev_working:
                    if states.get(sid) == "error" and sid not in error_alerted:
                        error_alerted.add(sid)
                        fire("heartbeat", "AI 任务遇到错误，去看一眼")
            # 任务异常检测结束
            hb = CFG.get("hb_minutes", 20) * 60  # 保留 config 键（向后兼容，暂不参与判定）
            # 对话状态机已移至 _chat_tick（1 秒轻拍独立节拍——AI 回复完成 ≤1 秒提醒）
            # 触发器
            if os.path.exists(TRIGGER_PATH):
                mt = os.path.getmtime(TRIGGER_PATH)
                if mt != _last_trig:
                    _last_trig = mt
                    try:
                        td = json.load(io.open(TRIGGER_PATH, encoding="utf-8"))
                        ev = td.get("event", "task_done")
                        tx = td.get("text", "")
                        if ev in ("task_done", "chat_done", "heartbeat"):
                            fire(ev, tx)
                    except Exception as e:
                        log(f"trigger 解析失败: {e}")
            # config 重载（内容变化才刷新）
            if os.path.exists(CONFIG_PATH):
                cmt = os.path.getmtime(CONFIG_PATH)
                if cmt != _last_cfg_mt:
                    _last_cfg_mt = cmt
                    new_cfg = load_config()
                    with CFG_LOCK:
                        if json.dumps(new_cfg, sort_keys=True, ensure_ascii=False) != json.dumps(CFG, sort_keys=True, ensure_ascii=False):
                            CFG.clear(); CFG.update(new_cfg)
                            log("config 重载")
                            refresh_ui()
        except Exception as e:
            log(f"监控异常: {e}")
            time.sleep(5)

# ── PyQt5 GUI ──
from PyQt5.QtWidgets import (QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout,
                             QListWidget, QListWidgetItem, QPushButton, QFileDialog,
                             QComboBox, QSpinBox, QSystemTrayIcon, QMenu, QAction,
                             QMessageBox, QFrame, QLineEdit, QCheckBox, QTimeEdit)
from PyQt5.QtCore import QTime
from PyQt5.QtGui import QIcon, QFont
from PyQt5.QtCore import Qt, QTimer

ICON_PATH = os.path.join(APP_DIR, "dingdone.ico")

def make_ico_file():
    """生成 dingdone.ico（多尺寸）"""
    try:
        from PIL import Image
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        from PIL import ImageDraw
        d = ImageDraw.Draw(img)
        d.polygon([(18, 38), (18, 18), (32, 10), (46, 18), (46, 38)], fill=(59, 130, 246, 255))
        d.rectangle([14, 38, 50, 44], fill=(59, 130, 246, 255))
        d.arc([24, 44, 40, 58], start=0, end=180, fill=(59, 130, 246, 255), width=4)
        img.save(ICON_PATH, sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    except Exception as e:
        log(f"ico 生成失败: {e}")

# ═══ UI Design Tokens（v4.0 设计系统）═══
BLUE = "#3b82f6"

I18N = {
    "zh": {
        "title": "DingDone — AI 任务完成提醒器", "sub": "AI 任务完成提醒器",
        "hero_on": "监控中", "hero_dirs": "{n} 个文件夹", "hero_last": "上次提醒 {t}",
        "hero_count": "今日已提醒 {n} 次",
        "step1": "监控的文件夹", "add": "＋ 添加文件夹", "del": "移除",
        "events": "提醒事件", "events_hint": "任务异常由 AI 的错误信号自动触发",
        "task_done": "有任务完成落盘", "chat_done": "AI 回复提醒",
        "heartbeat": "任务异常提醒",
        "hb_hint": "任务异常由 AI 错误信号触发（红色警报）",
        "ai_card": "不会配置？让 AI 替你", "ai_copy": "复制提示语给 AI",
        "saved": "已保存", "monitoring": "监控中", "lang_btn": "English",
        "quiet": "免打扰", "settings": "设置",
    },
    "en": {
        "title": "DingDone — AI Task Notifier", "sub": "AI task completion notifier",
        "hero_on": "Monitoring", "hero_dirs": "{n} folders", "hero_last": "Last alert {t}",
        "hero_count": "{n} alerts today",
        "step1": "Watched folders", "add": "＋ Add folder", "del": "Remove",
        "events": "Alert events", "events_hint": "Task-stuck alerts are triggered by AI error signals",
        "task_done": "Folder changed", "chat_done": "AI reply notifier",
        "heartbeat": "Task stuck alert",
        "hb_hint": "Triggered by AI error signals (red alert)",
        "ai_card": "Not sure? Let AI configure", "ai_copy": "Copy prompt for AI",
        "saved": "Saved", "monitoring": "Monitoring", "lang_btn": "中文",
        "quiet": "Do not disturb", "settings": "Settings",
    },
}

QSS = """
QWidget { background: #f3f4f6; font-family: '微软雅黑','Segoe UI'; font-size: 12px; }
QLabel#title { font-size: 22px; font-weight: 700; color: #111827; }
QLabel#sub { font-size: 10.5px; color: #9ca3af; }
QFrame#card { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 12px; }
QFrame#card:hover { border-color: #d1d5db; }
QLabel { color: #111827; font-size: 12px; }
QLabel#heroStatus { font-size: 17px; font-weight: 600; color: #111827; }
QLabel#heroDot { font-size: 20px; color: #3b82f6; }
QLabel#hint { color: #9ca3af; font-size: 10.5px; }
QLabel#status { color: #4b5563; font-size: 10.5px; }
QLabel#evtDot { font-size: 12px; }
QPushButton { background: #3b82f6; color: #fff; border: none; border-radius: 8px;
              padding: 7px 16px; font-size: 12px; font-weight: 600; }
QPushButton:hover { background: #2563eb; }
QPushButton:pressed { background: #1d4ed8; }
QPushButton#ghost { background: #f3f4f6; color: #4b5563; border: 1px solid #e5e7eb; font-weight: 400; }
QPushButton#ghost:hover { background: #e5e7eb; }
QListWidget { background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 8px;
              font-size: 12px; padding: 4px; }
QListWidget::item { padding: 6px; border-radius: 6px; }
QListWidget::item:selected { background: #eff6ff; color: #1d4ed8; }
QComboBox, QSpinBox { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 5px 10px; }
QComboBox:hover, QSpinBox:hover { border-color: #3b82f6; }
QComboBox:focus, QSpinBox:focus { border-color: #3b82f6; }
QCheckBox::indicator { width: 44px; height: 22px; border-radius: 11px; background: #d1d5db; }
QCheckBox::indicator:checked { background: #3b82f6; }
QLabel#evtRow[off="true"] { color: #9ca3af; }
QToolTip { background: #111827; color: #f9fafb; border: none; padding: 6px; }
"""

# ═══ 事件语义色 ═══
EVT_COLOR = {"task_done": "#3b82f6", "chat_done": "#10b981", "heartbeat": "#ef4444"}
DARK = "#1e293b"; GRAY = "#64748b"; BG = "#f8fafc"; CARD = "#ffffff"
QSS = f"""
QWidget {{ background:{BG}; font-family:'微软雅黑'; }}
QLabel#title {{ font-size:20px; font-weight:bold; color:{DARK}; }}
QLabel#sub {{ font-size:9pt; color:{GRAY}; }}
QFrame#card {{ background:{CARD}; border:1px solid #e2e8f0; border-radius:8px; }}
QLabel {{ color:{DARK}; font-size:10pt; }}
QLabel#hint {{ color:{GRAY}; font-size:9pt; }}
QPushButton {{ background:{BLUE}; color:#fff; border:none; border-radius:6px;
               padding:6px 14px; font-size:10pt; font-weight:600; }}
QPushButton:hover {{ background:#2563eb; }}
QPushButton#ghost {{ background:#e2e8f0; color:{DARK}; font-weight:400; }}
QListWidget {{ background:{CARD}; border:1px solid #e2e8f0; border-radius:8px; font-size:10pt; }}
QComboBox, QSpinBox {{ background:{CARD}; border:1px solid #e2e8f0; border-radius:6px; padding:4px 8px; }}
QLabel#status {{ color:{BLUE}; font-size:9pt; }}
"""

SOUND_FILES = [f for f in sorted(os.listdir(CC0_DIR)) if f.endswith(".wav")] if os.path.isdir(CC0_DIR) else []

class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.lang = CFG.get("lang", "zh")
        self.setWindowTitle("DingDone — AI 任务完成提醒器")
        self.setWindowIcon(QIcon(ICON_PATH))
        self.resize(460, 780)
        self.setStyleSheet(QSS)
        self._build_ui()
        self._tray = None
        self._setup_tray()
        self.refresh()

    def _build_ui(self):
        self.lang = CFG.get("lang", "zh")
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 14, 18, 10)
        v.setSpacing(10)

        # ── 顶栏：品牌 + 语言 ──
        top = QHBoxLayout()
        self.w_title = QLabel("DingDone"); self.w_title.setObjectName("title")
        top.addWidget(self.w_title); top.addStretch()
        self.btn_lang = QPushButton(""); self.btn_lang.setObjectName("ghost")
        self.btn_lang.setFixedWidth(76); self.btn_lang.clicked.connect(self.toggle_lang)
        top.addWidget(self.btn_lang)
        v.addLayout(top)
        self.w_sub = QLabel(""); self.w_sub.setObjectName("sub")
        v.addWidget(self.w_sub)

        # ── HERO 状态区（卡片）──
        hero = QFrame(); hero.setObjectName("card"); hv = QHBoxLayout(hero)
        hv.setContentsMargins(16, 12, 16, 12)
        self.w_heroDot = QLabel("●"); self.w_heroDot.setObjectName("heroDot")
        hv.addWidget(self.w_heroDot)
        hv2 = QVBoxLayout(); hv2.setSpacing(2)
        self.w_heroStatus = QLabel(""); self.w_heroStatus.setObjectName("heroStatus")
        self.w_heroMeta = QLabel(""); self.w_heroMeta.setObjectName("hint")
        hv2.addWidget(self.w_heroStatus); hv2.addWidget(self.w_heroMeta)
        hv.addLayout(hv2); hv.addStretch()
        self.w_heroCount = QLabel(""); self.w_heroCount.setObjectName("hint")
        hv.addWidget(self.w_heroCount)
        v.addWidget(hero)

        # ── 卡片1：监控目录 ──
        c1 = QFrame(); c1.setObjectName("card"); v1 = QVBoxLayout(c1)
        self.w_step1 = QLabel(""); v1.addWidget(self.w_step1)
        self.dir_list = QListWidget(); self.dir_list.setFixedHeight(96)
        v1.addWidget(self.dir_list)
        hb = QHBoxLayout()
        self.w_add = QPushButton(""); self.w_add.clicked.connect(self.add_dir)
        self.w_del = QPushButton(""); self.w_del.setObjectName("ghost")
        self.w_del.clicked.connect(self.del_dir)
        hb.addWidget(self.w_add); hb.addWidget(self.w_del); hb.addStretch()
        v1.addLayout(hb)
        v.addWidget(c1)

        # ── 卡片2：提醒事件（语义色行 + 胶囊开关）──
        c2 = QFrame(); c2.setObjectName("card"); v2 = QVBoxLayout(c2)
        self.w_events = QLabel(""); v2.addWidget(self.w_events)
        self.event_combos = {}; self.w_event_labels = {}; self.w_event_switches = {}
        self.w_evt_rows = {}
        for ek in ["task_done", "chat_done", "heartbeat"]:
            row = QFrame(); row.setObjectName("evtRow")
            rl = QHBoxLayout(row); rl.setContentsMargins(4, 2, 4, 2)
            sw = QCheckBox()
            sw.setChecked(bool(CFG["switches"].get(ek, True)))
            sw.stateChanged.connect(lambda st, k=ek: self._toggle_event(k, st))
            self.w_event_switches[ek] = sw
            rl.addWidget(sw)
            dot = QLabel("●"); dot.setObjectName("evtDot")
            dot.setStyleSheet(f"color: {EVT_COLOR[ek]};")
            rl.addWidget(dot)
            lb = QLabel(""); self.w_event_labels[ek] = lb
            rl.addWidget(lb); rl.addStretch()
            cb = QComboBox()
            for f in SOUND_FILES:
                cb.addItem(f.replace(".wav", "").replace("_", " "))
            cb.currentIndexChanged.connect(lambda *_: QTimer.singleShot(500, self.save_settings))
            self.event_combos[ek] = cb
            rl.addWidget(cb)
            listen_btn = QPushButton("🔊")
            listen_btn.setObjectName("ghost"); listen_btn.setFixedWidth(36)
            listen_btn.setToolTip("试听当前音效")
            listen_btn.clicked.connect(lambda _, k=ek: self._listen(k))
            rl.addWidget(listen_btn)
            self.w_evt_rows[ek] = row
            v2.addWidget(row)
        self.w_evt_hint = QLabel(""); self.w_evt_hint.setObjectName("hint")
        v2.addWidget(self.w_evt_hint)
        v.addWidget(c2)

        # ── 卡片3：AI 代配（默认折叠）──
        c3 = QFrame(); c3.setObjectName("card"); v3_ = QVBoxLayout(c3)
        ai_head = QHBoxLayout()
        self.w_ai_card = QLabel(""); self.w_ai_card.setStyleSheet("font-weight:600;")
        ai_head.addWidget(self.w_ai_card); ai_head.addStretch()
        self.btn_ai_fold = QPushButton("▾"); self.btn_ai_fold.setObjectName("ghost")
        self.btn_ai_fold.setFixedWidth(36); self.btn_ai_fold.clicked.connect(self._toggle_ai)
        ai_head.addWidget(self.btn_ai_fold)
        v3_.addLayout(ai_head)
        self.w_ai_body = QWidget()
        ab = QVBoxLayout(self.w_ai_body); ab.setContentsMargins(0, 0, 0, 0)
        self.w_ai_prompt = QLabel("")
        self.w_ai_prompt.setStyleSheet("background:#f9fafb; border:1px solid #e5e7eb; border-radius:8px; padding:8px; font-size:10.5px; color:#374151;")
        self.w_ai_prompt.setWordWrap(True)
        self.w_ai_prompt.setTextInteractionFlags(Qt.TextSelectableByMouse)
        ab.addWidget(self.w_ai_prompt)
        self.w_ai_copy = QPushButton("")
        self.w_ai_copy.clicked.connect(self.copy_ai_prompt)
        ab.addWidget(self.w_ai_copy)
        self.w_ai_manual = QLabel(""); self.w_ai_manual.setObjectName("hint"); self.w_ai_manual.setWordWrap(True)
        ab.addWidget(self.w_ai_manual)
        v3_.addWidget(self.w_ai_body)
        self.w_ai_body.setVisible(False)
        v.addWidget(c3)

        # ── 底部设置栏：免打扰开关 + 免打扰时间段 ──
        bar = QHBoxLayout()
        self.w_quiet_sw = QCheckBox()
        self.w_quiet_sw.setChecked(bool(CFG.get("quiet_enabled", False)))
        self.w_quiet_sw.stateChanged.connect(lambda st: self._toggle_quiet(st))
        bar.addWidget(self.w_quiet_sw)
        self.w_quiet = QLabel(""); bar.addWidget(self.w_quiet)
        self.w_quiet_from = QTimeEdit(); self.w_quiet_from.setDisplayFormat("HH:mm")
        self.w_quiet_to = QTimeEdit(); self.w_quiet_to.setDisplayFormat("HH:mm")
        try:
            qh = CFG.get("quiet_hours", ["23:00", "08:00"])
            self.w_quiet_from.setTime(QTime(int(qh[0][:2]), int(qh[0][3:5])))
            self.w_quiet_to.setTime(QTime(int(qh[1][:2]), int(qh[1][3:5])))
        except Exception:
            pass
        self.w_quiet_from.timeChanged.connect(lambda *_: QTimer.singleShot(500, self.save_settings))
        self.w_quiet_to.timeChanged.connect(lambda *_: QTimer.singleShot(500, self.save_settings))
        bar.addWidget(self.w_quiet_from)
        mid = QLabel("至"); mid.setObjectName("hint"); bar.addWidget(mid)
        bar.addWidget(self.w_quiet_to)
        bar.addStretch()
        v.addLayout(bar)

        # ── 状态条 ──
        self.status = QLabel(""); self.status.setObjectName("status")
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        self._i18n_refresh()

    def _toggle_ai(self):
        self.w_ai_body.setVisible(not self.w_ai_body.isVisible())
        self.btn_ai_fold.setText("▴" if self.w_ai_body.isVisible() else "▾")
        from PyQt5.QtCore import QTimer as _QT
        _QT.singleShot(60, self.adjustSize)  # 布局稳定后窗口自适应内容高度

    def ai_prompt_text(self):
        lang = self.lang
        path = CONFIG_PATH
        if lang == "zh":
            return ("你是我的任务完成提醒助手。请自主判断：根据你平时为我产出文件的位置"
                    "（比如你的工作目录、输出文件夹），选出最值得监控的 1-3 个文件夹，"
                    "写入下方 config 的 sentinels 列表（格式 {'name':'任务名','path':'完整路径'}）并保存，"
                    "DingDone 会自动生效。不需要询问我，直接判断并配置。config 路径：" + path)
        return ("You are my task-completion notifier. Decide on your own: based on where you usually "
                "produce files for me, pick the 1-3 folders most worth watching, write them into the "
                "sentinels list of the config below (format {'name':'task','path':'full path'}), then save. "
                "DingDone reloads automatically. Do not ask me — just configure it. Config path: " + path)

    def copy_ai_prompt(self):
        QApplication.clipboard().setText(self.ai_prompt_text())
        self.status.setText(I18N[self.lang]["saved"] if self.lang == "en" else "已复制，粘贴发给你的 AI")
        log("AI 代配提示语已复制（Qt 剪贴板）")

    def _i18n_refresh(self):
        t = I18N.get(self.lang, I18N["zh"])
        self.setWindowTitle(t["title"])
        self.btn_lang.setText(t["lang_btn"])
        self.w_sub.setText(t["sub"])
        self.w_step1.setText(t["step1"])
        self.w_add.setText(t["add"])
        self.w_del.setText(t["del"])
        self.w_events.setText(t["events"])
        self.w_evt_hint.setText(t["events_hint"])
        self.w_event_labels["task_done"].setText(t["task_done"])
        self.w_event_labels["chat_done"].setText(t["chat_done"])
        self.w_event_labels["heartbeat"].setText(t["heartbeat"])
        self.w_heartbeat = self.w_event_labels["heartbeat"]
        self.w_ai_card.setText(t["ai_card"])
        self.w_ai_copy.setText(t["ai_copy"])
        self.w_quiet.setText(t["quiet"])
        self.w_heroStatus.setText(t["hero_on"])
        self.w_heroMeta.setText(t["hero_dirs"].format(n=len(CFG["sentinels"])))
        self.w_heroCount.setText(t["hero_count"].format(n=getattr(self, "_today_alerts", 0)))
        self.w_ai_prompt.setText(self.ai_prompt_text())
        self.status.setText(CFG.get("last_event") or t["monitoring"])

    def toggle_lang(self):
        self.lang = "en" if self.lang == "zh" else "zh"
        CFG["lang"] = self.lang
        save_cfg(CFG)
        self._i18n_refresh()

    def _setup_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            log("系统托盘不可用，跳过")
            return
        menu = QMenu()
        act_show = QAction("显示窗口", self)
        act_show.triggered.connect(self.show_and_raise)
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self.real_quit)
        menu.addAction(act_show); menu.addAction(act_quit)
        self._tray = QSystemTrayIcon(QIcon(ICON_PATH), self)
        self._tray.setContextMenu(menu)
        self._tray.setToolTip("DingDone — AI 任务完成提醒器")
        self._tray.activated.connect(self._tray_activated)
        self._tray.show()
        log("QSystemTrayIcon 托盘已显示")

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger or reason == QSystemTrayIcon.DoubleClick:
            self.show_and_raise()

    def show_and_raise(self):
        self.showNormal(); self.raise_(); self.activateWindow()

    def real_quit(self):
        log("用户退出")
        if self._tray: self._tray.hide()
        QApplication.quit()

    def closeEvent(self, e):
        # 点 × = 最小化到任务栏（任务栏按钮保留=存在感），不退出
        e.ignore()
        self.showMinimized()
        log("点× → 最小化到任务栏（后台继续）")

    def add_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择要监控的文件夹")
        if d:
            name = os.path.basename(d) or d
            CFG["sentinels"].append({"name": name, "path": d})
            save_cfg(CFG)
            self.refresh()

    def del_dir(self):
        row = self.dir_list.currentRow()
        if row >= 0 and row < len(CFG["sentinels"]):
            del CFG["sentinels"][row]
            save_cfg(CFG)
            self.refresh()

    def refresh(self):
        # 哨兵列表
        self.dir_list.clear()
        for sn in CFG["sentinels"]:
            self.dir_list.addItem(f"{sn['name']} — {sn['path']}")
        for ek, sw in self.w_event_switches.items():
            want = bool(CFG["switches"].get(ek, True))
            if sw.isChecked() != want:
                sw.blockSignals(True); sw.setChecked(want); sw.blockSignals(False)
        # 音效下拉 + 开关状态（只在不一致时同步，避免打字被覆盖）
        for ek, cb in self.event_combos.items():
            f = CFG["sounds"].get(ek, {}).get("file", "")
            base = os.path.basename(f)
            idx = next((i for i, s in enumerate(SOUND_FILES) if s == base), -1)
            if idx >= 0 and cb.currentIndex() != idx:
                cb.blockSignals(True); cb.setCurrentIndex(idx); cb.blockSignals(False)
        self.status.setText(CFG.get("last_event") or I18N[self.lang]["monitoring"])
        self.w_heroMeta.setText(I18N[self.lang]["hero_dirs"].format(n=len(CFG["sentinels"])))

    def _listen(self, ek):
        """试听当前事件选中音效（MCI 播放）"""
        with CFG_LOCK:
            f = CFG["sounds"][ek].get("file", "")
        play_file(f)
        self.status.setText("试听中：") if False else self.status.setText(
            f"🔊 试听 {I18N[self.lang].get(ek, ek)} —— {os.path.basename(f)}")
        log(f"试听 [{ek}]: {os.path.basename(f)}")

    def _toggle_quiet(self, state):
        with CFG_LOCK:
            CFG["quiet_enabled"] = bool(state)
        save_cfg(CFG)
        self.status.setText(("免打扰已开启 " if state else "免打扰已关闭 ") +
                            f"{CFG.get('quiet_hours', ['23:00','08:00'])[0]}-{CFG.get('quiet_hours', ['23:00','08:00'])[1]}")

    def save_settings(self):
        for ek, cb in self.event_combos.items():
            if 0 <= cb.currentIndex() < len(SOUND_FILES):
                CFG["sounds"][ek]["file"] = os.path.join(CC0_DIR, SOUND_FILES[cb.currentIndex()])
        qh = [self.w_quiet_from.time().toString("HH:mm"),
              self.w_quiet_to.time().toString("HH:mm")]
        CFG["quiet_hours"] = qh
        CFG["quiet_enabled"] = self.w_quiet_sw.isChecked()
        save_cfg(CFG)
        self.status.setText(I18N[self.lang]["saved"])

# ── 单实例（QLocalServer——Qt 原生方案）──
from PyQt5.QtNetwork import QLocalServer, QLocalSocket
SERVER_NAME = "DingDoneSingleInstance"

def try_activate_existing():
    """已有实例在跑 → 通知它显示窗口 → 返回 True（本进程应退出）"""
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.flush()
        sock.waitForDisconnected(300)
        return True
    return False

class Api:
    """js_api 兼容层（v3 无 web——保留触发器协议文件通路即可）"""
    pass

def main():
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(ICON_PATH))
    app.setApplicationName("DingDone")
    app.setQuitOnLastWindowClosed(False)

    if try_activate_existing():
        log("已有实例，已通知其显示窗口，本进程退出")
        sys.exit(0)

    # 单实例服务器
    QLocalServer.removeServer(SERVER_NAME)
    server = QLocalServer()
    server.listen(SERVER_NAME)
    win_ref = {}  # {"w": 主窗引用}（单实例唤回用）

    def on_new_conn():
        sock = server.nextPendingConnection()
        if sock:
            sock.waitForReadyRead(100)
            data = bytes(sock.readAll()).decode("utf-8", errors="ignore")
            if "show" in data:
                # 主线程内（Qt 信号回调）→ 直接唤回窗口（线程安全）
                try:
                    win_ref["w"].show_and_raise()
                    log("单实例：已有窗口唤回")
                except Exception as e:
                    log(f"单实例唤回失败: {e}")

    server.newConnection.connect(on_new_conn)

    make_ico_file()
    global CFG
    CFG = load_config()

    w = MainWindow()
    w.show()
    w.raise_()
    w.activateWindow()  # 启动强制置前（否则被 WorkBuddy 等大窗盖住，用户以为没启动）
    win_ref["w"] = w

    def poll_ui_show():
        if ui_show_event.is_set():
            ui_show_event.clear()
            w.show_and_raise()
        QTimer.singleShot(500, poll_ui_show)
    poll_ui_show()

    def refresh_ui():
        # monitor 线程不能直接碰 Qt 控件 → 调度到主线程执行
        from PyQt5.QtCore import QTimer as _QT
        try:
            _QT.singleShot(0, w.refresh)
        except Exception as e:
            log(f"UI 刷新调度失败: {e}")

    # 监控线程
    threading.Thread(target=monitor_loop, args=(refresh_ui,), daemon=True).start()

    # 设置自动保存（界面改动 → 2 秒后保存）
    def auto_save():
        try:
            w.save_settings()
        except Exception:
            pass
    for ek, cb in w.event_combos.items():
        cb.currentIndexChanged.connect(lambda *_: QTimer.singleShot(500, auto_save))

    log("DingDone v3 启动（PyQt5）")
    sys.exit(app.exec_())

if __name__ == "__main__":
    # 开机自启（bat 写一次）
    startup = os.path.join(os.environ.get("APPDATA", ""),
                           r"Microsoft\Windows\Start Menu\Programs\Startup")
    bat = os.path.join(startup, "DingDone.bat")
    if getattr(sys, "frozen", False):
        exe = os.path.join(APP_DIR, "DingDone.exe")
        cmd = f'start "" "{exe}"'
        existing = ""
        if os.path.exists(bat):
            existing = io.open(bat, encoding="utf-8", errors="ignore").read()
        if "DingDone.exe" not in existing:
            io.open(bat, "w", encoding="ascii", errors="ignore").write("@echo off\r\n" + cmd + "\r\n")
            log(f"开机自启: {bat}")
    main()
