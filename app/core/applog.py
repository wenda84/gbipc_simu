"""崩溃 / 异常本地化：faulthandler + 未捕获异常落盘 + 启动/退出标记。

用途：在用户机器上复现偶发崩溃时拿到确定性线索。
  - 原生崩溃（segfault / 访问越界）由 faulthandler 打印 Python 线程栈到日志；
  - 未捕获 Python 异常由 sys.excepthook 写入同一日志；
  - 每次启动写 `APP START`，干净退出写 `APP QUIT (clean)`；
    若上一次日志末尾是 `APP START` 而无 `APP QUIT`，即可判定上次是崩溃而非正常退出。

日志文件：exe 同级 `logs/app.log`（打包态）或工程根 `logs/app.log`（源码态），
兜底为系统临时目录。

轮转（rotation）与数量限制（backup count）：
  - 单文件达到 MAX_BYTES（默认 1 MB）即触发轮转；
  - 旧文件顺移为 app.log.1 → app.log.2 → …，最多保留 BACKUP_COUNT（默认 5）份；
  - 超过数量限制的最旧备份会被删除。
  可通过模块级常量 MAX_BYTES / BACKUP_COUNT 调整阈值。
"""
from __future__ import annotations

import faulthandler
import os
import sys
import tempfile
import datetime
import traceback

# ---------------- 轮转配置（可按需调整） ----------------
MAX_BYTES = 1_000_000       # 单文件上限（字节），达到即轮转；<=0 关闭轮转
BACKUP_COUNT = 5            # 保留的历史备份份数（不含当前 app.log）；<=0 关闭轮转
# -------------------------------------------------------

_LOG_PATH = None
_WRITER = None


class _RotatingWriter:
    """按大小轮转的文件写入器，作为 faulthandler / excepthook / 打点的统一落盘对象。"""

    def __init__(self, path: str, max_bytes: int, backup_count: int) -> None:
        self._path = path
        self._max = max_bytes
        self._backup = backup_count
        self._f = open(path, "a", encoding="utf-8", errors="replace", buffering=1)

    def _should_rollover(self, text: str) -> bool:
        if self._max <= 0 or self._backup <= 0:
            return False
        try:
            size = os.fstat(self._f.fileno()).st_size
        except OSError:
            size = 0
        return size + len(text.encode("utf-8", "replace")) >= self._max

    def _do_rollover(self) -> None:
        try:
            self._f.close()
        except OSError:
            pass
        # 旧备份顺移：app.log.N -> app.log.N+1（从大到小，避免覆盖）
        for i in range(self._backup - 1, 0, -1):
            src = f"{self._path}.{i}"
            dst = f"{self._path}.{i + 1}"
            if os.path.exists(src):
                if os.path.exists(dst):
                    os.remove(dst)
                os.rename(src, dst)
        # 当前文件 -> app.log.1
        if os.path.exists(self._path):
            os.rename(self._path, f"{self._path}.1")
        self._f = open(self._path, "a", encoding="utf-8", errors="replace", buffering=1)
        # 关键点：faulthandler 在 enable 时会缓存底层 fd；轮转后 fd 已变化，
        # 必须重新 enable 刷新其缓存的 fd，否则崩溃转储仍会写到已关闭的旧文件。
        try:
            faulthandler.enable(file=self, all_threads=True)
        except Exception:
            pass

    def write(self, text: str) -> None:
        try:
            if self._should_rollover(text):
                self._do_rollover()
            self._f.write(text)
        except OSError:
            pass

    def flush(self) -> None:
        try:
            self._f.flush()
        except OSError:
            pass

    def fileno(self) -> int:
        return self._f.fileno()


def install() -> str:
    """安装崩溃捕获。返回日志路径（失败返回空串）。幂等。"""
    global _LOG_PATH, _WRITER
    if _WRITER is not None:
        return _LOG_PATH or ""
    _LOG_PATH = _resolve_path()
    try:
        _WRITER = _RotatingWriter(_LOG_PATH, MAX_BYTES, BACKUP_COUNT)
    except OSError:
        _WRITER = None
        faulthandler.enable(all_threads=True)
        return ""
    # faulthandler 直接写同一个轮转写入器，崩溃转储同样受轮转约束
    faulthandler.enable(file=_WRITER, all_threads=True)
    sys.excepthook = _make_excepthook(sys.excepthook)
    _stamp(f"APP START pid={os.getpid()} "
           f"frozen={bool(getattr(sys, 'frozen', False))}")
    return _LOG_PATH


def _resolve_path() -> str:
    cands = []
    if getattr(sys, "frozen", False):
        cands.append(os.path.join(os.path.dirname(sys.executable), "logs"))
    here = os.path.dirname(os.path.abspath(__file__))  # .../app/core
    cands.append(os.path.join(os.path.dirname(here), "logs"))  # .../app/logs
    cands.append(tempfile.gettempdir())
    for c in cands:
        try:
            os.makedirs(c, exist_ok=True)
            p = os.path.join(c, "app.log")
            with open(p, "a", encoding="utf-8"):
                pass
            return p
        except OSError:
            continue
    return os.path.join(tempfile.gettempdir(), "app.log")


def _stamp(msg: str) -> None:
    if _WRITER is None:
        return
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        _WRITER.write(f"[{ts}] {msg}\n")
        _WRITER.flush()
    except Exception:
        pass


def log_event(msg: str) -> None:
    """业务层打点（如「托盘双击恢复」），用于把崩溃定位到具体动作。"""
    _stamp(msg)


def mark_quit() -> None:
    """干净退出标记，连到 QApplication.aboutToQuit。"""
    _stamp(f"APP QUIT (clean) pid={os.getpid()}")


def _make_excepthook(orig):
    def _hook(etype, exc, tb):
        _stamp("UNCAUGHT EXCEPTION")
        if _WRITER is not None:
            try:
                traceback.print_exception(etype, exc, tb, file=_WRITER)
            except Exception:
                pass
            _WRITER.flush()
        orig(etype, exc, tb)
    return _hook
