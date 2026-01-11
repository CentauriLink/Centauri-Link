# main.py
# CentauriLink - final application glue (WS, HTTP fallback, MJPEG camera streamer, UI bindings)

import json
import threading
import time
import re
import traceback
import socket
import webbrowser
import uuid
from io import BytesIO
from datetime import datetime

import requests
from PIL import Image as PILImage

from kivy.app import App
from kivy.lang import Builder
from kivy.clock import Clock
from kivy.graphics.texture import Texture
from kivy.properties import (
    StringProperty, NumericProperty, BooleanProperty
)
from kivy.uix.screenmanager import ScreenManager, Screen

# websocket-client (optional)
try:
    import websocket
    WS_AVAILABLE = True
except Exception:
    WS_AVAILABLE = False

KV_FILE = "centaurilink.kv"
WS_PORT = 3030
WS_PATH = "/websocket"
MONITOR_PATH = "/network-device-manager/network/monitor"
CONTROL_PATH = "/network-device-manager/network/control"
POLL_INTERVAL = 1.0

# UDP discovery for Centauri / SDCP
UDP_DISCOVERY_PORT = 3000
UDP_DISCOVERY_MAGIC = b"M99999"  # replies with JSON including MainboardID in many SDCP implementations


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open("centauri_debug.log", "a", encoding="utf8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass
    print(f"[Centauri] {ts} {msg}")


def safe_float(v):
    try:
        return float(v)
    except Exception:
        return None


def interpret_status_code(code):
    mapping = {0: "Idle", 1: "Preparing", 2: "Homing", 3: "Printing", 4: "Pausing",
               5: "Paused", 6: "Stopping", 7: "Stopped", 8: "Busy", 9: "Complete"}
    try:
        return mapping.get(int(code), f"Status {code}")
    except Exception:
        return str(code)


def extract_all_from_message(obj):
    result = {
        "temps": {"nozzle": None, "bed": None, "chamber": None},
        "fans": {"ModelFan": None, "AuxiliaryFan": None, "BoxFan": None},
        "printinfo": {"status_code": None, "status_text": None, "progress": None, "current_layer": None, "total_layer": None},
        "coords": None,
        "lights": None,
        "timestamp": None
    }
    try:
        st = obj.get("Status", {}) if isinstance(obj, dict) else {}
        if "TempOfNozzle" in st:
            result["temps"]["nozzle"] = safe_float(st.get("TempOfNozzle"))
        if "TempOfHotbed" in st:
            result["temps"]["bed"] = safe_float(st.get("TempOfHotbed"))
        if "TempOfBox" in st:
            result["temps"]["chamber"] = safe_float(st.get("TempOfBox"))

        cf = st.get("CurrentFanSpeed")
        if isinstance(cf, dict):
            for k in ("ModelFan", "AuxiliaryFan", "BoxFan"):
                if k in cf:
                    try:
                        result["fans"][k] = int(cf.get(k))
                    except Exception:
                        result["fans"][k] = None

        pi = st.get("PrintInfo")
        if isinstance(pi, dict):
            sc = pi.get("Status")
            scv = sc[0] if isinstance(sc, (list, tuple)) and sc else sc
            try:
                result["printinfo"]["status_code"] = int(scv) if scv is not None else None
            except Exception:
                result["printinfo"]["status_code"] = scv
            result["printinfo"]["status_text"] = interpret_status_code(result["printinfo"]["status_code"]) if result["printinfo"]["status_code"] is not None else None
            try:
                result["printinfo"]["progress"] = float(pi.get("Progress")) if pi.get("Progress") is not None else None
            except Exception:
                result["printinfo"]["progress"] = None
            result["printinfo"]["current_layer"] = pi.get("CurrentLayer")
            result["printinfo"]["total_layer"] = pi.get("TotalLayer")

        cc = st.get("CurrenCoord") or st.get("CurrentCoord") or st.get("CurrCoord")
        if cc:
            result["coords"] = str(cc)
        ls = st.get("LightStatus")
        if ls:
            result["lights"] = ls

        ts = obj.get("TimeStamp") or obj.get("timestamp")
        if ts:
            try:
                t = int(ts)
                result["timestamp"] = datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                result["timestamp"] = str(ts)
    except Exception:
        log("extract_all_from_message error: " + traceback.format_exc())

    # fallback walk for temps
    if not any(result["temps"].values()):
        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    kl = str(k).lower()
                    if isinstance(v, (dict, list)):
                        walk(v)
                    else:
                        try:
                            if any(x in kl for x in ("nozzle", "hotend", "extruder")) and result["temps"]["nozzle"] is None:
                                result["temps"]["nozzle"] = safe_float(v)
                            if any(x in kl for x in ("hotbed", "bed", "heatbed")) and result["temps"]["bed"] is None:
                                result["temps"]["bed"] = safe_float(v)
                            if any(x in kl for x in ("box", "chamber", "enclosure")) and result["temps"]["chamber"] is None:
                                result["temps"]["chamber"] = safe_float(v)
                        except Exception:
                            pass
            elif isinstance(o, list):
                for i in o:
                    walk(i)
        walk(obj)
    return result


class MJPEGStreamer:
    """
    Robust MJPEG stream reader.
    Fixes common "black camera" issues by:
    - probing multiple common endpoints
    - logging HTTP status + content-type
    - keeping buffer bounded
    - extracting multiple frames per chunk
    - auto-retrying if the stream disconnects
    """
    def __init__(self):
        self._thread = None
        self._running = False
        self._ip = None
        self._widget = None
        self._last_error = ""
        self._active_url = ""

    def start(self, widget, ip: str, fps_limit=8):
        if not ip or not widget:
            return
        if self._running and ip == self._ip:
            return
        if self._running:
            self.stop()
        self._ip = ip
        self._widget = widget
        self._running = True
        self._thread = threading.Thread(target=self._run, args=(fps_limit,), daemon=True)
        self._thread.start()
        log(f"MJPEG start {ip}")

    def stop(self):
        self._running = False
        self._ip = None
        self._widget = None
        self._active_url = ""
        log("MJPEG stop")

    def _probe_camera_url(self, ip: str):
        # Try a few common camera endpoints (your current is /video).
        candidates = [
            f"http://{ip}:3031/video",
            f"http://{ip}:3031/mjpeg",
            f"http://{ip}:3031/stream",
            f"http://{ip}:3031/video.mjpg",
            f"http://{ip}:3031/snapshot",
            f"http://{ip}:3031/shot.jpg",
        ]
        headers = {"User-Agent": "CentauriLink/1.0"}
        for url in candidates:
            try:
                r = requests.get(url, stream=True, timeout=3, headers=headers)
                ct = (r.headers.get("Content-Type") or "").lower()
                log(f"Camera probe {url} -> {r.status_code} {ct}")
                if r.status_code == 200 and ("multipart" in ct or "jpeg" in ct or "jpg" in ct):
                    try:
                        r.close()
                    except Exception:
                        pass
                    return url
                try:
                    r.close()
                except Exception:
                    pass
            except Exception:
                pass
        return candidates[0]  # fallback to default

    def _run(self, fps_limit):
        if not self._ip:
            return

        self._active_url = self._probe_camera_url(self._ip)
        url = self._active_url

        headers = {"User-Agent": "CentauriLink/1.0"}
        buffer = b""
        last_frame = 0.0

        # retry loop so camera can recover
        while self._running and self._ip:
            try:
                r = requests.get(url, stream=True, timeout=6, headers=headers)
                ct = (r.headers.get("Content-Type") or "").lower()
                if r.status_code != 200:
                    self._last_error = f"HTTP {r.status_code}"
                    log(f"MJPEG HTTP error {r.status_code} at {url}")
                    try:
                        r.close()
                    except Exception:
                        pass
                    time.sleep(1.0)
                    continue

                log(f"MJPEG connected {url} content-type={ct}")
                self._last_error = ""

                for chunk in r.iter_content(chunk_size=4096):
                    if not self._running:
                        break
                    if not chunk:
                        continue

                    buffer += chunk

                    # Keep buffer bounded (avoid memory blowup)
                    if len(buffer) > 2_000_000:
                        buffer = buffer[-500_000:]

                    # Extract ALL complete JPEG frames from buffer
                    while True:
                        a = buffer.find(b"\xff\xd8")  # SOI
                        if a == -1:
                            break
                        b = buffer.find(b"\xff\xd9", a + 2)  # EOI
                        if b == -1:
                            break

                        jpg = buffer[a:b + 2]
                        buffer = buffer[b + 2:]

                        now = time.time()
                        if fps_limit and (now - last_frame) < (1.0 / float(fps_limit)):
                            continue
                        last_frame = now

                        self._update_widget_texture(jpg)

                try:
                    r.close()
                except Exception:
                    pass

            except Exception:
                self._last_error = "Stream error"
                log("MJPEG run error: " + traceback.format_exc())

            # If we get here, connection dropped or errored — retry
            if self._running:
                time.sleep(1.0)

        self._running = False

    def _update_widget_texture(self, jpg_bytes):
        try:
            pil = PILImage.open(BytesIO(jpg_bytes)).convert("RGB")
            tex = Texture.create(size=pil.size)
            tex.blit_buffer(pil.tobytes(), colorfmt="rgb", bufferfmt="ubyte")
            tex.flip_vertical()
            if self._widget:
                Clock.schedule_once(lambda dt: setattr(self._widget, "texture", tex))
        except Exception:
            log("MJPEG update error: " + traceback.format_exc())


class HomeScreen(Screen):
    def start_camera(self):
        app = App.get_running_app()
        if not app.printer_ip:
            app._set_status("No IP selected")
            return
        try:
            img = self.ids.camera_view
        except Exception:
            img = None
        if img:
            app.mjpeg.start(img, app.printer_ip)
            app._set_status("Camera started (MJPEG)")
        else:
            app._set_status("Camera widget not found")


class PrintScreen(Screen):
    status_text = StringProperty("Ready")
    selected_ip = StringProperty("")
    selected_port = StringProperty("")
    current_temp = StringProperty("N/A")
    auto_poll = BooleanProperty(False)
    _poll_event = None
    mainboard_id = StringProperty("")

    def on_kv_post(self, base_widget):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            self.status_text = f"Local IP: {ip}"
        except Exception:
            self.status_text = "Ready"

    def add_manual_printer(self, iptext: str):
        ip = (iptext or "").strip()
        if not ip:
            self.status_text = "Empty address"
            return
        try:
            socket.gethostbyname(ip)
        except Exception:
            self.status_text = "Invalid IP/hostname"
            return
        try:
            exists = False
            for child in list(self.ids.device_list.children):
                if getattr(child, "text", "") == ip:
                    exists = True
                    break
            if not exists:
                from kivy.uix.button import Button
                b = Button(text=ip, size_hint_y=None, height=44)
                b.bind(on_release=lambda inst, ip=ip: self.select_device(ip))
                self.ids.device_list.add_widget(b)
        except Exception:
            pass
        self.select_device(ip)

    def select_device(self, ip):
        self.selected_ip = ip
        self.status_text = f"Selected {ip}"
        app = App.get_running_app()
        app.printer_ip = ip
        try:
            app.camera_src = f"http://{ip}:3031/video"
        except Exception:
            app.camera_src = ""
        threading.Thread(target=self._try_connect_ws_then_fallback, daemon=True).start()

    def connect_printer(self):
        if not self.selected_ip:
            self.status_text = "No IP selected"
            return
        self.status_text = f"Connecting to {self.selected_ip}..."
        threading.Thread(target=self._try_connect_ws_then_fallback, daemon=True).start()

    def toggle_auto_poll(self, enabled: bool):
        self.auto_poll = bool(enabled)
        app = App.get_running_app()
        if self.auto_poll:
            if not app.ws_running:
                if self._poll_event:
                    Clock.unschedule(self._poll_event)
                self._poll_event = Clock.schedule_interval(
                    lambda dt: threading.Thread(target=app._http_fallback_fetch, daemon=True).start(),
                    POLL_INTERVAL
                )
                self._set_status(f"HTTP polling every {POLL_INTERVAL}s")
            else:
                self._set_status("Auto Poll ON (WS active)")
        else:
            if self._poll_event:
                Clock.unschedule(self._poll_event)
                self._poll_event = None
            self._set_status("Auto Poll OFF")

    def _try_connect_ws_then_fallback(self):
        app = App.get_running_app()
        app._stop_ws()
        if not app.printer_ip:
            self._set_status("No IP")
            return

        # NEW: try UDP discovery for MainboardID so fan commands work immediately
        try:
            app._try_udp_discover_mainboard_id(app.printer_ip)
            # reflect it into the screen property too
            if app.mainboard_id:
                Clock.schedule_once(lambda dt: setattr(self, "mainboard_id", str(app.mainboard_id)), 0)
        except Exception:
            pass

        if WS_AVAILABLE:
            try:
                app._start_ws(app.printer_ip)
                for _ in range(6):
                    if app.ws_running:
                        return
                    time.sleep(0.3)
            except Exception:
                log("WS start error: " + traceback.format_exc())
        self._set_status("WebSocket unavailable — using HTTP fallback")
        app._http_fallback_fetch()
        if self.auto_poll:
            if self._poll_event:
                Clock.unschedule(self._poll_event)
            self._poll_event = Clock.schedule_interval(
                lambda dt: threading.Thread(target=app._http_fallback_fetch, daemon=True).start(),
                POLL_INTERVAL
            )

    def disconnect_printer(self):
        app = App.get_running_app()
        app._stop_ws()
        app.mjpeg.stop()
        self.selected_ip = ""
        self.current_temp = "N/A"
        self.status_text = "Disconnected"
        app.printer_ip = None

    def _set_status(self, t):
        self.status_text = t

    # Fan controls — these already call into app._reliable_send()
    def on_fan_toggle(self, fan_key: str, is_down: bool):
        app = App.get_running_app()
        try:
            speed = 0
            if is_down:
                if fan_key == "ModelFan":
                    speed = int(self.ids.model_slider.value)
                elif fan_key == "AuxiliaryFan":
                    speed = int(self.ids.aux_slider.value)
                elif fan_key == "BoxFan":
                    speed = int(self.ids.box_slider.value)
            else:
                speed = 0
            threading.Thread(target=app._reliable_send, args=(fan_key, int(speed)), daemon=True).start()
            self._set_status(f"Sent {fan_key} -> {speed}% (attempt)")
        except Exception:
            log("on_fan_toggle error: " + traceback.format_exc())

    def set_fan_speed(self, fan_key: str, speed: int):
        app = App.get_running_app()
        try:
            speed = max(0, min(100, int(speed)))
            threading.Thread(target=app._reliable_send, args=(fan_key, speed), daemon=True).start()
            self._set_status(f"Set {fan_key} -> {speed}% (attempt)")
        except Exception:
            log("set_fan_speed error: " + traceback.format_exc())

    def open_camera_in_browser(self):
        app = App.get_running_app()
        if not app.printer_ip:
            self._set_status("No host selected")
            return
        url = f"http://{app.printer_ip}:3031/video"
        webbrowser.open(url)
        self._set_status("Opened camera in browser")

class OctoScreen(Screen):
        pass

class SettingsScreen(Screen):
    pass


class CentauriApp(App):
    # App properties used by KV
    nozzle_temp = StringProperty("N/A")
    bed_temp = StringProperty("N/A")
    chamber_temp = StringProperty("N/A")
    nozzle_value = NumericProperty(0)
    bed_value = NumericProperty(0)
    chamber_value = NumericProperty(0)

    print_state = StringProperty("(unknown)")
    print_progress = NumericProperty(0.0)
    print_layer = StringProperty("")
    print_total_layer = StringProperty("")
    coords = StringProperty("-")
    light_state = StringProperty("-")
    status_timestamp = StringProperty("-")

    camera_src = StringProperty("")
    settings_custom_payload = StringProperty("")

    # runtime fields
    printer_ip = None
    mjpeg = None
    ws_app = None
    ws_thread = None
    ws_running = False
    mainboard_id = None

    def build(self):
        Builder.load_file(KV_FILE)
        self.mjpeg = MJPEGStreamer()
        sm = ScreenManager()
        sm.add_widget(HomeScreen(name="home"))
        sm.add_widget(PrintScreen(name="print"))
        sm.add_widget(SettingsScreen(name="settings"))
        sm.add_widget(OctoScreen(name="octo"))
        return sm

    # ---------------- UDP discovery (NEW) ----------------
    def _try_udp_discover_mainboard_id(self, ip: str):
        """
        Best-effort: ask the device on UDP/3000 for its identity (M99999).
        Many SDCP devices reply with JSON containing MainboardID.
        """
        if not ip:
            return None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1.0)
            try:
                # send directly to target IP (works on most LANs)
                s.sendto(UDP_DISCOVERY_MAGIC, (ip, UDP_DISCOVERY_PORT))
            except Exception:
                pass

            # also broadcast once (some firmwares only reply to broadcast)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.sendto(UDP_DISCOVERY_MAGIC, ("255.255.255.255", UDP_DISCOVERY_PORT))
            except Exception:
                pass

            start = time.time()
            while (time.time() - start) < 1.2:
                try:
                    data, addr = s.recvfrom(4096)
                    if not data:
                        continue
                    try:
                        txt = data.decode("utf-8", errors="ignore").strip()
                    except Exception:
                        txt = str(data)

                    log(f"UDP discovery reply from {addr}: {txt[:300]}")
                    try:
                        obj = json.loads(txt)
                    except Exception:
                        # some reply formats may wrap JSON
                        m = re.search(r"(\{.*\})", txt, flags=re.S)
                        obj = json.loads(m.group(1)) if m else None

                    if isinstance(obj, dict):
                        mb = obj.get("MainboardID") or obj.get("mainboard_id") or obj.get("id")
                        if mb:
                            self.mainboard_id = str(mb)
                            log(f"Discovered MainboardID: {self.mainboard_id}")
                            return self.mainboard_id
                except socket.timeout:
                    break
                except Exception:
                    pass
        except Exception:
            log("UDP discovery error: " + traceback.format_exc())
        finally:
            try:
                s.close()
            except Exception:
                pass
        return None

    # ---------------- WS management ----------------
    def _start_ws(self, host: str):
        if not WS_AVAILABLE:
            self._set_status("websocket-client not installed")
            return
        url = f"ws://{host}:{WS_PORT}{WS_PATH}"
        log(f"WS connecting to {url}")

        def on_message(ws, message):
            try:
                obj = None
                try:
                    obj = json.loads(message)
                except Exception:
                    m = re.search(r"(\{.*\})", message, flags=re.S)
                    if m:
                        try:
                            obj = json.loads(m.group(1))
                        except Exception:
                            obj = None
                if obj:
                    mb = obj.get("MainboardID")
                    if mb:
                        self.mainboard_id = str(mb)
                        # reflect to screen prop too
                        try:
                            ps = self.root.get_screen("print")
                            Clock.schedule_once(lambda dt: setattr(ps, "mainboard_id", str(self.mainboard_id)), 0)
                        except Exception:
                            pass
                    all_data = extract_all_from_message(obj)
                    Clock.schedule_once(lambda dt: self._apply_updates(all_data, "WS"), 0)
            except Exception:
                log("ws on_message error: " + traceback.format_exc())

        def on_error(ws, err):
            log("ws error: " + str(err))
            Clock.schedule_once(lambda dt: self._set_status("WebSocket error"), 0)

        def on_close(ws, *args):
            log("ws closed")
            self.ws_running = False
            Clock.schedule_once(lambda dt: self._set_status("WebSocket disconnected"), 0)

        def on_open(ws):
            log("ws open")
            self.ws_running = True
            Clock.schedule_once(lambda dt: self._set_status("Connected (WebSocket)"), 0)

        self.ws_app = websocket.WebSocketApp(
            url,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
            on_open=on_open
        )

        def run_ws():
            try:
                self.ws_app.run_forever()
            except Exception:
                log("ws run_forever exception: " + traceback.format_exc())
            finally:
                self.ws_running = False

        self.ws_thread = threading.Thread(target=run_ws, daemon=True)
        self.ws_thread.start()

    def _stop_ws(self):
        try:
            if getattr(self, "ws_app", None):
                try:
                    self.ws_app.close()
                except Exception:
                    pass
            self.ws_app = None
            self.ws_running = False
        except Exception:
            log("stop_ws error: " + traceback.format_exc())

    # ---------------- HTTP fallback ----------------
    def _http_fallback_fetch(self):
        host = self.printer_ip
        if not host:
            return
        try:
            url = f"http://{host}{MONITOR_PATH}"
            r = requests.get(url, timeout=3.0)
            if r.status_code == 200:
                try:
                    data = r.json()
                    all_data = extract_all_from_message(data if isinstance(data, dict) else {"Status": data})
                    Clock.schedule_once(lambda dt: self._apply_updates(all_data, "HTTP monitor"), 0)
                    Clock.schedule_once(lambda dt: self._set_status("Connected (HTTP monitor)"), 0)
                    return
                except Exception:
                    pass
        except Exception:
            pass

        try:
            url = f"http://{host}{CONTROL_PATH}"
            r = requests.get(url, timeout=3.0)
            if r.status_code == 200:
                html = r.text or ""

                def first_num_after(keyword):
                    m = re.search(rf"{keyword}[^0-9]{{0,60}}([0-9]+(?:\.[0-9]+)?)", html, flags=re.I)
                    return float(m.group(1)) if m else None

                temps = {"nozzle": first_num_after("Nozzle") or first_num_after("Extruder"),
                         "bed": first_num_after("Bed") or first_num_after("Hotbed"),
                         "chamber": first_num_after("Chamber")}
                all_data = {"temps": temps, "fans": {}, "printinfo": {}, "coords": None, "lights": None, "timestamp": None}
                Clock.schedule_once(lambda dt: self._apply_updates(all_data, "HTTP scrape"), 0)
                Clock.schedule_once(lambda dt: self._set_status("Connected (HTTP scrape)"), 0)
                return
        except Exception:
            pass

        Clock.schedule_once(lambda dt: self._set_status("No response"), 0)

    def _apply_updates(self, all_data, source_text=""):
        try:
            temps = all_data.get("temps", {}) or {}
            fans = all_data.get("fans", {}) or {}
            pi = all_data.get("printinfo", {}) or {}
            coords = all_data.get("coords")
            lights = all_data.get("lights")
            timestamp = all_data.get("timestamp")

            if temps.get("nozzle") is not None:
                self.nozzle_temp = f"{temps.get('nozzle'):.1f}"
                try:
                    self.nozzle_value = float(temps.get("nozzle"))
                except Exception:
                    pass
            if temps.get("bed") is not None:
                self.bed_temp = f"{temps.get('bed'):.1f}"
                try:
                    self.bed_value = float(temps.get("bed"))
                except Exception:
                    pass
            if temps.get("chamber") is not None:
                self.chamber_temp = f"{temps.get('chamber'):.1f}"
                try:
                    self.chamber_value = float(temps.get("chamber"))
                except Exception:
                    pass

            # Sync fan sliders/toggles from incoming status
            try:
                ps = self.root.get_screen("print")

                def _upd(dt):
                    try:
                        mv = fans.get("ModelFan")
                        if mv is not None:
                            try:
                                ps.ids.model_slider.value = int(mv)
                            except Exception:
                                pass
                            try:
                                ps.ids.model_toggle.state = "down" if int(mv) > 0 else "normal"
                            except Exception:
                                pass

                        av = fans.get("AuxiliaryFan")
                        if av is not None:
                            try:
                                ps.ids.aux_slider.value = int(av)
                            except Exception:
                                pass
                            try:
                                ps.ids.aux_toggle.state = "down" if int(av) > 0 else "normal"
                            except Exception:
                                pass

                        bv = fans.get("BoxFan")
                        if bv is not None:
                            try:
                                ps.ids.box_slider.value = int(bv)
                            except Exception:
                                pass
                            try:
                                ps.ids.box_toggle.state = "down" if int(bv) > 0 else "normal"
                            except Exception:
                                pass
                    except Exception:
                        log("apply_updates inner error")

                Clock.schedule_once(_upd, 0)
            except Exception:
                pass

            status_code = pi.get("status_code")
            status_text = pi.get("status_text") or (interpret_status_code(status_code) if status_code is not None else "(unknown)")
            prog = pi.get("progress")
            try:
                prog_val = float(prog) if prog is not None else 0.0
            except Exception:
                prog_val = 0.0

            self.print_state = str(status_text) if status_text is not None else "(unknown)"
            self.print_progress = prog_val
            self.print_layer = str(pi.get("current_layer") or "")
            self.print_total_layer = str(pi.get("total_layer") or "")
            self.coords = coords or ""
            self.light_state = json.dumps(lights) if lights else ""
            self.status_timestamp = timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            try:
                ps = self.root.get_screen("print")
                ps.current_temp = f"{source_text}: Nozzle {self.nozzle_temp}°C Bed {self.bed_temp}°C Box {self.chamber_temp}°C"
            except Exception:
                pass

        except Exception:
            log("apply_updates error: " + traceback.format_exc())

    # ---------- Fan control logic (SDCP Cmd 403) ----------
    def _mk_request_id(self) -> str:
        return uuid.uuid4().hex

    def _build_sdcp_fan403(self, fan_key: str, speed: int, topic_style: str = "request"):
        """
        SDCP fan control (Cmd 403) expects:
        Data.Data.TargetFanSpeed = { ModelFan/AuxiliaryFan/BoxFan: 0-100 }

        We build only the specified fan_key, but in the required nested structure.
        """
        if not self.mainboard_id:
            return None

        speed = max(0, min(100, int(speed)))

        payload = {
            "Id": self._mk_request_id(),
            "Data": {
                "Cmd": 403,
                "Data": {
                    "TargetFanSpeed": {
                        fan_key: speed
                    }
                },
                "RequestID": self._mk_request_id(),
                "MainboardID": str(self.mainboard_id),
                "TimeStamp": int(time.time()),
                "From": 0
            },
            "Topic": f"sdcp/{topic_style}/{self.mainboard_id}"
        }
        return payload

    def _reliable_send(self, fan_key: str, speed: int):
        if not self.printer_ip:
            self._set_status("No host selected")
            return

        speed = max(0, min(100, int(speed)))

        # If we still don't have a mainboard id, try once quickly (best-effort)
        if not self.mainboard_id:
            try:
                self._try_udp_discover_mainboard_id(self.printer_ip)
            except Exception:
                pass

        candidates = []

        # Custom user payload (optional)
        custom = (self.settings_custom_payload or "").strip()
        if custom:
            try:
                txt = custom.replace("{fan_key}", fan_key).replace("{speed}", str(speed))
                try:
                    candidates.append(json.loads(txt))
                except Exception:
                    candidates.append(txt)
            except Exception:
                pass

        # SDCP Cmd 403 candidates (correct structure)
        if self.mainboard_id:
            # try both request + command topics, different firmwares vary
            p_req = self._build_sdcp_fan403(fan_key, speed, topic_style="request")
            p_cmd = self._build_sdcp_fan403(fan_key, speed, topic_style="command")
            if p_req:
                candidates.append(p_req)
            if p_cmd:
                candidates.append(p_cmd)

            # Older/looser sdcp style (kept as fallback)
            candidates.append({"Topic": f"sdcp/command/{self.mainboard_id}", "Cmd": "SetFanSpeed", "Data": {fan_key: speed}})

        # Generic fallbacks
        candidates.append({"cmd": "set_fan", "data": {fan_key: speed}})
        candidates.append({"action": "setFanSpeed", "params": {fan_key: speed}})

        sent = False

        # WebSocket send
        if WS_AVAILABLE and getattr(self, "ws_app", None) and self.ws_running:
            for p in candidates:
                try:
                    msg = json.dumps(p) if not isinstance(p, str) else p
                    log(f"WS SEND -> {msg[:900]}")
                    try:
                        self.ws_app.send(msg)
                        time.sleep(0.08)
                        # send twice (your original trick)
                        try:
                            self.ws_app.send(msg)
                        except Exception:
                            pass
                        sent = True
                        break
                    except Exception:
                        log("ws send failed, trying next candidate")
                except Exception:
                    log("candidate prepare error")

        # HTTP fallback
        if not sent:
            for p in candidates:
                try:
                    log(f"HTTP TRY JSON -> {str(p)[:900]}")
                    url1 = f"http://{self.printer_ip}{CONTROL_PATH}"
                    r = requests.post(url1, json=p, timeout=2.0)
                    log(f"POST {url1} -> {r.status_code}")
                    if r.status_code in (200, 204):
                        sent = True
                        break
                except Exception:
                    pass

                try:
                    url2 = f"http://{self.printer_ip}:{WS_PORT}{CONTROL_PATH}"
                    r = requests.post(url2, json=p, timeout=2.0)
                    log(f"POST {url2} -> {r.status_code}")
                    if r.status_code in (200, 204):
                        sent = True
                        break
                except Exception:
                    pass

        if sent:
            self._set_status(f"Command sent: {fan_key} -> {speed}%")
        else:
            self._set_status("Command send failed (capture WS payload and paste into Settings)")

    # ---------- Status helper ----------
    def _set_status(self, text: str):
        try:
            try:
                ps = self.root.get_screen("print")
                ps.status_text = text
            except Exception:
                pass
            log(text)
        except Exception:
            pass

    def open_camera_in_browser(self):
        if not self.printer_ip:
            self._set_status("No host selected")
            return
        url = f"http://{self.printer_ip}:3031/video"
        webbrowser.open(url)
        self._set_status("Opened camera in browser")

    def connect_ip(self, ip: str):
        ip = (ip or "").strip()
        if not ip:
            self._set_status("Empty IP")
            return
        self.printer_ip = ip
        threading.Thread(target=self._try_connect_flow, daemon=True).start()

    def _try_connect_flow(self):
        try:
            self._stop_ws()

            # NEW: discover MainboardID first (best effort)
            try:
                self._try_udp_discover_mainboard_id(self.printer_ip)
                if self.mainboard_id:
                    ps = self.root.get_screen("print")
                    Clock.schedule_once(lambda dt: setattr(ps, "mainboard_id", str(self.mainboard_id)), 0)
            except Exception:
                pass

            if WS_AVAILABLE:
                self._start_ws(self.printer_ip)
                for _ in range(6):
                    if self.ws_running:
                        return
                    time.sleep(0.3)
            self._http_fallback_fetch()
        except Exception:
            log("connect flow error: " + traceback.format_exc())

    def open_url(self, url):
        webbrowser.open(url)


if __name__ == "__main__":
    log("Starting CentauriApp")
    if not WS_AVAILABLE:
        log("websocket-client not available; HTTP fallback only.")
    CentauriApp().run()
