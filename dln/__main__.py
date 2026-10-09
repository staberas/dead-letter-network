"""Supervise two isolated HTTP listeners with one shared SQLite store."""
import signal
import subprocess
import sys
import time


def main():
    processes = []
    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for app, port in (("public", "8000"), ("admin", "8001")):
            processes.append(subprocess.Popen([sys.executable, "-m", "uvicorn", f"dln.server:{app}",
                                              "--host", "0.0.0.0", "--port", port,
                                              "--no-proxy-headers", "--no-access-log"]))
        while all(p.poll() is None for p in processes):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for p in processes:
            if p.poll() is None:
                p.terminate()
        for p in processes:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
    return next((p.returncode for p in processes if p.returncode and p.returncode > 0), 0)


if __name__ == "__main__":
    sys.exit(main())
