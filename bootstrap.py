"""Create a project-local environment on first source launch."""
from pathlib import Path
import os
import subprocess
import sys
import venv


def main():
    if sys.version_info < (3, 11):
        raise SystemExit("Please install Python 3.11+ from https://www.python.org/downloads/")
    root = Path(__file__).resolve().parent
    env = root / ".venv"
    python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(env)
    requirements = root / "requirements.txt"
    marker = env / ".recorder-requirements"
    wanted = requirements.read_text()
    if not marker.exists() or marker.read_text() != wanted:
        subprocess.run([str(python), "-m", "pip", "install", "-r", str(requirements)], check=True)
        marker.write_text(wanted)
    subprocess.run([str(python), "-c", "import tkinter; from network_capture import configure_macos_tk; configure_macos_tk()"], cwd=root, check=True)
    return subprocess.call([str(python), str(root / "network_capture.py")], cwd=root)


if __name__ == "__main__":
    raise SystemExit(main())
