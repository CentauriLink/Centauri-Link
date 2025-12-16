# main.py
from kivy.app import App
from kivy.uix.screenmanager import ScreenManager, Screen
from kivy.properties import StringProperty, ListProperty
from kivy.clock import Clock
from kivy.uix.button import Button

import socket, time, ipaddress
from threading import Thread
from concurrent.futures import ThreadPoolExecutor

# Try zeroconf (mDNS/DNS-SD) if available
try:
    from zeroconf import Zeroconf, ServiceBrowser, ServiceListener
    ZEROCONF_AVAILABLE = True
except Exception:
    ZEROCONF_AVAILABLE = False

# ports to probe (JetDirect 9100, IPP 631, OctoPrint common 5000, HTTP 80/8080)
COMMON_PRINTER_PORTS = [9100, 631, 5000, 80, 8080]


def get_local_ip():
    """Return the primary local IPv4 used to reach the internet (or 127.0.0.1)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def try_connect(ip, port, timeout=0.5):
    """Quick TCP connect test."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


# ---------- Zeroconf helper ----------
class _SimpleZcListener(ServiceListener):
    def __init__(self):
        self.found = []  # list of (ip, desc)

    def add_service(self, zeroconf, type_, name):
        try:
            info = zeroconf.get_service_info(type_, name, timeout=1000)
            if info:
                addr = None
                if info.addresses:
                    addr = socket.inet_ntoa(info.addresses[0])
                elif info.server:
                    try:
                        addr = socket.gethostbyname(info.server.rstrip('.'))
                    except Exception:
                        addr = None
                desc = f"{name} ({type_})"
                if addr:
                    self.found.append((addr, desc))
        except Exception:
            pass

    def update_service(self, *args): pass
    def remove_service(self, *args): pass


def zeroconf_discover(service_types=None, wait=2.0):
    if not ZEROCONF_AVAILABLE:
        return []
    if not service_types:
        service_types = ['_ipp._tcp.local.', '_http._tcp.local.', '_printer._tcp.local.']
    zc = Zeroconf()
    listener = _SimpleZcListener()
    browsers = [ServiceBrowser(zc, s, listener) for s in service_types]
    time.sleep(wait)
    try:
        zc.close()
    except Exception:
        pass
    return listener.found


# ---------- SSDP (UPnP) helper ----------
def ssdp_search(timeout=2.0):
    """
    Send M-SEARCH SSDP and collect responses.
    Returns list of (ip, info)
    """
    message = '\r\n'.join([
        'M-SEARCH * HTTP/1.1',
        'HOST: 239.255.255.250:1900',
        'MAN: "ssdp:discover"',
        'MX: 1',
        'ST: ssdp:all', '', ''
    ]).encode('utf-8')

    results = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(timeout)
    try:
        sock.sendto(message, ('239.255.255.250', 1900))
        start = time.time()
        while True:
            if time.time() - start > timeout:
                break
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                break
            text = data.decode(errors='ignore')
            loc = None
            for line in text.splitlines():
                if line.lower().startswith('location:'):
                    loc = line.split(':', 1)[1].strip()
                if line.lower().startswith('usn:') and not loc:
                    loc = line.split(':', 1)[1].strip()
            results.add((addr[0], loc or 'ssdp'))
    finally:
        sock.close()
    return list(results)


# ---------- Fast subnet port scan ----------
def subnet_port_scan(timeout=0.35, ports=None, workers=80):
    ports = ports or COMMON_PRINTER_PORTS
    local_ip = get_local_ip()
    try:
        net = ipaddress.ip_network(local_ip + '/24', strict=False)
    except Exception:
        return []
    ips = [str(ip) for ip in net.hosts()]
    try:
        ips.remove(local_ip)
    except ValueError:
        pass

    found = []

    def scan_host(ip):
        for p in ports:
            if try_connect(ip, p, timeout=timeout):
                return (ip, f"open:{p}")
        return (None, None)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(scan_host, ip) for ip in ips]
        for f in futures:
            ip, desc = f.result()
            if ip:
                found.append((ip, desc))
    return found


# ---------- Combined discovery ----------
def discover_printers(timeout_mdns=2.0, timeout_ssdp=2.0, scan_timeout=0.35):
    results = {}
    # 1) Zeroconf (mDNS/DNS-SD)
    try:
        zres = zeroconf_discover(wait=timeout_mdns)
        for ip, desc in zres:
            results[ip] = desc
    except Exception:
        pass

    # 2) SSDP
    try:
        sres = ssdp_search(timeout=timeout_ssdp)
        for ip, desc in sres:
            if ip not in results:
                results[ip] = desc
    except Exception:
        pass

    # 3) Fast port scan fallback
    try:
        pres = subnet_port_scan(timeout=scan_timeout)
        for ip, desc in pres:
            if ip not in results:
                results[ip] = desc
    except Exception:
        pass

    return [(ip, results[ip]) for ip in results]


# ---------- Kivy screens ----------
class HomeScreen(Screen):
    pass


class PrintScreen(Screen):
    status_text = StringProperty("Ready")
    devices = ListProperty([])         # list of (ip,desc)
    selected_ip = StringProperty("")

    def on_kv_post(self, base_widget):
        self.status_text = f"Local IP: {get_local_ip()}"

    def search_printer(self):
        self.status_text = "Starting discovery..."
        self.devices = []
        self.selected_ip = ""
        # clear UI list immediately
        Clock.schedule_once(lambda dt: self._clear_list(), 0)
        Thread(target=self._scan_background, daemon=True).start()

    def _clear_list(self):
        if "device_list" in self.ids:
            self.ids.device_list.clear_widgets()

    def _scan_background(self):
        # Run combined discovery
        Clock.schedule_once(lambda dt: self._set_status("Discovering (mDNS/SSDP/port scan)..."), 0)
        found = discover_printers()
        if not found:
            Clock.schedule_once(lambda dt: self._set_status("No devices found."), 0)
        else:
            Clock.schedule_once(lambda dt: self._set_status(f"Found {len(found)} device(s)"), 0)
        # update device list in UI thread
        Clock.schedule_once(lambda dt, d=found: self._update_devices(d), 0)

    def _update_devices(self, devices):
        self.devices = devices
        self.ids.device_list.clear_widgets()
        if not devices:
            b = Button(text="No devices found", size_hint_y=None, height=40, background_color=(0,0,0,0.15))
            self.ids.device_list.add_widget(b)
            return
        for ip, desc in devices:
            b = Button(text=f"{ip}  •  {desc}", size_hint_y=None, height=44)
            b.bind(on_release=lambda inst, ip=ip: self.select_device(ip))
            self.ids.device_list.add_widget(b)

    def select_device(self, ip):
        self.selected_ip = ip
        self.status_text = f"Selected {ip}"

    def connect_printer(self):
        if not self.selected_ip:
            self.status_text = "No device selected"
            return
        self.status_text = f"Connecting to {self.selected_ip}..."
        Thread(target=self._connect_task, daemon=True).start()

    def _connect_task(self):
        # try typical ports for a quick connection check
        for p in COMMON_PRINTER_PORTS:
            if try_connect(self.selected_ip, p, timeout=1.0):
                Clock.schedule_once(lambda dt, ip=self.selected_ip, port=p: self._set_status(f"Connected to {ip}:{port} ✅"), 0)
                return
        Clock.schedule_once(lambda dt: self._set_status(f"Failed to connect to {self.selected_ip}"), 0)

    def _set_status(self, t):
        self.status_text = t


class SettingsScreen(Screen):
    pass


class CentauriLinkApp(App):
    def build(self):
        sm = ScreenManager()
        sm.add_widget(HomeScreen(name="home"))
        sm.add_widget(PrintScreen(name="print"))
        sm.add_widget(SettingsScreen(name="settings"))
        return sm


if __name__ == "__main__":
    CentauriLinkApp().run()

