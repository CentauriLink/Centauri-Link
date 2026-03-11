# main.py — CentauriLink (OctoEverywhere + Elegoo Centauri Carbon SDCP v3)
#
# KEY PROTOCOL FACTS (confirmed by OctoEverywhere founder + SDCP docs):
#
#  WebSocket:  Use the base URL returned by the App Portal as-is.
#              Replace https:// with wss:// — do NOT alter the hostname.
#              Example: https://app-abc123.octoeverywhere.com  →  wss://app-abc123.octoeverywhere.com/websocket
#
#  Video:      https://<oe-host>/video  (OE webcam proxy endpoint)
#              Example: https://app-abc123.octoeverywhere.com/video
#
#  Auth:       Bearer token on HTTP requests AND on the WS upgrade header —
#              OctoEverywhere's tunnel proxy requires it on the WS upgrade too.
#
#  On open:    MUST send Cmd 512 {"TimePeriod": 5000} to subscribe the printer
#              to push status updates every 5000ms.  Without this the printer
#              stays silent.  This is the "subscribe" command confirmed by the
#              OctoEverywhere founder.
#
#  Temps:      TempOfNozzle / TempOfHotbed are [target, actual] arrays.
#              Index [1] = actual current temperature.
#
#  Status:     Pushed on topic sdcp/status/<MainboardID> as:
#              {"Id":..., "Data": {"Status": {...}, "MainboardID":..., "TimeStamp":...}}
#
#  MainboardID: Required in every outgoing SDCP command. Obtained from the
#              HTTP monitor endpoint (/network-device-manager/network/monitor).
#
#  Local WS:   ws://<ip>:3030/websocket  (port 3030, local LAN only)

import os
import json
import uuid
import threading
import time
import traceback
import socket
import urllib.parse
import webbrowser
import ssl
from datetime import datetime
from io import BytesIO

import requests
try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass

from kivy.app import App
from kivy.lang import Builder
from kivy.clock import Clock
from kivy.properties import StringProperty, NumericProperty, BooleanProperty
from kivy.uix.image import Image as KivyImage
from kivy.core.image import Image as CoreImage
from kivy.graphics.texture import Texture

try:
    import websocket
    WS_AVAILABLE = True
except Exception:
    websocket = None
    WS_AVAILABLE = False

try:
    from PIL import Image as PILImage
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False

IS_ANDROID  = False
android_bind = None
try:
    from kivy.utils import platform
    if platform == "android":
        IS_ANDROID = True
        try:
            from android.activity import bind as android_bind
        except Exception:
            android_bind = None
except Exception:
    IS_ANDROID = False

KV_FILE = "centaurilink.kv"

# Elegoo / SDCP API paths — identical locally and through the OE tunnel
WS_PORT      = 3030                                    # local direct only
WS_PATH      = "/websocket"
MONITOR_PATH = "/network-device-manager/network/monitor"
CONTROL_PATH = "/network-device-manager/network/control"

# Camera / video paths (tried in order for OE tunnel)
OE_VIDEO_PATH         = "/video"                                       # OE webcam proxy (preferred)
OE_WEBCAM_STREAM_PATH = "/octoeverywhere-command-api/webcam/stream"    # legacy OE path
ELEGOO_CAMERA_PATH    = "/network-device-manager/network/camera"       # direct Elegoo path

POLL_INTERVAL     = 5.0   # REST fallback poll interval (WS preferred)
OCTO_CONTROL_POLL = 5.0   # OE REST poll interval

DEBUG_WS = False


# ── Storage ───────────────────────────────────────────────────────────────────

def get_app_storage_dir():
    try:
        if IS_ANDROID:
            from jnius import autoclass
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            ctx = PythonActivity.mActivity
            ext = ctx.getExternalFilesDir(None)
            if ext:
                path = ext.getAbsolutePath()
                try: os.makedirs(path, exist_ok=True)
                except Exception: pass
                return path
    except Exception:
        pass
    try:
        p = os.path.abspath(os.getcwd())
        os.makedirs(p, exist_ok=True)
        return p
    except Exception:
        return "/"

APP_STORAGE     = get_app_storage_dir()
LOG_PATH        = os.path.join(APP_STORAGE, "centauri_debug.log")
SESSION_PATH    = os.path.join(APP_STORAGE, "centauri_session.json")
WS_RAW_LOG      = os.path.join(APP_STORAGE, "octo_ws_raw.log")
WS_UNPARSED_LOG = os.path.join(APP_STORAGE, "octo_ws_unparsed.log")


def log(msg: str):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        with open(LOG_PATH, "a", encoding="utf8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print("[Centauri]", line)


# ── URL helpers ───────────────────────────────────────────────────────────────

def _make_octo_ws_url(base_url: str) -> str:
    """
    Build the OctoEverywhere WebSocket URL from the portal-returned base URL.
    The hostname is used exactly as returned by the App Portal — no mutation.
    Only the scheme is swapped: https -> wss, http -> ws.
    """
    base = base_url.strip().rstrip("/")
    if base.startswith("https://"):
        ws_url = "wss://" + base[len("https://"):] + WS_PATH
    elif base.startswith("http://"):
        ws_url = "ws://" + base[len("http://"):] + WS_PATH
    else:
        ws_url = "wss://" + base.lstrip(":/") + WS_PATH
    log(f"OE WS URL: {ws_url}")
    return ws_url


def _make_octo_video_url(base_url: str) -> str:
    """
    Build the OctoEverywhere video stream URL from the portal-returned base URL.
    """
    return base_url.rstrip("/") + OE_VIDEO_PATH


# ── SDCP helpers ──────────────────────────────────────────────────────────────

def safe_float(v):
    """
    Parse a float from v.  SDCP temps are [target, actual] arrays —
    return actual (index 1).  Scalars returned directly.
    """
    if isinstance(v, (list, tuple)):
        v = v[1] if len(v) > 1 else (v[0] if v else None)
    try:
        return float(v)
    except Exception:
        return None


def interpret_status_code(code):
    mapping = {
        0: "Idle",      1: "Preparing",  2: "Homing",
        3: "Printing",  4: "Pausing",    5: "Paused",
        6: "Stopping",  7: "Stopped",    8: "Busy",
        9: "Complete",
    }
    try:
        return mapping.get(int(code), f"Status {code}")
    except Exception:
        return str(code)


def make_sdcp_cmd(cmd: int, data: dict, mainboard_id: str) -> str:
    """
    Build a fully-formed SDCP request packet as a JSON string.
    Required fields: Id, Data.Cmd, Data.Data, Data.RequestID,
                     Data.MainboardID, Data.TimeStamp, Data.From, Topic.
    """
    msg_id     = str(uuid.uuid4())
    request_id = str(uuid.uuid4())
    return json.dumps({
        "Id": msg_id,
        "Data": {
            "Cmd": cmd,
            "Data": data,
            "RequestID": request_id,
            "MainboardID": mainboard_id,
            "TimeStamp": int(time.time()),
            "From": 1,   # 1 = Mobile app
        },
        "Topic": f"sdcp/request/{mainboard_id}",
    })


def extract_all_from_message(obj: dict) -> dict:
    """
    Parse an Elegoo SDCP status payload into a normalised result dict.
    """
    result = {
        "temps":     {"nozzle": None, "bed": None, "chamber": None},
        "printinfo": {"status_code": None, "status_text": None,
                      "progress": None, "current_layer": None,
                      "total_layer": None, "file": None},
        "coords":    None,
        "lights":    None,
        "timestamp": None,
        "mainboard_id": None,
    }
    try:
        st = {}
        if isinstance(obj, dict):
            st = obj.get("Status") if isinstance(obj.get("Status"), dict) else obj

        if "TempOfNozzle" in st:
            result["temps"]["nozzle"]  = safe_float(st["TempOfNozzle"])
        if "TempOfHotbed" in st:
            result["temps"]["bed"]     = safe_float(st["TempOfHotbed"])
        if "TempOfBox" in st:
            result["temps"]["chamber"] = safe_float(st["TempOfBox"])

        pi = st.get("PrintInfo") or st.get("printInfo") or st.get("Printinfo")
        if isinstance(pi, dict):
            sc  = pi.get("Status")
            scv = sc[0] if isinstance(sc, (list, tuple)) and sc else sc
            try:
                result["printinfo"]["status_code"] = int(scv) if scv is not None else None
            except Exception:
                result["printinfo"]["status_code"] = scv
            result["printinfo"]["status_text"] = (
                interpret_status_code(result["printinfo"]["status_code"])
                if result["printinfo"]["status_code"] is not None else None)
            try:
                prog = pi.get("Progress")
                result["printinfo"]["progress"] = float(prog) if prog is not None else None
            except Exception:
                result["printinfo"]["progress"] = None
            result["printinfo"]["current_layer"] = pi.get("CurrentLayer") or pi.get("currentLayer")
            result["printinfo"]["total_layer"]   = pi.get("TotalLayer")   or pi.get("totalLayer")
            result["printinfo"]["file"] = (pi.get("Filename") or pi.get("filename")
                                           or pi.get("File")  or pi.get("file"))

        cc = st.get("CurrenCoord") or st.get("CurrentCoord") or st.get("CurrCoord")
        if cc:
            result["coords"] = str(cc)

        ls = st.get("LightStatus")
        if ls is not None:
            result["lights"] = ls

        mid = (obj.get("MainboardID") or obj.get("mainboard_id")
               or st.get("MainboardID"))
        if mid:
            result["mainboard_id"] = str(mid)

        ts = (obj.get("TimeStamp") or obj.get("timestamp")
              or st.get("TimeStamp") or st.get("timestamp"))
        if ts:
            try:
                result["timestamp"] = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                result["timestamp"] = str(ts)
    except Exception:
        log("extract_all_from_message error: " + traceback.format_exc())
    return result


# ── JSON helpers ──────────────────────────────────────────────────────────────

def _find_balanced_json_objects(s: str):
    objs = []; stack = 0; start = None; in_str = False; esc = False
    for i, ch in enumerate(s):
        if ch == '"' and not esc:
            in_str = not in_str
        esc = (ch == '\\' and in_str) if not esc else False
        if not in_str:
            if ch == '{':
                if stack == 0: start = i
                stack += 1
            elif ch == '}' and stack > 0:
                stack -= 1
                if stack == 0 and start is not None:
                    objs.append(s[start:i + 1]); start = None
    return objs


def _parse_json_objects_from_string(s: str):
    parsed = []
    if not s:
        return parsed
    st = s.strip()
    try:
        if (st.startswith("{") and st.endswith("}")) or (st.startswith("[") and st.endswith("]")):
            j = json.loads(st)
            if isinstance(j, dict):
                parsed.append(j); return parsed
            if isinstance(j, list):
                for el in j:
                    if isinstance(el, dict): parsed.append(el)
                if parsed: return parsed
    except Exception:
        pass
    for c in _find_balanced_json_objects(s):
        try:
            parsed.append(json.loads(c))
        except Exception:
            pass
    return parsed


def _status_from_parsed_object(obj):
    """
    Detect whether a parsed SDCP dict contains status data and normalise it.
    """
    if not isinstance(obj, dict):
        return None

    if "Status" in obj and isinstance(obj["Status"], dict):
        return obj

    topic = obj.get("Topic") or obj.get("topic") or ""
    data  = obj.get("Data")  or obj.get("data")

    if isinstance(data, dict):
        if "Status" in data and isinstance(data["Status"], dict):
            result = {"Status": data["Status"]}
            if data.get("MainboardID"):
                result["MainboardID"] = data["MainboardID"]
            if data.get("TimeStamp"):
                result["TimeStamp"] = data["TimeStamp"]
            return result

        inner = data.get("Data") or data.get("data")
        if isinstance(inner, dict) and "Status" in inner and isinstance(inner["Status"], dict):
            result = {"Status": inner["Status"]}
            if inner.get("MainboardID"):
                result["MainboardID"] = inner["MainboardID"]
            return result

        if isinstance(topic, str) and "sdcp/status" in topic:
            return {"Status": data, "Topic": topic}

    if isinstance(topic, str) and "sdcp/status" in topic:
        return {"Status": {k: v for k, v in obj.items() if k not in ("Topic", "Id")},
                "Topic": topic}

    for k in ("TempOfNozzle", "TempOfHotbed", "PrintInfo", "Printinfo", "CurrentStatus"):
        if k in obj:
            return {"Status": obj}

    return None


# ── Kivy screens ──────────────────────────────────────────────────────────────

from kivy.uix.screenmanager import ScreenManager, Screen


class HomeScreen(Screen):
    pass


class ControlScreen(Screen):
    status_text  = StringProperty("Ready")
    selected_ip  = StringProperty("")
    current_temp = StringProperty("N/A")
    auto_poll    = BooleanProperty(False)

    def on_kv_post(self, base_widget):
        self.status_text = "Ready"

    def add_manual_printer(self, iptext: str):
        ip = (iptext or "").strip()
        if not ip:
            self.status_text = "Empty address"; return
        try:
            socket.gethostbyname(ip)
        except Exception:
            self.status_text = "Invalid IP/hostname"; return
        try:
            from kivy.uix.button import Button
            if not any(getattr(c, "text", "") == ip for c in self.ids.device_list.children):
                b = Button(text=ip, size_hint_y=None, height=44)
                b.bind(on_release=lambda inst, _ip=ip: self.select_device(_ip))
                self.ids.device_list.add_widget(b)
        except Exception:
            pass
        self.select_device(ip)
        try:
            self.toggle_auto_poll(True)
        except Exception:
            try:
                App.get_running_app().start_local_polling()
                self.auto_poll = True
            except Exception:
                log("Failed enabling auto poll: " + traceback.format_exc())

    def select_device(self, ip):
        self.selected_ip = ip; self.status_text = f"Selected {ip}"
        app = App.get_running_app(); app.local_printer_ip = ip
        threading.Thread(target=self._try_connect_ws_then_http, daemon=True).start()

    def _try_connect_ws_then_http(self):
        app = App.get_running_app()
        app._local_stop_ws()
        if not app.local_printer_ip:
            self.status_text = "No IP"; return
        app.start_local_polling()
        if WS_AVAILABLE:
            try:
                app._local_start_ws(app.local_printer_ip)
                for _ in range(8):
                    if app.local_ws_running: return
                    time.sleep(0.25)
            except Exception:
                log("WS start error: " + traceback.format_exc())
        self.status_text = "Using HTTP fallback"

    def toggle_auto_poll(self, enabled: bool):
        self.auto_poll = bool(enabled)
        app = App.get_running_app()
        if self.auto_poll: app.start_local_polling()
        else: app.stop_local_polling()

    def disconnect_printer(self):
        app = App.get_running_app()
        app._local_stop_ws()
        app.local_printer_ip = None
        self.selected_ip = ""; self.current_temp = "N/A"; self.status_text = "Disconnected"
        app.stop_local_polling()

    def connect_printer(self):
        try:
            ip = ""
            try:
                w = self.ids.get("manual_ip") if hasattr(self, "ids") else None
                if w and getattr(w, "text", "").strip():
                    ip = w.text.strip()
            except Exception:
                pass
            if not ip: ip = (self.selected_ip or "").strip()
            if not ip: self.status_text = "No IP provided"; return
            try: socket.gethostbyname(ip)
            except Exception: self.status_text = "Invalid IP/hostname"; return
            self.add_manual_printer(ip)
        except Exception:
            log("connect_printer error: " + traceback.format_exc())
            try: self.status_text = "Connect failed"
            except Exception: pass


class OctoScreen(Screen):
    status_text = StringProperty("Not connected")

    def connect_octoeverywhere(self):
        app = App.get_running_app()
        self.status_text = "Starting Octo login..."
        threading.Thread(target=app.start_octo_login_flow, daemon=True).start()

    def disconnect_octoeverywhere(self):
        App.get_running_app().octo_disconnect()
        self.status_text = "Disconnected"

    def ui_only_notice(self, msg="Coming soon"):
        self.status_text = f"{msg} (UI only)"


class SettingsScreen(Screen):
    pass


class PrintScreen(ControlScreen):
    pass


# ── Main App ──────────────────────────────────────────────────────────────────

class CentauriApp(App):

    # Local printer UI props
    nozzle_temp      = StringProperty("N/A"); bed_temp       = StringProperty("N/A")
    chamber_temp     = StringProperty("N/A")
    nozzle_value     = NumericProperty(0);    bed_value      = NumericProperty(0)
    chamber_value    = NumericProperty(0)
    print_state      = StringProperty("(unknown)"); print_progress = NumericProperty(0.0)
    print_layer      = StringProperty(""); print_total_layer = StringProperty("")
    coords           = StringProperty("-"); light_state      = StringProperty("-")
    status_timestamp = StringProperty("-")

    # OctoEverywhere remote printer UI props
    octo_nozzle_temp   = StringProperty("N/A"); octo_bed_temp    = StringProperty("N/A")
    octo_chamber_temp  = StringProperty("N/A")
    octo_nozzle_value  = NumericProperty(0);    octo_bed_value   = NumericProperty(0)
    octo_chamber_value = NumericProperty(0)
    octo_state         = StringProperty("(not connected)"); octo_progress = NumericProperty(0.0)
    octo_file          = StringProperty(""); octo_timestamp = StringProperty("-")

    local_printer_ip  = None
    local_ws_app      = None; local_ws_thread = None; local_ws_running = False

    octo_base_url     = None; octo_bearer_token = None
    octo_basic_user   = None; octo_basic_pass   = None
    octo_mainboard_id = None   # learned from first HTTP monitor response

    _octo_poll_event  = None
    _camera_thread    = None; _camera_running = False
    _ws_thread        = None; _ws_running     = False
    _local_poll_event = None

    OCTO_PORTAL_BASE = (
        "https://octoeverywhere.com/appportal/v1/"
        "?appid=centaurilink"
        "&OctoPrintApiKeyAppName=CentauriLink"
        "&formatVersion=1"
        "&appLogoUrl=https%3A%2F%2Fi.ibb.co%2FTDMtSWZ3%2Fprofile-Icon-jurremdvv57g1.png"
    )

    # ── Build ─────────────────────────────────────────────────────────────────

    def build(self):
        Builder.load_file(KV_FILE)
        sm = ScreenManager()
        sm.add_widget(HomeScreen(name="home"))
        sm.add_widget(PrintScreen(name="print"))
        sm.add_widget(OctoScreen(name="octo"))
        sm.add_widget(SettingsScreen(name="settings"))
        return sm

    def on_start(self):
        if IS_ANDROID and android_bind:
            try:
                android_bind(on_new_intent=self._on_android_new_intent)
                Clock.schedule_once(lambda dt: self._consume_android_intent(), 0.5)
            except Exception:
                log("Android intent bind failed: " + traceback.format_exc())
        try:
            self._load_saved_session_and_autostart()
        except Exception:
            log("load session error: " + traceback.format_exc())

    def _on_android_new_intent(self, intent):
        try: self._consume_android_intent(intent)
        except Exception: log("on_new_intent error: " + traceback.format_exc())

    def _consume_android_intent(self, intent=None):
        if not IS_ANDROID: return
        try:
            from jnius import autoclass
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            activity = PythonActivity.mActivity
            it = intent if intent is not None else activity.getIntent()
            if it is None: return
            data = it.getDataString()
            if not data: return
            log("Android intent: " + str(data))
            try: it.setData(None); activity.setIntent(it)
            except Exception: pass
            self._handle_octoeverywhere_callback_url(data)
        except Exception:
            log("consume_intent error: " + traceback.format_exc())

    # ── OE login flow ─────────────────────────────────────────────────────────

    def start_octo_login_flow(self):
        try:
            return_url = "centaurilink://octoeverywhere-finish"
            url = self.OCTO_PORTAL_BASE + "&returnUrl=" + urllib.parse.quote(return_url, safe='')
            log("Opening OE portal: " + url)
            self.open_url(url)
        except Exception:
            log("start_octo_login_flow error: " + traceback.format_exc())

    def _handle_octoeverywhere_callback_url(self, url: str):
        """
        Parse OE App Portal callback.
        parse_qs is case-sensitive — normalise all keys to lowercase.
        """
        try:
            log("OE callback: " + str(url))
            parsed = urllib.parse.urlparse(url)
            raw_qs = parsed.query or parsed.fragment
            qs     = {k.lower(): v[0]
                      for k, v in urllib.parse.parse_qs(raw_qs).items() if v}

            if qs.get("success", "false").lower() != "true":
                self._octo_set_ui_status("Login cancelled/failed"); return

            base_url   = qs.get("url")
            bearer     = qs.get("authbearertoken") or qs.get("appapitoken") or qs.get("apikey")
            basic_user = qs.get("authbasichttpuser")
            basic_pass = qs.get("authbasichttppassword")
            printername = qs.get("printername", "(printer)")

            if base_url:
                base_url = urllib.parse.unquote(base_url).strip().rstrip("/")
                # Force hostname to lowercase — OE DNS only resolves lowercase.
                # The portal callback returns uppercase in the URL param.
                try:
                    _p = urllib.parse.urlparse(base_url)
                    base_url = _p._replace(netloc=_p.netloc.lower()).geturl()
                except Exception:
                    base_url = base_url.lower()
            if bearer:     bearer     = urllib.parse.unquote(bearer).strip()
            if basic_user: basic_user = urllib.parse.unquote(basic_user).strip()
            if basic_pass: basic_pass = urllib.parse.unquote(basic_pass).strip()

            if not base_url:
                self._octo_set_ui_status("Missing url= in callback"); return

            self.octo_base_url     = base_url
            self.octo_bearer_token = bearer
            self.octo_basic_user   = basic_user
            self.octo_basic_pass   = basic_pass

            log(f"OE session: url={base_url}  bearer={'yes' if bearer else 'no'}  printer={printername}")
            self._save_session()
            self._octo_set_ui_status(f"Connected to {printername} — starting...")
            # Kill any previous session's WS/camera before starting fresh
            self._ws_running = False
            self._camera_running = False
            self.octo_mainboard_id = None
            self.stop_octo_control_poll()
            threading.Thread(target=self._warmup_then_start_octo, daemon=True).start()
        except Exception:
            log("callback parse error: " + traceback.format_exc())
            self._octo_set_ui_status("Callback parse failed")

    # ── Auth helpers ──────────────────────────────────────────────────────────

    def _octo_headers(self) -> dict:
        h = {"User-Agent": "CentauriLink/1.0"}
        if self.octo_bearer_token:
            h["Authorization"] = f"Bearer {self.octo_bearer_token}"
        return h

    def _octo_auth(self):
        if self.octo_basic_user and self.octo_basic_pass:
            return (self.octo_basic_user, self.octo_basic_pass)
        return None

    def _octo_set_ui_status(self, text: str):
        try: self.root.get_screen("octo").status_text = text
        except Exception: pass
        log("[OCTO] " + text)

    # ── Session ───────────────────────────────────────────────────────────────

    def _save_session(self):
        try:
            with open(SESSION_PATH, "w", encoding="utf8") as f:
                json.dump({
                    "octo_base_url": self.octo_base_url,
                    "octo_bearer_token": self.octo_bearer_token,
                    "octo_basic_user": self.octo_basic_user,
                    "octo_basic_pass": self.octo_basic_pass,
                    "saved_at": int(time.time()),
                }, f)
            log("Session saved")
        except Exception:
            log("save session error: " + traceback.format_exc())

    def _load_saved_session_and_autostart(self):
        try:
            if not os.path.exists(SESSION_PATH):
                log("No saved session"); return
            with open(SESSION_PATH, "r", encoding="utf8") as f:
                d = json.load(f)
            self.octo_base_url     = d.get("octo_base_url")
            self.octo_bearer_token = d.get("octo_bearer_token")
            self.octo_basic_user   = d.get("octo_basic_user")
            self.octo_basic_pass   = d.get("octo_basic_pass")
            if self.octo_base_url:
                log("Loaded session — auto-starting")
                Clock.schedule_once(
                    lambda dt: threading.Thread(
                        target=self._warmup_then_start_octo, daemon=True).start(), 0.5)
        except Exception:
            log("load session error: " + traceback.format_exc())

    def octo_disconnect(self):
        self._ws_running = False; self._camera_running = False
        self.octo_base_url = self.octo_bearer_token = None
        self.octo_basic_user = self.octo_basic_pass = None
        self.octo_mainboard_id = None
        try:
            if os.path.exists(SESSION_PATH): os.remove(SESSION_PATH)
        except Exception: pass
        self.stop_octo_control_poll()
        def reset():
            self.octo_state = "(not connected)"; self.octo_progress = 0
            self.octo_file  = ""; self.octo_timestamp = "-"
            self.octo_nozzle_temp = "N/A"; self.octo_bed_temp = "N/A"
            self.octo_chamber_temp = "N/A"
            self.octo_nozzle_value = 0; self.octo_bed_value = 0; self.octo_chamber_value = 0
            try: self.root.get_screen("octo").status_text = "Disconnected"
            except Exception: pass
        Clock.schedule_once(lambda dt: reset(), 0)

    # ── Warmup ────────────────────────────────────────────────────────────────

    def _warmup_then_start_octo(self):
        """
        Probe the OE tunnel via the Elegoo REST monitor endpoint.
        Also extracts MainboardID for use in SDCP WS commands.
        """
        if not self.octo_base_url:
            log("warmup: no base URL"); return

        probe   = self.octo_base_url.rstrip("/") + MONITOR_PATH
        headers = self._octo_headers()
        auth    = self._octo_auth()
        success = False

        for attempt in range(1, 7):
            try:
                log(f"warmup attempt {attempt}: GET {probe}")
                r = requests.get(probe, headers=headers, auth=auth,
                                 timeout=8, verify=False)
                log(f"warmup: http {r.status_code}")
                if r.status_code == 200:
                    body = r.text.strip()
                    if body:
                        try:
                            d = r.json()
                            all_data = extract_all_from_message(
                                d if isinstance(d, dict) else {"Status": d})
                            if all_data.get("mainboard_id"):
                                self.octo_mainboard_id = all_data["mainboard_id"]
                                log(f"warmup: MainboardID = {self.octo_mainboard_id}")
                            Clock.schedule_once(
                                lambda dt, v=all_data: self._apply_octo_updates(v), 0)
                        except Exception:
                            log("warmup: parse error: " + traceback.format_exc())
                    else:
                        log("warmup: http 200 but empty body (tunnel not yet proxying SDCP)")
                    success = True; break
                elif r.status_code in (401, 403):
                    log("warmup: auth rejected"); break
            except Exception as e:
                log(f"warmup attempt {attempt} error: {e}")
            time.sleep(min(2 ** attempt, 8))

        if success:
            log("warmup: OK — launching camera + WS + poll")
            Clock.schedule_once(lambda dt: self._octo_set_ui_status("Connected"), 0)
        else:
            log("warmup: failed — launching services anyway")
            Clock.schedule_once(
                lambda dt: self._octo_set_ui_status("Tunnel may not be ready — retrying..."), 0)

        Clock.schedule_once(lambda dt: self._start_octo_camera(), 0)
        Clock.schedule_once(
            lambda dt: threading.Thread(
                target=self._start_octo_websocket, daemon=True).start(), 0.5)
        Clock.schedule_once(lambda dt: self.start_octo_control_poll(), 1.0)

    # ── REST poll (fallback / supplement to WS) ───────────────────────────────

    def _octo_control_fetch_once(self):
        if not self.octo_base_url: return
        url     = self.octo_base_url.rstrip("/") + MONITOR_PATH
        headers = self._octo_headers()
        auth    = self._octo_auth()
        try:
            r = requests.get(url, headers=headers, auth=auth, timeout=8, verify=False)
            if r.status_code == 200:
                try:
                    d = r.json()
                except Exception:
                    d = {"Status": r.text}
                all_data = extract_all_from_message(
                    d if isinstance(d, dict) else {"Status": d})
                if all_data.get("mainboard_id") and not self.octo_mainboard_id:
                    self.octo_mainboard_id = all_data["mainboard_id"]
                    log(f"REST poll: learned MainboardID = {self.octo_mainboard_id}")
                Clock.schedule_once(lambda dt, v=all_data: self._apply_octo_updates(v), 0)
                log("octo REST poll: OK")
            else:
                log(f"octo REST poll: http {r.status_code}")
                if r.status_code >= 600:
                    Clock.schedule_once(
                        lambda dt, s=r.status_code:
                            self._octo_set_ui_status(
                                f"OE error {s} — docs.octoeverywhere.com/error-codes"), 0)
        except Exception:
            log("octo REST poll error: " + traceback.format_exc())

    def start_octo_control_poll(self):
        if self._octo_poll_event: return
        threading.Thread(target=self._octo_control_fetch_once, daemon=True).start()
        self._octo_poll_event = Clock.schedule_interval(
            lambda dt: threading.Thread(
                target=self._octo_control_fetch_once, daemon=True).start(),
            OCTO_CONTROL_POLL)
        log("Started OE REST poll")

    def stop_octo_control_poll(self):
        if self._octo_poll_event:
            try: self._octo_poll_event.cancel()
            except Exception:
                try: Clock.unschedule(self._octo_poll_event)
                except Exception: pass
            self._octo_poll_event = None; log("Stopped OE REST poll")

    # ── Camera ────────────────────────────────────────────────────────────────

    def _start_octo_camera(self):
        if self._camera_running: return
        self._camera_thread = threading.Thread(
            target=self._camera_stream_loop, daemon=True)
        self._camera_thread.start()

    def _camera_stream_loop(self):
        if not self.octo_base_url:
            log("camera: no base URL"); return
        self._camera_running = True
        base    = self.octo_base_url.rstrip("/")
        headers = self._octo_headers()
        auth    = self._octo_auth()

        # OE /video endpoint is preferred; fall back to legacy paths
        candidates = [
            _make_octo_video_url(base),           # https://<host>/video
            base + OE_WEBCAM_STREAM_PATH,          # legacy OE webcam path
            base + ELEGOO_CAMERA_PATH,             # direct Elegoo camera
        ]
        log(f"camera: candidates = {candidates}")
        backoff = 1

        while self._camera_running:
            stream_url = None; r_stream = None
            for c in candidates:
                try:
                    probe = requests.get(c, headers=headers, auth=auth,
                                         stream=True, timeout=6, verify=False)
                    if probe.status_code == 200:
                        stream_url = c; r_stream = probe
                        log(f"camera: streaming {stream_url}"); break
                    probe.close(); log(f"camera: {c} → {probe.status_code}")
                except Exception as e:
                    log(f"camera: probe {c} failed: {e}")

            if stream_url is None:
                self._set_octo_camera_error("No camera stream (retrying)")
                time.sleep(backoff); backoff = min(backoff * 2, 30); continue

            backoff = 1; last_frame = time.time(); buf = b""
            try:
                for chunk in r_stream.iter_content(chunk_size=4096):
                    if not self._camera_running: break
                    if not chunk: continue
                    buf += chunk
                    a = buf.find(b"\xff\xd8"); b_idx = buf.find(b"\xff\xd9")
                    if a != -1 and b_idx != -1 and b_idx > a:
                        jpg = buf[a:b_idx + 2]; buf = buf[b_idx + 2:]
                        self._decode_and_push_frame(jpg)
                        last_frame = time.time()
                    if time.time() - last_frame > 10:
                        log("camera: no frames 10s — reconnecting")
                        self._set_octo_camera_error("No frames (reconnecting)..."); break
                try: r_stream.close()
                except Exception: pass
                time.sleep(0.3)
            except requests.exceptions.SSLError as e:
                log("camera SSL: " + str(e)); self._set_octo_camera_error("Camera SSL error"); time.sleep(3)
            except requests.exceptions.RequestException as e:
                log("camera req: " + str(e)); self._set_octo_camera_error("Camera error (retrying)"); time.sleep(2)
            except Exception:
                log("camera: " + traceback.format_exc()); self._set_octo_camera_error("Camera error (retrying)"); time.sleep(2)

        log("camera exit"); self._camera_running = False

    def _decode_and_push_frame(self, jpg: bytes):
        decoded = False
        try:
            if PIL_AVAILABLE:
                img = PILImage.open(BytesIO(jpg)).convert("RGB")
                w, h = img.size; raw = img.tobytes()
                tex = Texture.create(size=(w, h), colorfmt='rgb')
                tex.blit_buffer(raw, colorfmt='rgb', bufferfmt='ubyte')
                tex.flip_vertical()
                Clock.schedule_once(lambda dt, t=tex: self._update_octo_camera_texture(t), 0)
                decoded = True
        except Exception:
            log("camera PIL: " + traceback.format_exc())
        if not decoded:
            try:
                ci = CoreImage(BytesIO(jpg), ext="jpg")
                Clock.schedule_once(lambda dt, c=ci: self._update_octo_camera_coreimage(c), 0)
                decoded = True
            except Exception:
                log("camera CoreImage: " + traceback.format_exc())
        if not decoded:
            try:
                sp = os.path.join(APP_STORAGE, "camera_bad_sample.jpg")
                with open(sp, "wb") as f: f.write(jpg[:4096])
                log(f"camera: bad sample saved to {sp}")
            except Exception: pass

    def _set_octo_camera_error(self, msg):
        try: self.root.get_screen("octo").status_text = msg
        except Exception: pass
        log("[OCTO CAM] " + msg)

    def _update_octo_camera_texture(self, texture: Texture):
        try:
            octo = self.root.get_screen("octo")
            try: img = octo.ids.get("octo_camera")
            except Exception: img = None
            if img: img.texture = texture; return
            self._replace_camera_placeholder(octo, texture=texture)
        except Exception:
            log("camera UI: " + traceback.format_exc())

    def _update_octo_camera_coreimage(self, ci):
        try:
            octo = self.root.get_screen("octo")
            try: img = octo.ids.get("octo_camera")
            except Exception: img = None
            if img: img.texture = ci.texture; return
            self._replace_camera_placeholder(octo, coreimage=ci)
        except Exception:
            log("camera coreimage UI: " + traceback.format_exc())

    def _replace_camera_placeholder(self, screen, texture=None, coreimage=None):
        found = None
        for child in screen.walk(restrict=True):
            try:
                if "Camera preview" in getattr(child, "text", ""):
                    found = child; break
            except Exception: pass
        if found and found.parent:
            parent = found.parent
            try: parent.remove_widget(found)
            except Exception: pass
            w = KivyImage()
            w.id = "octo_camera"; w.allow_stretch = True; w.keep_ratio = True
            if texture:    w.texture = texture
            elif coreimage: w.texture = coreimage.texture
            parent.add_widget(w)
        else:
            log("camera: no placeholder widget in UI")

    # ── OctoEverywhere WebSocket (SDCP transparent relay) ─────────────────────

    def _send_subscribe_cmd(self, ws):
        """
        Send SDCP Cmd 512 — the subscribe / status-push command.

        This MUST be sent immediately after the WebSocket connection is opened.
        Without it the printer stays silent and never pushes status updates.

        Cmd 512 data: {"TimePeriod": 5000}   (milliseconds between push updates)
        Confirmed by OctoEverywhere founder as the correct subscribe command.

        If MainboardID is not yet known (warmup hasn't completed), we spin-wait
        in a background thread and retry until we have it (or give up after 20s).
        """
        mid = self.octo_mainboard_id or ""
        if mid:
            try:
                pkt = make_sdcp_cmd(512, {"TimePeriod": 5000}, mid)
                ws.send(pkt)
                log(f"WS: sent Cmd 512 subscribe (TimePeriod=5000) for mainboard {mid}")
            except Exception as e:
                log("WS: Cmd 512 send failed: " + str(e))
        else:
            log("WS: MainboardID not yet known — deferring Cmd 512 subscribe")
            def _deferred():
                deadline = time.time() + 20
                while time.time() < deadline:
                    time.sleep(1)
                    mid2 = self.octo_mainboard_id or ""
                    if mid2:
                        try:
                            pkt = make_sdcp_cmd(512, {"TimePeriod": 5000}, mid2)
                            ws.send(pkt)
                            log(f"WS: deferred Cmd 512 subscribe sent for mainboard {mid2}")
                        except Exception as e:
                            log("WS: deferred Cmd 512 failed: " + str(e))
                        return
                log("WS: gave up waiting for MainboardID for Cmd 512")
            threading.Thread(target=_deferred, daemon=True).start()

    def _start_octo_websocket(self):
        """
        Connect to the Elegoo SDCP WebSocket through the OE tunnel.

        URL CONSTRUCTION:
          HTTP base (from App Portal): https://app-abc123.octoeverywhere.com
          WSS URL (scheme swap only):  wss://app-abc123.octoeverywhere.com/websocket
          Hostname is used exactly as returned by the portal — no mutation.

        AUTH:
          The OE tunnel proxy requires the Bearer token on the HTTP Upgrade
          request (WS header), just like any other OE HTTP request.
          The SDCP protocol itself needs no authentication once the WS is open.

        ON OPEN — must send Cmd 512 immediately:
          {"Cmd": 512, "Data": {"TimePeriod": 5000}, "MainboardID": "...", ...}
          This subscribes the printer to push status updates every 5 seconds.
          Without this the printer never sends any data.

        INCOMING messages:
          {"Id":..., "Data": {"Status": {...}, "MainboardID": "...", "TimeStamp":...}}
          TempOfNozzle / TempOfHotbed are [target, actual] arrays — index[1] = actual.
        """
        if not WS_AVAILABLE:
            log("websocket-client not available"); return
        if not self.octo_base_url or self._ws_running:
            return

        ws_url = _make_octo_ws_url(self.octo_base_url)
        self._ws_running = True

        if DEBUG_WS:
            try: websocket.enableTrace(True)
            except Exception: pass

        def run_loop():
            backoff = 1
            while self._ws_running:
                log(f"octo ws: connecting {ws_url}  bearer={'yes' if self.octo_bearer_token else 'NO — auth will fail'}")

                # OE tunnel proxy requires Bearer on the HTTP Upgrade request.
                conn_headers = ["User-Agent: CentauriLink/1.0"]
                if self.octo_bearer_token:
                    conn_headers.append(f"Authorization: Bearer {self.octo_bearer_token}")

                def on_open(ws):
                    log("octo ws: opened")
                    self._octo_set_ui_status("WS connected — subscribing...")
                    # Send subscribe command immediately — printer won't push without it
                    self._send_subscribe_cmd(ws)

                def on_close(ws, code, msg):
                    log(f"octo ws: CLOSED  code={code!r}  msg={msg!r}")
                    # Common close codes:
                    #   1000 normal, 1001 going away, 1006 abnormal (no close frame — ping timeout or network drop)
                    #   4000-4999 app-level (OE: 4001=auth, 4003=forbidden, 4004=not found)
                    if code == 1006:
                        log("octo ws: code 1006 = abnormal close — likely ping timeout or network drop")
                    elif code in (4001, 4003):
                        log("octo ws: OE auth/forbidden — bearer token may be invalid or expired")
                    self._octo_set_ui_status(f"WS closed ({code}) — reconnecting...")

                def on_error(ws, err):
                    err_str = str(err)
                    err_type = type(err).__name__
                    log(f"octo ws: ERROR  type={err_type}  detail={err_str}")
                    if "ping" in err_str.lower() or "pong" in err_str.lower() or "timeout" in err_str.lower():
                        log("octo ws: ping/pong timeout — OE tunnel does not respond to WS pings, disable ping")
                    elif "certificate" in err_str.lower() or "ssl" in err_str.lower():
                        log("octo ws: SSL error — cert verification still active somehow")
                    elif "address" in err_str.lower() or "resolve" in err_str.lower():
                        log("octo ws: DNS failure — hostname cannot be resolved")
                    elif "refused" in err_str.lower():
                        log("octo ws: connection refused — OE tunnel endpoint not reachable")

                def on_message(ws, message):
                    try:
                        raw = (message if isinstance(message, str)
                               else message.decode("utf-8", errors="replace"))
                        try:
                            with open(WS_RAW_LOG, "a", encoding="utf8") as f:
                                f.write(datetime.now().isoformat() + " :: " + raw[:2000] + "\n\n")
                        except Exception: pass

                        m = raw.strip()
                        # Strip optional numeric length prefix (some SDCP implementations)
                        if m and m[0].isdigit() and not m.startswith("{"):
                            idx = next((i for i, c in enumerate(m) if c in ('{', '[')), None)
                            if idx is not None: m = m[idx:]

                        objs = _parse_json_objects_from_string(m)
                        if not objs:
                            try:
                                with open(WS_UNPARSED_LOG, "a", encoding="utf8") as f:
                                    f.write(datetime.now().isoformat() + " :: " + m[:500] + "\n\n")
                            except Exception: pass
                            return

                        for p in objs:
                            if not isinstance(p, dict): continue

                            topic = p.get("Topic") or p.get("topic") or ""

                            # ── sdcp/attributes — printer announces itself on connect ──
                            # This is the FIRST message the printer sends.
                            # It contains the MainboardID we need for Cmd 512.
                            # Extract it and fire the subscribe immediately.
                            if isinstance(topic, str) and "sdcp/attributes" in topic:
                                mid = (p.get("MainboardID")
                                       or (p.get("Attributes") or {}).get("MainboardID"))
                                if mid:
                                    if not self.octo_mainboard_id:
                                        self.octo_mainboard_id = str(mid)
                                        log(f"octo ws: learned MainboardID from attributes = {mid}")
                                    log("octo ws: attributes received — sending Cmd 512 subscribe")
                                    self._send_subscribe_cmd(ws)
                                attrs = p.get("Attributes") or {}
                                if attrs:
                                    name = attrs.get("Name") or attrs.get("MachineName") or ""
                                    fw   = attrs.get("FirmwareVersion") or ""
                                    Clock.schedule_once(
                                        lambda dt, n=name, f=fw:
                                            self._octo_set_ui_status(
                                                f"Connected: {n} fw={f}" if n else "WS connected"), 0)
                                continue

                            # Cache MainboardID from any other message too
                            mid = (p.get("MainboardID")
                                   or (p.get("Data") or {}).get("MainboardID"))
                            if mid and not self.octo_mainboard_id:
                                self.octo_mainboard_id = str(mid)
                                log(f"octo ws: learned MainboardID = {mid}")

                            if isinstance(topic, str) and "sdcp/error" in topic:
                                log("octo ws SDCP error: " + json.dumps(p)[:300])

                            # Handle command acknowledgement responses
                            data_inner = p.get("Data") or {}
                            if isinstance(data_inner, dict) and "Cmd" in data_inner:
                                cmd = data_inner.get("Cmd")
                                ack = (data_inner.get("Data") or {}).get("Ack")
                                log(f"octo ws: Cmd {cmd} ack={ack}")
                                if cmd == 512 and ack == 0:
                                    log("octo ws: Cmd 512 subscribe confirmed — status pushes active")
                                    self._octo_set_ui_status("WS subscribed — receiving data")
                                continue

                            st_wrapper = _status_from_parsed_object(p)
                            if st_wrapper:
                                all_data = extract_all_from_message(st_wrapper)
                                Clock.schedule_once(
                                    lambda dt, d=all_data: self._apply_octo_updates(d), 0)
                    except Exception:
                        log("ws on_message error: " + traceback.format_exc())

                try:
                    ws_app = websocket.WebSocketApp(
                        ws_url, header=conn_headers,
                        on_open=on_open, on_close=on_close,
                        on_error=on_error, on_message=on_message)

                    # Android does not honour ssl_context in sslopt reliably.
                    # Use the flat dict form which patches the underlying
                    # ssl.wrap_socket call directly — works on all platforms.
                    sslopt = {
                        "cert_reqs": ssl.CERT_NONE,
                        "check_hostname": False,
                    }
                    # Also patch the global default so any fallback path is covered
                    try:
                        ssl._create_default_https_context = ssl._create_unverified_context
                    except Exception: pass

                    # ping_interval=0 disables the ping/pong keepalive entirely.
                    # OctoEverywhere's tunnel does NOT respond to websocket ping frames,
                    # so any non-zero ping_timeout will cause a 1006 disconnect after
                    # ping_timeout seconds of silence (typically ~5s after the last message).
                    ws_app.run_forever(sslopt=sslopt, ping_interval=0)
                except Exception:
                    log("ws run loop: " + traceback.format_exc())

                if not self._ws_running: break
                log(f"ws: reconnecting in {backoff}s")
                time.sleep(backoff); backoff = min(backoff * 2, 30)

            log("ws loop ended"); self._ws_running = False

        self._ws_thread = threading.Thread(target=run_loop, daemon=True)
        self._ws_thread.start()

    # ── Apply OE updates to UI ────────────────────────────────────────────────

    def _apply_octo_updates(self, all_data):
        try:
            temps = all_data.get("temps", {})    or {}
            pi    = all_data.get("printinfo", {}) or {}
            ts    = all_data.get("timestamp")

            if temps.get("nozzle") is not None:
                self.octo_nozzle_temp  = f"{temps['nozzle']:.1f}"
                self.octo_nozzle_value = float(temps["nozzle"])
            if temps.get("bed") is not None:
                self.octo_bed_temp     = f"{temps['bed']:.1f}"
                self.octo_bed_value    = float(temps["bed"])
            if temps.get("chamber") is not None:
                self.octo_chamber_temp  = f"{temps['chamber']:.1f}"
                self.octo_chamber_value = float(temps["chamber"])

            status_text = (pi.get("status_text")
                           or (interpret_status_code(pi["status_code"])
                               if pi.get("status_code") is not None else None))
            prog_val = float(pi["progress"]) if pi.get("progress") is not None else 0.0

            if status_text:
                self.octo_state = status_text
            try:
                self.octo_progress = max(0.0, min(100.0, prog_val))
            except Exception: pass

            new_file = str(pi.get("file") or "")
            if new_file: self.octo_file = new_file

            self.octo_timestamp = ts or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            try:
                self.root.get_screen("octo").status_text = (
                    f"{self.octo_state} • {int(self.octo_progress)}%")
            except Exception: pass
        except Exception:
            log("apply_octo_updates error: " + traceback.format_exc())

    # ── Local LAN printer ─────────────────────────────────────────────────────

    def _local_start_ws(self, host: str):
        if not WS_AVAILABLE:
            log("websocket-client not available"); return
        url = f"ws://{host}:{WS_PORT}{WS_PATH}"
        log(f"Local WS: {url}")

        def on_open(ws):
            self.local_ws_running = True
            self._set_control_status("Connected (WebSocket)")
            self.stop_local_polling()

        def on_close(ws, *args):
            self.local_ws_running = False
            self._set_control_status("WS disconnected")
            self.start_local_polling()

        def on_error(ws, err):
            self.local_ws_running = False
            self._set_control_status("WS error"); log("local ws error: " + str(err))
            self.start_local_polling()

        def on_message(ws, message):
            try:
                objs = _parse_json_objects_from_string(
                    message if isinstance(message, str) else str(message))
                for p in objs:
                    st = _status_from_parsed_object(p)
                    if st:
                        Clock.schedule_once(
                            lambda dt, d=extract_all_from_message(st):
                                self._apply_local_updates(d, "WS"), 0)
                        return
            except Exception:
                log("local ws on_message: " + traceback.format_exc())

        try:
            self.local_ws_app = websocket.WebSocketApp(
                url, on_open=on_open, on_close=on_close,
                on_error=on_error, on_message=on_message)
        except Exception:
            log("Failed to create local_ws_app: " + traceback.format_exc()); return

        def run_ws():
            try: self.local_ws_app.run_forever()
            except Exception: log("local ws run_forever: " + traceback.format_exc())
            finally: self.local_ws_running = False; self.start_local_polling()

        self.local_ws_thread = threading.Thread(target=run_ws, daemon=True)
        self.local_ws_thread.start()

    def _local_stop_ws(self):
        try:
            if self.local_ws_app: self.local_ws_app.close()
        except Exception: pass
        self.local_ws_app = None; self.local_ws_running = False

    def _local_http_fetch(self):
        host = self.local_printer_ip
        if not host: return
        try:
            r = requests.get(f"http://{host}{MONITOR_PATH}", timeout=3.0)
            if r.status_code == 200:
                try: d = r.json()
                except Exception: d = {"Status": r.text}
                all_data = extract_all_from_message(d if isinstance(d, dict) else {"Status": d})
                Clock.schedule_once(lambda dt: self._apply_local_updates(all_data, "HTTP"), 0)
                self._set_control_status("Connected (HTTP monitor)")
            else:
                self._set_control_status(f"HTTP {r.status_code}")
        except Exception:
            self._set_control_status("No response")

    def _set_control_status(self, text: str):
        try: self.root.get_screen("print").status_text = text
        except Exception: pass
        log("[LOCAL] " + text)

    def _apply_local_updates(self, all_data, source=""):
        try:
            temps = all_data.get("temps", {})    or {}
            pi    = all_data.get("printinfo", {}) or {}
            if temps.get("nozzle") is not None:
                self.nozzle_temp  = f"{temps['nozzle']:.1f}"; self.nozzle_value = float(temps["nozzle"])
            if temps.get("bed") is not None:
                self.bed_temp    = f"{temps['bed']:.1f}";    self.bed_value   = float(temps["bed"])
            if temps.get("chamber") is not None:
                self.chamber_temp  = f"{temps['chamber']:.1f}"; self.chamber_value = float(temps["chamber"])

            sc   = pi.get("status_code")
            stxt = (pi.get("status_text") or
                    (interpret_status_code(sc) if sc is not None else "(unknown)"))
            prog = float(pi["progress"]) if pi.get("progress") is not None else 0.0

            self.print_state       = str(stxt)
            self.print_progress    = max(0.0, min(100.0, prog))
            self.print_layer       = str(pi.get("current_layer") or "")
            self.print_total_layer = str(pi.get("total_layer")   or "")
            self.coords            = all_data.get("coords") or "-"
            self.light_state       = json.dumps(all_data["lights"]) if all_data.get("lights") else "-"
            self.status_timestamp  = (all_data.get("timestamp") or
                                      datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            try:
                cs = self.root.get_screen("print")
                cs.current_temp = (f"{source}: Nozzle {self.nozzle_temp}°C  "
                                   f"Bed {self.bed_temp}°C  Chamber {self.chamber_temp}°C")
            except Exception: pass
        except Exception:
            log("apply_local_updates: " + traceback.format_exc())

    def start_local_polling(self):
        if self._local_poll_event: return
        self._local_poll_event = Clock.schedule_interval(
            lambda dt: threading.Thread(
                target=self._local_http_fetch, daemon=True).start(),
            POLL_INTERVAL)
        log("Started local HTTP polling")

    def stop_local_polling(self):
        if self._local_poll_event:
            try: self._local_poll_event.cancel()
            except Exception:
                try: Clock.unschedule(self._local_poll_event)
                except Exception: pass
            self._local_poll_event = None; log("Stopped local HTTP polling")

    def open_url(self, url: str):
        try: webbrowser.open(url)
        except Exception: log("open_url: " + traceback.format_exc())


if __name__ == "__main__":
    log("Starting CentauriApp — storage: " + APP_STORAGE)
    if not WS_AVAILABLE:  log("websocket-client not available (HTTP fallback ok).")
    if not PIL_AVAILABLE: log("Pillow not available (CoreImage fallback).")
    CentauriApp().run()
