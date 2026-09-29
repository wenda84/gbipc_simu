import sys, threading, time
from PySide6.QtCore import QTimer, QObject, Signal
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv)

fired = {"single": False, "signal": False}

class Bridge(QObject):
    sig = Signal(str)
bridge = Bridge()
bridge.sig.connect(lambda m: fired.__setitem__("signal", True))

print("UI thread:", threading.current_thread().name, flush=True)

def worker():
    QTimer.singleShot(0, lambda: fired.__setitem__("single", True))
    bridge.sig.emit("done")

threading.Thread(target=worker, daemon=True).start()
QTimer.singleShot(1500, app.quit)
app.exec()
print("singleShot(from worker thread) fired:", fired["single"])
print("cross-thread Signal fired        :", fired["signal"])
