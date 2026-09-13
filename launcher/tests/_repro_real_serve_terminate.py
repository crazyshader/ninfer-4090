
import os, sys, time, subprocess
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"E:\ai\ninfer-4090-native\launcher")

from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])
from PySide6.QtCore import QProcess

SERVE = r"E:\ai\ninfer-4090-native\build-ninja\apps\ninfer-serve.exe"
MODEL = r"E:\ai\ninfer-4090-native\launcher\models\qwen3_8_27b_8-19.ninfer"

def os_alive(pid):
    if not pid: return False
    r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, creationflags=0x08000000)
    text = (r.stdout or b"").decode("utf-8", errors="replace")
    return any(line.split() and line.split()[0].endswith(".exe") and line.split()[1] == str(pid) for line in text.splitlines() if line.strip())

def pump(ms=100):
    end = time.time() + ms/1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)

# Start the REAL server binary (spare port 18099, small context to reduce footprint)
proc = QProcess()
proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
args = [MODEL, "--port", "18099", "--max-context", "4096", "--kv-dtype", "int8"]
started = proc.start(SERVE, args)
print("start() returned:", started)
pump(300)
pid = int(proc.processId())
print(f"pid={pid} qt_state={proc.state()} os_alive={os_alive(pid)}")

# Let CUDA init / early load run ~2.5s (real GPU work), then terminate exactly like the launcher's close path
time.sleep(2.5)
pump(100)
print(f"before terminate: qt_state={proc.state()} os_alive={os_alive(pid)}")
proc.terminate()
t0 = time.time()
ok = proc.waitForFinished(5000)
dt = time.time() - t0
print(f"terminate() issued; waitForFinished(5000) -> {ok} after {dt:.2f}s; qt_state={proc.state()}")
pump(300)
alive_after = os_alive(pid)
print(f"OS truth after terminate+wait: alive={alive_after}")
if alive_after:
    print(">>> terminate() FAILED to kill the real CUDA process! Fallback to taskkill /T /F:")
    tk = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=0x08000000)
    print("taskkill rc:", tk.returncode, (tk.stdout or b"").decode("utf-8","replace").strip()[:200])
    time.sleep(1.0)
    pump(100)
    alive_after = os_alive(pid)
    print(f"OS truth after taskkill: alive={alive_after}")
print("FINAL:", "KILLED" if not alive_after else "STILL ALIVE (leak reproduced)")
