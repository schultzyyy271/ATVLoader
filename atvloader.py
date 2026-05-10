#!/usr/bin/env python3
"""
ATVLoader v3.0 - Windows Apple TV Sideloader
- Scan: zeroconf (in-process)
- Sign: zsign (subprocess)  
- Pair: pymobiledevice3 Python API + GUI PIN dialog (in-process)
- Tunnel: pymobiledevice3 CLI (subprocess, proven reliable)
- Install: pymobiledevice3 Python API via RSD (in-process)
"""

import asyncio
import builtins
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import customtkinter as ctk
from tkinter import filedialog, messagebox

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("ATVLoader")

# ---------------------------------------------------------------------------
# Async event loop
# ---------------------------------------------------------------------------
_bg_loop = None
_bg_thread = None

def _ensure_bg_loop():
    global _bg_loop, _bg_thread
    if _bg_loop is not None and _bg_loop.is_running():
        return
    _bg_loop = asyncio.new_event_loop()
    _bg_thread = threading.Thread(target=_bg_loop.run_forever, daemon=True)
    _bg_thread.start()

def run_async(coro, timeout=300):
    _ensure_bg_loop()
    return asyncio.run_coroutine_threadsafe(coro, _bg_loop).result(timeout=timeout)

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
@dataclass
class AppleTVDevice:
    name: str
    address: str
    port: int
    identifier: str = ""
    paired: bool = False

    def display(self):
        s = "✓ Paired" if self.paired else "○ Not Paired"
        return f"{self.name}  ({self.address})  [{s}]"

@dataclass
class AppState:
    ipa_path: Optional[str] = None
    signed_ipa_path: Optional[str] = None
    zsign_path: Optional[str] = None
    output_dir: str = ""
    selected_device: Optional[AppleTVDevice] = None
    discovered_devices: list = field(default_factory=list)
    service_provider: Optional[object] = None
    tunnel_proc: Optional[subprocess.Popen] = None
    rsd_address: Optional[str] = None
    rsd_port: Optional[int] = None

    def __post_init__(self):
        self.output_dir = str(Path.home() / "ATVLoader")
        os.makedirs(self.output_dir, exist_ok=True)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_app_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def find_zsign():
    app_dir = get_app_dir()
    for c in [os.path.join(app_dir, "zsign.exe"), os.path.join(app_dir, "zsign"),
              os.path.join(app_dir, "bin", "zsign.exe"), os.path.join(os.getcwd(), "zsign.exe"),
              shutil.which("zsign")]:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    return None

def get_python():
    """Get Python executable path. When frozen, find system Python."""
    if not getattr(sys, 'frozen', False):
        return sys.executable
    for c in [shutil.which("python"), shutil.which("python3")]:
        if c and os.path.isfile(c):
            return c
    return "python"

# ---------------------------------------------------------------------------
# Backend: zsign
# ---------------------------------------------------------------------------
def sign_ipa(ipa_path, p12_path, p12_password, provision_path, output_path, zsign_path,
             bundle_id=None, progress_callback=None):
    for label, path in [("zsign", zsign_path), ("IPA", ipa_path), ("Certificate", p12_path), ("Profile", provision_path)]:
        if not os.path.isfile(path):
            return False, f"{label} not found: {path}"
    cmd = [zsign_path, "-k", p12_path, "-p", p12_password, "-m", provision_path, "-z", "9", "-o", output_path]
    if bundle_id: cmd.extend(["-b", bundle_id])
    cmd.append(ipa_path)
    if progress_callback: progress_callback("Signing IPA with zsign...")
    try:
        kwargs = {"capture_output": True, "text": True, "timeout": 300}
        if platform.system() == "Windows": kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(cmd, **kwargs)
        if result.returncode == 0 and os.path.isfile(output_path):
            return True, f"Signed ({os.path.getsize(output_path)/1048576:.1f} MB)"
        return False, f"zsign failed: {result.stderr.strip() or result.stdout.strip()}"
    except Exception as e:
        return False, f"zsign error: {e}"

# ---------------------------------------------------------------------------
# Backend: Discovery (zeroconf)
# ---------------------------------------------------------------------------
def discover_apple_tvs(timeout=8.0, progress_callback=None):
    devices = []
    if progress_callback: progress_callback("Scanning network for Apple TVs...")
    try:
        from zeroconf import Zeroconf, ServiceBrowser
        import socket
        found = []
        class L:
            def add_service(self, zc, stype, name):
                try:
                    info = zc.get_service_info(stype, name)
                    if info: found.append(info)
                except Exception: pass
            def remove_service(self, *a): pass
            def update_service(self, *a): pass

        for stype in ["_remotepairing-manual-pairing._tcp.local.", "_remotepairing._tcp.local."]:
            zc = Zeroconf()
            ServiceBrowser(zc, stype, L())
            time.sleep(timeout)
            zc.close()
            if found: break

        seen = set()
        for info in found:
            addr = info.parsed_addresses()[0] if info.parsed_addresses() else "unknown"
            name = info.name.split("._")[0]
            ident = info.properties.get(b'identifier', b'').decode() if info.properties else ""
            key = f"{addr}:{info.port}"
            if key in seen: continue
            seen.add(key)
            devices.append(AppleTVDevice(name=name, address=addr, port=info.port, identifier=ident))
            log.info(f"Found: {name} at {addr}:{info.port}")
    except Exception as e:
        log.error(f"Discovery failed: {e}")
        if progress_callback: progress_callback(f"Discovery error: {e}")
    if progress_callback: progress_callback(f"Found {len(devices)} device(s)")
    return devices

# ---------------------------------------------------------------------------
# Backend: Pair (pymobiledevice3 Python API — in-process with GUI PIN)
# ---------------------------------------------------------------------------
async def _pair_device(ip, port, identifier, pin_callback=None, progress_callback=None):
    from pymobiledevice3.remote.tunnel_service import create_core_device_service_using_remotepairing_manual_pairing
    from pymobiledevice3.exceptions import RemotePairingCompletedError

    # Re-scan for fresh port
    if progress_callback: progress_callback("Getting fresh connection...")
    try:
        fresh = discover_apple_tvs(timeout=5.0)
        for d in fresh:
            if d.address == ip:
                port = d.port
                identifier = d.identifier or identifier
                if progress_callback: progress_callback(f"Fresh port: {ip}:{port}")
                break
    except: pass

    # Monkey-patch input() for GUI PIN dialog
    original_input = builtins.input
    def gui_input(prompt=""):
        log.info(f"pymobiledevice3 asks: {prompt}")
        if pin_callback:
            pin = pin_callback()
            if pin: return pin
            raise Exception("Pairing cancelled")
        return original_input(prompt)
    builtins.input = gui_input

    try:
        if progress_callback:
            progress_callback(f"Connecting to {ip}:{port}...")
            progress_callback("Apple TV will show a PIN — enter it when prompted")

        try:
            await create_core_device_service_using_remotepairing_manual_pairing(
                identifier, ip, port, autopair=True
            )
            if progress_callback: progress_callback("Paired successfully!")
            return True, "Paired!"
        except RemotePairingCompletedError:
            if progress_callback: progress_callback("Already paired!")
            return True, "Already paired!"
    except Exception as e:
        log.error(f"Pairing failed: {e}", exc_info=True)
        return False, f"Pairing failed: {e}"
    finally:
        builtins.input = original_input

def pair_device(device, pin_callback=None, progress_callback=None):
    try:
        return run_async(_pair_device(device.address, device.port, device.identifier,
                                      pin_callback=pin_callback, progress_callback=progress_callback), timeout=120)
    except Exception as e:
        return False, f"Pairing error: {e}"

# ---------------------------------------------------------------------------
# Backend: Tunnel (pymobiledevice3 CLI — subprocess, proven reliable)
# ---------------------------------------------------------------------------
def start_tunnel(udid=None, progress_callback=None):
    """
    Start tunnel via pymobiledevice3 CLI with WiFi transport.
    Returns (success, message, process, rsd_address, rsd_port).
    The process must stay alive during install.
    """
    python = get_python()
    cmd = [python, "-m", "pymobiledevice3", "remote", "start-tunnel", "-t", "wifi", "--script-mode"]

    # Pass UDID to auto-select device and skip the interactive prompt
    if udid:
        cmd.extend(["--udid", udid])

    if progress_callback:
        progress_callback("Starting tunnel...")

    try:
        # Set PYTHONIOENCODING to avoid Unicode crashes in inquirer3
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )

        rsd_address = None
        rsd_port = None
        start_time = time.time()

        while time.time() - start_time < 60:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
                continue

            # Strip ANSI escape codes for clean parsing
            clean = re.sub(r'\x1b\[[0-9;]*m', '', line).strip()
            if not clean:
                continue

            log.info(f"[tunnel] {clean}")
            if progress_callback:
                progress_callback(f"[tunnel] {clean}")

            # Parse output — script mode: "ADDRESS PORT" on one line
            if "RSD Address:" in clean:
                rsd_address = clean.split("RSD Address:")[1].strip()
            elif "RSD Port:" in clean:
                try:
                    rsd_port = int(clean.split("RSD Port:")[1].strip())
                except ValueError:
                    pass
            elif "ERROR" in clean or "error" in clean.lower() or "Traceback" in clean:
                continue
            elif "tunnel created" in clean.lower():
                # Next line should be the address/port
                continue
            else:
                # Script mode: "ADDRESS PORT" e.g. "fd1b:eca2:c11b::1 64412"
                parts = clean.split()
                if len(parts) == 2:
                    try:
                        rsd_port = int(parts[1])
                        rsd_address = parts[0]
                    except ValueError:
                        pass

            if rsd_address and rsd_port:
                if progress_callback:
                    progress_callback(f"Tunnel ready: [{rsd_address}]:{rsd_port}")
                return True, f"Tunnel active", proc, rsd_address, rsd_port

        if proc.poll() is not None:
            remaining = proc.stdout.read()
            return False, f"Tunnel exited: {remaining[:200]}", None, None, None
        else:
            proc.terminate()
            return False, "Tunnel timed out (60s) — run as admin", None, None, None

    except Exception as e:
        return False, f"Tunnel error: {e}", None, None, None

# ---------------------------------------------------------------------------
# Backend: Install (pymobiledevice3 CLI — proven reliable)
# ---------------------------------------------------------------------------
def install_ipa(ipa_path, rsd_address, rsd_port, progress_callback=None):
    if not os.path.isfile(ipa_path):
        return False, f"IPA not found: {ipa_path}"

    python = get_python()
    cmd = [python, "-m", "pymobiledevice3", "apps", "install", ipa_path, "--rsd", rsd_address, str(rsd_port)]

    if progress_callback:
        progress_callback("Installing IPA to Apple TV...")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        # Read output in real-time for progress updates
        while True:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
                continue

            line = line.strip()
            if not line:
                continue

            log.info(f"[install] {line}")

            # Parse progress percentage
            if "Complete" in line:
                if progress_callback:
                    progress_callback(line.split("INFO")[-1].strip() if "INFO" in line else line)
            elif "Installation succeed" in line:
                if progress_callback:
                    progress_callback("Installation complete!")
                return True, "App installed!"
            elif "ERROR" in line:
                if progress_callback:
                    progress_callback(f"Error: {line}")

        # Check exit code
        if proc.returncode == 0:
            return True, "App installed!"
        else:
            remaining = proc.stdout.read()
            return False, f"Install failed (exit {proc.returncode}): {remaining}"

    except Exception as e:
        log.error(f"Install failed: {e}", exc_info=True)
        return False, f"Install failed: {e}"

# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
C = {
    "bg": "#0a0a0f", "card": "#12121a", "card_h": "#1a1a25", "input": "#1e1e2a",
    "border": "#2a2a3a", "text": "#e0e0e8", "dim": "#7a7a8a", "muted": "#4a4a5a",
    "accent": "#6c5ce7", "accent_h": "#7c6cf7", "ok": "#00d68f", "warn": "#f0a500", "err": "#ff4757",
}

class PinDialog(ctk.CTkToplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("Enter Apple TV PIN")
        self.geometry("400x220")
        self.configure(fg_color=C["bg"])
        self.resizable(False, False)
        self.grab_set()
        self.pin_value = None
        ctk.CTkLabel(self, text="Enter the PIN shown on your Apple TV",
                     font=("Segoe UI", 14, "bold"), text_color=C["text"]).pack(pady=(24, 4))
        ctk.CTkLabel(self, text="Check your TV screen for a 6-digit code",
                     font=("Segoe UI", 11), text_color=C["dim"]).pack(pady=(0, 16))
        self.entry = ctk.CTkEntry(self, font=("JetBrains Mono", 28), fg_color=C["input"],
                                  border_color=C["accent"], text_color=C["text"],
                                  height=50, width=200, justify="center", placeholder_text="000000")
        self.entry.pack(pady=(0, 16))
        self.entry.focus()
        self.entry.bind("<Return>", lambda e: self._ok())
        ctk.CTkButton(self, text="Pair", height=40, width=200, font=("Segoe UI", 14, "bold"),
                      fg_color=C["accent"], hover_color=C["accent_h"], text_color="white",
                      command=self._ok).pack()
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - 400) // 2
        y = parent.winfo_y() + (parent.winfo_height() - 220) // 2
        self.geometry(f"+{x}+{y}")

    def _ok(self):
        self.pin_value = self.entry.get().strip()
        self.destroy()

    def get_pin(self):
        self.wait_window()
        return self.pin_value

class LogPanel(ctk.CTkFrame):
    def __init__(self, parent, **kw):
        super().__init__(parent, fg_color=C["card"], corner_radius=12, **kw)
        ctk.CTkLabel(self, text="LOG", font=("JetBrains Mono", 11, "bold"),
                     text_color=C["dim"], anchor="w").pack(padx=16, pady=(12, 4), fill="x")
        self.tb = ctk.CTkTextbox(self, font=("JetBrains Mono", 11), fg_color=C["bg"],
                                 text_color=C["dim"], corner_radius=8, height=140, wrap="word", state="disabled")
        self.tb.pack(padx=12, pady=(0, 12), fill="both", expand=True)

    def append(self, text, level="info"):
        self.tb.configure(state="normal")
        p = {"info": "›", "success": "✓", "warning": "⚠", "error": "✗"}.get(level, "›")
        self.tb.insert("end", f"  {time.strftime('%H:%M:%S')}  {p}  {text}\n")
        self.tb.see("end")
        self.tb.configure(state="disabled")

class FilePicker(ctk.CTkFrame):
    def __init__(self, parent, label, ftypes, on_select=None, **kw):
        super().__init__(parent, fg_color="transparent", **kw)
        self.ftypes, self.on_select, self.filepath = ftypes, on_select, None
        ctk.CTkLabel(self, text=label, font=("Segoe UI", 12, "bold"),
                     text_color=C["text"], anchor="w", width=140).pack(side="left", padx=(0, 8))
        self.pv = ctk.StringVar(value="No file selected")
        self.pl = ctk.CTkLabel(self, textvariable=self.pv, font=("JetBrains Mono", 11),
                               text_color=C["dim"], anchor="w")
        self.pl.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(self, text="Browse", width=80, height=32, font=("Segoe UI", 12),
                      fg_color=C["input"], hover_color=C["card_h"], border_width=1,
                      border_color=C["border"], text_color=C["text"], command=self._browse).pack(side="right")

    def _browse(self):
        p = filedialog.askopenfilename(filetypes=self.ftypes)
        if p:
            self.filepath = p; self.pv.set(Path(p).name)
            self.pl.configure(text_color=C["text"])
            if self.on_select: self.on_select(p)

    def get_path(self): return self.filepath

class StepCard(ctk.CTkFrame):
    def __init__(self, parent, num, title, **kw):
        super().__init__(parent, fg_color=C["card"], corner_radius=12, border_width=1, border_color=C["border"], **kw)
        h = ctk.CTkFrame(self, fg_color="transparent"); h.pack(fill="x", padx=16, pady=(16, 8))
        self.badge = ctk.CTkLabel(h, text=str(num), width=28, height=28, font=("Segoe UI", 12, "bold"),
                                  fg_color=C["accent"], corner_radius=14, text_color="white")
        self.badge.pack(side="left", padx=(0, 12))
        ctk.CTkLabel(h, text=title, font=("Segoe UI", 15, "bold"), text_color=C["text"],
                     anchor="w").pack(side="left", fill="x", expand=True)
        self.status = ctk.CTkLabel(h, text="", font=("Segoe UI", 11), text_color=C["dim"])
        self.status.pack(side="right")
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.pack(fill="x", padx=16, pady=(0, 16))

    def set_status(self, t, c=None): self.status.configure(text=t, text_color=c or C["dim"])
    def mark_ok(self): self.badge.configure(fg_color=C["ok"], text="✓"); self.configure(border_color=C["ok"])
    def mark_err(self): self.badge.configure(fg_color=C["err"]); self.configure(border_color=C["err"])
    def reset(self): self.badge.configure(fg_color=C["accent"]); self.configure(border_color=C["border"]); self.set_status("")

# ---------------------------------------------------------------------------
# Main App
# ---------------------------------------------------------------------------
class ATVLoaderApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.app_state = AppState()
        self.app_state.zsign_path = find_zsign()
        self.title("ATVLoader")
        self.geometry("780x900")
        self.minsize(680, 700)
        self.configure(fg_color=C["bg"])
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        _ensure_bg_loop()
        self._build()
        self._check()

    def _build(self):
        hdr = ctk.CTkFrame(self, fg_color="transparent", height=64)
        hdr.pack(fill="x", padx=24, pady=(20, 4))
        ctk.CTkLabel(hdr, text="ATVLoader", font=("Segoe UI", 28, "bold"), text_color=C["text"]).pack(side="left")
        ctk.CTkLabel(hdr, text="  Windows → Apple TV  •  No Xcode  •  No BS",
                     font=("Segoe UI", 12), text_color=C["dim"]).pack(side="left", padx=(8,0), pady=(8,0))
        ctk.CTkLabel(hdr, text="v1.0", font=("JetBrains Mono", 11), text_color=C["muted"]).pack(side="right")

        self.scroll = ctk.CTkScrollableFrame(self, fg_color="transparent",
                                             scrollbar_button_color=C["border"], scrollbar_button_hover_color=C["dim"])
        self.scroll.pack(fill="both", expand=True, padx=24, pady=(8, 8))
        self._s1(); self._s2(); self._s3(); self._s4()
        self.logp = LogPanel(self, height=160)
        self.logp.pack(fill="x", padx=24, pady=(4, 16))

    # Step 1 — IPA
    def _s1(self):
        self.step1 = StepCard(self.scroll, 1, "Select IPA"); self.step1.pack(fill="x", pady=(0,8))
        self.ipa_fp = FilePicker(self.step1.content, "IPA File", [("IPA", "*.ipa"), ("All", "*.*")], on_select=self._ipa_sel)
        self.ipa_fp.pack(fill="x", pady=(4,0))

    def _ipa_sel(self, p):
        self.app_state.ipa_path = p
        mb = os.path.getsize(p)/1048576
        self.step1.set_status(f"{mb:.1f} MB"); self.step1.mark_ok()
        self.logp.append(f"IPA: {Path(p).name} ({mb:.1f} MB)")
        try:
            import plistlib
            with zipfile.ZipFile(p) as zf:
                for n in zf.namelist():
                    if n.endswith("Info.plist") and n.count("/")==2:
                        info = plistlib.load(zf.open(n))
                        bid = info.get("CFBundleIdentifier","?")
                        bn = info.get("CFBundleDisplayName", info.get("CFBundleName","?"))
                        self.step1.set_status(f"{bn} ({bid})", C["text"])
                        self.logp.append(f"  Bundle: {bid}")
                        break
        except: pass

    # Step 2 — Sign
    def _s2(self):
        self.step2 = StepCard(self.scroll, 2, "Signing Certificate"); self.step2.pack(fill="x", pady=(0,8))
        self.p12_fp = FilePicker(self.step2.content, "Certificate (.p12)", [("P12","*.p12"),("PFX","*.pfx"),("All","*.*")])
        self.p12_fp.pack(fill="x", pady=(4,6))
        r = ctk.CTkFrame(self.step2.content, fg_color="transparent"); r.pack(fill="x", pady=(0,6))
        ctk.CTkLabel(r, text="Password", font=("Segoe UI",12,"bold"), text_color=C["text"], width=140).pack(side="left", padx=(0,8))
        self.pw = ctk.CTkEntry(r, show="•", font=("JetBrains Mono",12), fg_color=C["input"],
                               border_color=C["border"], text_color=C["text"], height=32, placeholder_text="leave blank if none")
        self.pw.pack(side="left", fill="x", expand=True, padx=(0,6))
        self._pw_visible = False
        self.pw_toggle = ctk.CTkButton(r, text="Show", width=60, height=32, font=("Segoe UI",11),
                                       fg_color=C["input"], hover_color=C["card_h"], border_width=1,
                                       border_color=C["border"], text_color=C["dim"], command=self._toggle_pw)
        self.pw_toggle.pack(side="right")
        self.prov_fp = FilePicker(self.step2.content, "Provisioning Profile", [("Prov","*.mobileprovision"),("All","*.*")])
        self.prov_fp.pack(fill="x", pady=(0,6))
        r2 = ctk.CTkFrame(self.step2.content, fg_color="transparent"); r2.pack(fill="x", pady=(0,6))
        ctk.CTkLabel(r2, text="Bundle ID (optional)", font=("Segoe UI",12,"bold"), text_color=C["text"], width=140).pack(side="left", padx=(0,8))
        self.bid = ctk.CTkEntry(r2, font=("JetBrains Mono",12), fg_color=C["input"], border_color=C["border"],
                                text_color=C["text"], height=32, placeholder_text="com.example.app (leave blank to keep)")
        self.bid.pack(side="left", fill="x", expand=True)
        self.sign_btn = ctk.CTkButton(self.step2.content, text="Sign IPA", height=40, font=("Segoe UI",14,"bold"),
                                      fg_color=C["accent"], hover_color=C["accent_h"], text_color="white",
                                      corner_radius=8, command=self._sign)
        self.sign_btn.pack(fill="x", pady=(8,0))

    # Step 3 — Connect Apple TV (Scan → Select → Pair → Tunnel)
    def _s3(self):
        self.step3 = StepCard(self.scroll, 3, "Connect Apple TV"); self.step3.pack(fill="x", pady=(0,8))
        ctk.CTkLabel(self.step3.content,
                     text="Apple TV must be on same network. First-time: Settings → Remotes and Devices → Remote App and Devices",
                     font=("Segoe UI",11), text_color=C["dim"], justify="left", anchor="w").pack(fill="x", pady=(4,8))

        # Device selector row
        sel_row = ctk.CTkFrame(self.step3.content, fg_color="transparent"); sel_row.pack(fill="x", pady=(0,8))
        ctk.CTkLabel(sel_row, text="Device", font=("Segoe UI",12,"bold"), text_color=C["text"],
                     width=60).pack(side="left", padx=(0,8))
        self.dev_dropdown = ctk.CTkOptionMenu(
            sel_row, values=["No devices — click Scan"],
            font=("JetBrains Mono", 11), fg_color=C["input"], button_color=C["accent"],
            button_hover_color=C["accent_h"], dropdown_fg_color=C["card"],
            dropdown_hover_color=C["card_h"], text_color=C["text"],
            dropdown_text_color=C["text"], corner_radius=8, height=34,
            command=self._on_device_selected,
        )
        self.dev_dropdown.pack(side="left", fill="x", expand=True)

        # Buttons row
        br = ctk.CTkFrame(self.step3.content, fg_color="transparent"); br.pack(fill="x", pady=(0,4))
        bkw = dict(height=36, font=("Segoe UI",12,"bold"), fg_color=C["input"], hover_color=C["card_h"],
                   border_width=1, border_color=C["border"], text_color=C["text"])
        self.scan_b = ctk.CTkButton(br, text="1. Scan", width=130, command=self._scan, **bkw); self.scan_b.pack(side="left", padx=(0,6))
        self.pair_b = ctk.CTkButton(br, text="2. Pair", width=130, command=self._pair, **bkw); self.pair_b.pack(side="left", padx=(0,6))
        self.tun_b = ctk.CTkButton(br, text="3. Start Tunnel", width=160, command=self._tunnel, **bkw); self.tun_b.pack(side="left")

    # Step 4 — Install
    def _s4(self):
        self.step4 = StepCard(self.scroll, 4, "Install to Apple TV"); self.step4.pack(fill="x", pady=(0,8))
        self.inst_btn = ctk.CTkButton(self.step4.content, text="Install App", height=48, font=("Segoe UI",16,"bold"),
                                      fg_color=C["accent"], hover_color=C["accent_h"], text_color="white",
                                      corner_radius=8, command=self._install)
        self.inst_btn.pack(fill="x", pady=(8,4))
        self.prog = ctk.CTkProgressBar(self.step4.content, fg_color=C["bg"], progress_color=C["accent"], height=6, corner_radius=3)
        self.prog.pack(fill="x", pady=(4,0)); self.prog.set(0)

    def _check(self):
        s = self.app_state
        if not s.zsign_path: self.logp.append("zsign not found — place zsign.exe in app folder", "warning")
        else: self.logp.append(f"zsign: {s.zsign_path}")
        try: import pymobiledevice3; self.logp.append("pymobiledevice3 OK")
        except: self.logp.append("pymobiledevice3 missing", "error")
        try: from zeroconf import Zeroconf; self.logp.append("zeroconf OK")
        except: self.logp.append("zeroconf missing", "warning")
        self.logp.append(f"Platform: {platform.system()}")
        self.logp.append("Run as admin for tunnel step. Everything runs locally.")

    def _t(self, fn, *a): threading.Thread(target=fn, args=a, daemon=True).start()
    def _l(self, m, lv="info"): self.after(0, lambda: self.logp.append(m, lv))

    def _toggle_pw(self):
        self._pw_visible = not self._pw_visible
        if self._pw_visible:
            self.pw.configure(show="")
            self.pw_toggle.configure(text="Hide")
        else:
            self.pw.configure(show="•")
            self.pw_toggle.configure(text="Show")

    # ── Sign ──
    def _sign(self):
        s = self.app_state
        ipa, p12, pw, prov = s.ipa_path, self.p12_fp.get_path(), self.pw.get(), self.prov_fp.get_path()
        bid = self.bid.get().strip() or None
        if not ipa: return messagebox.showwarning("Missing", "Select an IPA first")
        if not p12: return messagebox.showwarning("Missing", "Select a .p12 certificate")
        if not prov: return messagebox.showwarning("Missing", "Select a provisioning profile")
        if not s.zsign_path: return messagebox.showerror("Missing", "zsign.exe not found")
        out = os.path.join(s.output_dir, f"signed_{Path(ipa).stem}.ipa")
        self.sign_btn.configure(state="disabled", text="Signing..."); self.step2.reset(); self.prog.set(0.2)
        def do():
            self._l("Signing..."); self.after(0, lambda: self.prog.set(0.4))
            ok, msg = sign_ipa(ipa, p12, pw, prov, out, s.zsign_path, bundle_id=bid, progress_callback=self._l)
            self.after(0, lambda: self.prog.set(1.0 if ok else 0))
            if ok:
                s.signed_ipa_path = out; self._l(f"Signed: {out}", "success")
                self.after(0, lambda: (self.step2.mark_ok(), self.step2.set_status("Signed ✓", C["ok"])))
            else:
                self._l(f"Failed: {msg}", "error"); self.after(0, self.step2.mark_err)
            self.after(0, lambda: self.sign_btn.configure(state="normal", text="Sign IPA"))
        self._t(do)

    # ── Device selected from dropdown ──
    def _on_device_selected(self, choice):
        for d in self.app_state.discovered_devices:
            label = f"{d.name}  ({d.address})"
            if label == choice:
                self.app_state.selected_device = d
                self._l(f"Selected: {d.name} at {d.address}:{d.port}")
                break

    # ── Scan ──
    def _scan(self):
        self.scan_b.configure(state="disabled", text="Scanning..."); self.step3.reset()
        def do():
            self._l("Scanning...")
            devs = discover_apple_tvs(timeout=8.0, progress_callback=self._l)
            self.app_state.discovered_devices = devs
            def up():
                if devs:
                    labels = [f"{d.name}  ({d.address})" for d in devs]
                    self.dev_dropdown.configure(values=labels)
                    self.dev_dropdown.set(labels[0])
                    self.app_state.selected_device = devs[0]
                    self.step3.set_status(f"{len(devs)} found", C["ok"])
                else:
                    self.dev_dropdown.configure(values=["No devices found"])
                    self.dev_dropdown.set("No devices found")
                    self.app_state.selected_device = None
                    self.step3.set_status("No devices", C["warn"])
                self.scan_b.configure(state="normal", text="1. Scan")
            self.after(0, up)
        self._t(do)

    # ── Pair ──
    def _pair(self):
        dev = self.app_state.selected_device
        if not dev: return messagebox.showwarning("No device", "Scan for devices first")
        self.pair_b.configure(state="disabled", text="Pairing...")

        pin_result = [None]; pin_event = threading.Event()
        def ask_pin():
            def show():
                d = PinDialog(self); pin_result[0] = d.get_pin(); pin_event.set()
            self.after(0, show); pin_event.wait(120)
            return pin_result[0]

        def do():
            self._l(f"Pairing with {dev.name}...")
            ok, msg = pair_device(dev, pin_callback=ask_pin, progress_callback=self._l)
            if ok:
                self._l(msg, "success"); dev.paired = True
                self.after(0, lambda: self.pair_b.configure(state="normal", text="Paired ✓", fg_color=C["ok"]))
            else:
                self._l(msg, "error")
                self.after(0, lambda: self.pair_b.configure(state="normal", text="2. Pair"))
        self._t(do)

    # ── Tunnel ──
    def _tunnel(self):
        self.tun_b.configure(state="disabled", text="Starting...")
        dev = self.app_state.selected_device
        def do():
            self._l("Starting tunnel...")
            # Pass device identifier to auto-select (avoids interactive prompt)
            udid = dev.identifier if dev else None
            ok, msg, proc, addr, port = start_tunnel(udid=udid, progress_callback=self._l)
            if ok:
                self.app_state.tunnel_proc = proc
                self.app_state.rsd_address = addr
                self.app_state.rsd_port = port
                self._l(msg, "success")
                self.after(0, lambda: (self.step3.mark_ok(),
                                       self.tun_b.configure(state="normal", text="Tunnel Active ✓", fg_color=C["ok"])))
            else:
                self._l(msg, "error")
                self.after(0, lambda: self.tun_b.configure(state="normal", text="3. Start Tunnel"))
        self._t(do)

    # ── Install ──
    def _install(self):
        s = self.app_state
        if not s.signed_ipa_path or not os.path.isfile(s.signed_ipa_path):
            return messagebox.showwarning("Missing", "Sign an IPA first (Step 2)")
        if not s.rsd_address or not s.rsd_port:
            return messagebox.showwarning("No tunnel", "Start a tunnel first (Step 3)")

        self.inst_btn.configure(state="disabled", text="Installing..."); self.step4.reset(); self.prog.set(0.1)
        def do():
            self._l("Installing to Apple TV..."); self.after(0, lambda: self.prog.set(0.3))
            ok, msg = install_ipa(s.signed_ipa_path, s.rsd_address, s.rsd_port, progress_callback=self._l)
            self.after(0, lambda: self.prog.set(1.0 if ok else 0))
            if ok:
                self._l(msg, "success"); self._l("Check your Apple TV!", "success")
                self.after(0, lambda: (self.step4.mark_ok(), self.step4.set_status("Installed ✓", C["ok"])))
            else:
                self._l(msg, "error"); self.after(0, self.step4.mark_err)
            self.after(0, lambda: self.inst_btn.configure(state="normal", text="Install App"))
        self._t(do)

    def destroy(self):
        # Kill tunnel process on exit
        if self.app_state.tunnel_proc:
            try: self.app_state.tunnel_proc.terminate()
            except: pass
        if _bg_loop and _bg_loop.is_running():
            _bg_loop.call_soon_threadsafe(_bg_loop.stop)
        super().destroy()

if __name__ == "__main__":
    ATVLoaderApp().mainloop()
