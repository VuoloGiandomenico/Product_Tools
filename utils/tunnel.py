import subprocess
import re
import threading
import time
import os
import sys
import shutil
import socket

# Global reference to the tunnel process and URL
_tunnel_process = None
_tunnel_url = None
_tunnel_thread = None
_tunnel_error = None

def get_tunnel_url():
    """Returns the active tunnel URL, or None if not started."""
    global _tunnel_url
    return _tunnel_url

def is_tunnel_running():
    """Return True only when the local tunnel process is still alive."""
    global _tunnel_process
    if _tunnel_process is None:
        return False
    try:
        return _tunnel_process.poll() is None
    except Exception:
        return False


def set_tunnel_url(url):
    """Sets the active tunnel URL manually (e.g. from sniffer headers).

    This must only be trusted when the local tunnel process is actually still running.
    If the tunnel process has died or never started, a stale trycloudflare host must
    not be treated as a healthy tunnel URL.
    """
    global _tunnel_url
    if not url:
        _tunnel_url = None
        return
    if not is_tunnel_running():
        _tunnel_url = None
        return
    _tunnel_url = url


def get_tunnel_error():
    """Returns any startup or runtime errors occurred, or None."""
    global _tunnel_error
    return _tunnel_error


def _local_service_is_reachable(port):
    """True when the app target (e.g. localhost:8501) is listening."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except Exception:
        return False


def _local_http_is_reachable(port):
    """True when the local origin responds over HTTP."""
    try:
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{port}/", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status in (200, 301, 302, 403, 404)
    except Exception:
        return False


def start_tunnel(port=8501):
    """Starts the Cloudflare tunnel in a background thread if it isn't already running."""
    global _tunnel_process, _tunnel_url, _tunnel_thread, _tunnel_error

    if _tunnel_process is not None and is_tunnel_running():
        # Tunnel is already running or attempting to run
        return _tunnel_url

    if not _local_service_is_reachable(port):
        _tunnel_error = f"The local app is not listening on http://localhost:{port}. Start the app before creating the tunnel."
        return None

    def run():
        global _tunnel_process, _tunnel_url, _tunnel_error

        cloudflared_binary = shutil.which("cloudflared")

        # Pre-flight check to detect if corporate firewall (e.g. Zscaler) is intercepting SSL
        import urllib.request
        try:
            req = urllib.request.Request("https://www.cloudflare.com", headers={'User-Agent': 'Mozilla/5.0'})
            urllib.request.urlopen(req, timeout=3)
        except Exception as e:
            error_str = str(e).lower()
            if "certificate_verify_failed" in error_str:
                if not cloudflared_binary:
                    _tunnel_error = "Corporate network (e.g. Zscaler) detected: SSL interception prevents Python from downloading cloudflared. Install cloudflared manually (e.g. 'brew install cloudflared') or turn off the VPN."
                    return
            elif "no such host" in error_str or "timeout" in error_str:
                _tunnel_error = "Network error: could not reach the internet or DNS is blocked. Check your connection or turn off the VPN."
                return

        # Prefer the locally installed Cloudflare binary.  pycloudflared downloads
        # this binary at runtime, which fails on corporate networks that intercept
        # SSL certificates.  The Python module remains a fallback for environments
        # where the executable has not been installed.
        if cloudflared_binary:
            cmd = [cloudflared_binary, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{port}"]
        else:
            cmd = [sys.executable, "-m", "pycloudflared", "tunnel", "--no-autoupdate", "--url", f"http://localhost:{port}"]

        try:
            _tunnel_error = None
            _tunnel_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            
            url_pattern = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
            
            for line in _tunnel_process.stdout:
                lower_line = line.lower()
                if "429" in line or "1015" in line or "Too Many Requests" in line:
                    _tunnel_error = "Rate limit reached (429 Too Many Requests). Please wait a few minutes or change your IP (VPN)."
                elif "failed to unmarshal" in line:
                    _tunnel_error = "Rate limit reached. Try again in a few minutes or connect via VPN."
                elif "x509: certificate signed by unknown authority" in line or "handshake failure" in line or "certificate verify failed" in lower_line:
                    _tunnel_error = "SSL interception detected: a VPN or corporate proxy is altering the TLS certificate. Turn off the VPN or switch networks."
                elif "failed to connect to the local service" in lower_line or "connection refused" in lower_line:
                    _tunnel_error = f"The local app is not listening on http://localhost:{port}. Make sure Streamlit is running and the port is correct."
                elif "no such host" in lower_line or "i/o timeout" in lower_line or "dial udp" in lower_line or "lookup" in lower_line:
                    _tunnel_error = "Cloudflare could not resolve or reach the service. Check your internet connection and try again."

                match = url_pattern.search(line)
                if match:
                    _tunnel_url = match.group(0)
                    _tunnel_error = None
                    if not _local_http_is_reachable(port):
                        _tunnel_error = f"The tunnel was created, but the local app on http://localhost:{port} is not responding. Start Streamlit and check that the port is open."
                        _tunnel_url = None
                        print(f"[TUNNEL] Generated URL found, but localhost:{port} is not responding.")
                    else:
                        print(f"[TUNNEL] Cloudflare Tunnel successfully started: {_tunnel_url}")

            _tunnel_process.wait()
            if _tunnel_process.returncode != 0 and not _tunnel_error:
                _tunnel_error = f"Tunnel exited with code {_tunnel_process.returncode}."
        except Exception as e:
            _tunnel_error = str(e)
            print(f"[TUNNEL] Error running Cloudflare Tunnel: {e}")
        finally:
            if _tunnel_process is not None and _tunnel_process.poll() is not None:
                _tunnel_url = None
                _tunnel_process = None

    _tunnel_thread = threading.Thread(target=run, daemon=True)
    _tunnel_thread.start()

    for _ in range(20):
        if _tunnel_url or _tunnel_error:
            break
        time.sleep(0.5)

    return _tunnel_url


def stop_tunnel():
    """Stops the active Cloudflare tunnel if running."""
    global _tunnel_process, _tunnel_url, _tunnel_error
    if _tunnel_process:
        try:
            _tunnel_process.terminate()
            _tunnel_process.wait(timeout=2)
        except Exception:
            try:
                _tunnel_process.kill()
            except Exception:
                pass
        _tunnel_process = None
        _tunnel_url = None
        _tunnel_error = None
        print("[TUNNEL] Tunnel stopped.")
