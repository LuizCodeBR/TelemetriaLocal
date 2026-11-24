"""Prepara um ambiente local na primeira execução e encaminha o comando."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys


def main():
    if sys.version_info[:2] not in {(3, 12), (3, 13)}:
        print("Use Python 3.12 ou 3.13 para executar esta versão.", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parent
    cache = root / ".cache"
    temporary = cache / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    env = os.environ | {
        "TMPDIR": str(temporary), "TEMP": str(temporary), "TMP": str(temporary),
        "PIP_CACHE_DIR": str(cache / "pip"), "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
    }
    venv = root / ".venv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        subprocess.run([sys.executable, "-B", "-m", "venv", str(venv)], check=True, env=env)
    requirements = root / "requirements.txt"
    stamp = venv / "telemetria-deps.sha256"
    digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
    if not stamp.is_file() or stamp.read_text(encoding="ascii").strip() != digest:
        subprocess.run([str(python), "-B", "-m", "pip", "install", "-r", str(requirements)], check=True, env=env)
        stamp.write_text(digest, encoding="ascii")
    args = sys.argv[1:]
    command = ["-m", "unittest", "discover", "-s", "tests", "-v"] if args == ["testar"] else ["-m", "telemetria", *args]
    return subprocess.call([str(python), "-B", *command], env=env, cwd=root)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)
    except KeyboardInterrupt:
        sys.exit(130)
