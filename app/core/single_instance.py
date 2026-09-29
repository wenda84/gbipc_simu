"""按 exe 绝对路径加锁的单实例控制。

行为约定：
  - 不同目录的 exe（如 ``c:/ipc.exe`` 与 ``d:/ipc.exe``）各自可运行一个实例；
  - 同一 exe 再次启动时，不新开窗口，而是把已存在实例的窗口恢复到前台，
    随后本进程自行退出。

实现基于 ``QLocalServer``/``QLocalSocket``：server 名由 exe 绝对路径哈希派生，
因此「路径不同 => 锁名不同 => 允许两个实例」，天然满足上述需求。若上一个实例
异常崩溃，其命名管道可能残留，``listen`` 前先 ``removeServer`` 清理，保证可再次启动。
"""
from __future__ import annotations

import hashlib
import os
import sys

from PySide6.QtNetwork import QLocalServer, QLocalSocket

SERVER_PREFIX = "GB28181_IPC_SINGLE_"
_SHOW = b"SHOW\n"


def server_key() -> str:
    """由 exe 绝对路径派生的稳定标识：不同路径 => 不同的实例锁。"""
    exe = os.path.abspath(sys.executable)
    digest = hashlib.md5(exe.encode("utf-8", "ignore")).hexdigest()[:16]
    return SERVER_PREFIX + digest


class SingleInstance:
    def __init__(self) -> None:
        self._name = server_key()
        self._server: QLocalServer | None = None
        self._on_show = None
        self._conns: list = []  # 保持连接对象存活，避免被 GC 提前回收

    def is_another_running(self) -> bool:
        """探测是否已有同路径的实例在运行（能连上说明已在监听）。"""
        sock = QLocalSocket()
        sock.connectToServer(self._name)
        ok = sock.waitForConnected(400)
        sock.abort()
        return ok

    def notify_foreground(self) -> None:
        """向已存在的实例发送『恢复到前台』请求（自身随后退出）。"""
        sock = QLocalSocket()
        sock.connectToServer(self._name)
        if sock.waitForConnected(1000):
            sock.write(_SHOW)
            sock.waitForBytesWritten(1000)
        sock.disconnectFromServer()

    def listen(self, on_show) -> bool:
        """本实例成为唯一实例：启动 server，收到唤起请求时回调 ``on_show``。

        成功返回 True；若监听失败（极端情况）返回 False，调用方仍继续运行。
        """
        self._on_show = on_show
        # 清理上一次崩溃残留的命名管道，否则同名 pipe 已存在会导致 listen 失败。
        QLocalServer.removeServer(self._name)
        server = QLocalServer()
        server.newConnection.connect(self._on_new_connection)
        if not server.listen(self._name):
            try:
                server.deleteLater()
            finally:
                self._server = None
            return False
        self._server = server
        return True

    def _on_new_connection(self) -> None:
        conn = self._server.nextPendingConnection()
        if conn is None:
            return
        # 保持引用，等客户端断开后再释放，避免被 GC
        self._conns.append(conn)
        # 连接本身即代表「唤起窗口」请求（内容仅作握手），无需解析
        if callable(self._on_show):
            try:
                self._on_show()
            except Exception:
                pass
