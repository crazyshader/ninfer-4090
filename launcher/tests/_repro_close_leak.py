
import os, sys, time, subprocess, json
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"E:\ai\ninfer-4090-native\launcher")

from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])

from ninfer_launcher.core.process import ServerProcess, ServerState

PING = r"C:\Windows\System32\ping.exe"

def os_alive(pid):
    """OS-level truth: is pid running? (0 is success)"""
    if pid is None: return False
    r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, creationflags=0x08000000)
    text = (r.stdout or b"").decode("utf-8", errors="replace")
    return f" {pid} " in f" {text.replace(chr(13),'').replace(chr(10),' ')} " or any(line.split()[1] == str(pid) for line in text.splitlines() if line.strip())

def pump(ms=200):
    end = time.time() + ms/1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)

def report(tag, proc, pid):
    alive_os = os_alive(pid)
    print(f"[{tag}] state={proc.state} qt_alive={proc.is_process_alive()} os_alive={alive_os} pid={pid}")
    return alive_os

def new_proc():
    # fake VRAM reader: None -> degraded path -> 0.5s fallback wait (fast, deterministic)
    return ServerProcess(vram_reader=lambda: None, terminate_grace=1.0, settle_timeout=1.0)

def run_case(name, pre_hook, close_like=True):
    print(f"===== CASE: {name} =====")
    proc = new_proc()
    ok = proc.start(PING, ["-n", "3600", "127.0.0.1"])
    assert ok, "start failed"
    pump(400)
    pid = proc.pid
    print(f"  started: pid={pid} state={proc.state} os_alive={os_alive(pid)}")
    if pre_hook:
        pre_hook(proc)
    pump(100)
    # mimic MainWindow.closeEvent gate
    if close_like and proc.state.active:
        print(f"  closeEvent: state active -> stop_and_wait()")
        t0 = time.time()
        proc.stop_and_wait()
        print(f"  stop_and_wait took {time.time()-t0:.2f}s")
    else:
        print(f"  closeEvent: state NOT active ({proc.state}) -> skip stop (as real closeEvent does)")
    pump(300)
    leaked = report("after close", proc, pid)
    if leaked:
        print(f"  *** LEAK: pid {pid} still alive at OS level after close ***")
        # cleanup
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        print("  OK: process cleaned up")
    return leaked

# Case 1: close while RUNNING
def hook_ready(proc):
    proc.mark_ready()
leak1 = run_case("close while RUNNING (normal user flow)", hook_ready)

# Case 2: close while STARTING (user closes during model load)
leak2 = run_case("close while STARTING (user closes during load)", None)

# Case 3: async stop already begun, then close (user clicked Stop then closed quickly)
def hook_async_stop(proc):
    proc.stop()
leak3 = run_case("close during in-flight async stop", hook_async_stop)

# Case 4: DESYNC probe — state forced STOPPED while process alive (what closeEvent gate would do)
print("===== CASE: state=STOPPED but process alive (desync probe) =====")
proc = new_proc()
proc.start(PING, ["-n", "3600", "127.0.0.1"])
pump(400)
pid = proc.pid
proc._state = ServerState.STOPPED   # simulate desync
print(f"  forced state=STOPPED, pid={pid} os_alive={os_alive(pid)}")
if proc.state.active:
    proc.stop_and_wait()
else:
    print("  closeEvent gate: state NOT active -> stop_and_wait NEVER CALLED")
pump(200)
leaked = report("after close", proc, pid)
if leaked:
    print(f"  *** LEAK: pid {pid} still alive; launcher would exit leaving it behind ***")
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)

print()
print("SUMMARY:", json.dumps({
    "close_running_leaked": leak1,
    "close_starting_leaked": leak2,
    "close_async_stop_leaked": leak3,
}))
