# bricks/kiosk_modal/__init__.py
import platform
import subprocess
import tempfile
import time
import urllib.request

from arduino.app_utils import brick, Logger

logger = Logger("KioskLauncher")


def wait_for_server(url: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def build_kiosk_command(url: str) -> list:
    system = platform.system()

    if system == "Darwin":
        chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        profile_dir = tempfile.mkdtemp(prefix="kiosk-profile-")
        return [chrome_path, "--kiosk", f"--app={url}", f"--user-data-dir={profile_dir}"]

    if system == "Linux":
        return ["chromium-browser", "--kiosk", "--noerrdialogs", "--disable-infobars", f"--app={url}"]

    if system == "Windows":
        return ["msedge", "--kiosk", url, "--edge-kiosk-type=fullscreen"]

    raise RuntimeError(f"Unsupported platform: {system}")


def launch_kiosk(url: str, ready_timeout: float = 15.0):
    if not wait_for_server(url, timeout=ready_timeout):
        logger.error(f"Server never became reachable at {url}")
        return None
    cmd = build_kiosk_command(url)
    logger.info(f"Launching: {' '.join(cmd)}")
    return subprocess.Popen(cmd)


@brick
class KioskLauncher:
    def __init__(self, url: str, poll_timeout: float = 15.0):
        self._url = url
        self._poll_timeout = poll_timeout
        self._proc = None

    def start(self):
        pass

    def execute(self):
        self._proc = launch_kiosk(self._url, ready_timeout=self._poll_timeout)
        if self._proc:
            self._proc.wait()

    def stop(self):
        if self._proc:
            self._proc.terminate()