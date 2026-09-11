"""Start a private X11 desktop and normally sandboxed installed Chrome."""
import json
import os
from pathlib import Path
import signal
import subprocess
import time

width = int(os.environ.get('ORE_DESKTOP_WIDTH', '1280'))
height = int(os.environ.get('ORE_DESKTOP_HEIGHT', '800'))
if not 640 <= width <= 3840 or not 480 <= height <= 2160:
    raise SystemExit('Unsupported desktop dimensions')
profile = Path('/state/profile')
profile.mkdir(mode=0o700, exist_ok=True)
Path('/state/downloads').mkdir(mode=0o700, exist_ok=True)
for directory in ('/tmp/ore-cache','/tmp/ore-config','/tmp/ore-runtime','/tmp/ore-data'):
    Path(directory).mkdir(mode=0o700,exist_ok=True)
children = []

def stop(*_):
    for child in reversed(children):
        if child.poll() is None:
            child.terminate()
    for child in reversed(children):
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
children.append(subprocess.Popen(['Xvfb', ':99', '-screen', '0', f'{width}x{height}x24', '-nolisten', 'tcp', '-ac']))
for _ in range(100):
    if Path('/tmp/.X11-unix/X99').exists():
        break
    if children[0].poll() is not None:
        raise SystemExit('Xvfb failed')
    time.sleep(.05)
children.append(subprocess.Popen(['openbox', '--sm-disable']))
# No debugger endpoint, automation/stealth switches, or sandbox-disabling flags.
children.append(subprocess.Popen(['google-chrome-stable', '--user-data-dir=/state/profile',
    '--no-first-run', '--no-default-browser-check', '--start-maximized', 'about:blank']))
while True:
    if any(child.poll() is not None for child in children):
        stop()
    time.sleep(.2)
