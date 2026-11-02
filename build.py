"""Build an app using only this repository and PyPI dependencies."""
import platform
import subprocess
import sys

subprocess.run([
    sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
    '--windowed', '--name', 'BrowserNetworkRecorder',
    '--collect-all', 'selenium', '--hidden-import', 'tkinter',
    'network_capture.py',
], check=True)
print('Build finished for', platform.system(), platform.machine())
