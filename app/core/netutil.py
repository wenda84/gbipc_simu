"""网络小工具：为「留空的端口」挑选一个当前空闲的本机端口。

用法场景：用户在配置里把「本地 SIP 端口 / 本机媒体端口」留空，程序自行
选一个空闲端口。做法是先用端口 0 让内核分配、读回端口号再关闭——在
Windows/Linux 上均可用，且避免硬编码一段端口区间。

注意：这是「先探测再绑定」，与环境里其它进程争抢该端口之间存在极小
窗口期；绑定失败时调用方仍可回退到 0（由内核再次分配）。
"""
from __future__ import annotations

import socket


def find_free_port(udp: bool = True, host: str = "0.0.0.0") -> int:
    """返回一个当前可用（UDP/TCP 均可）的本机端口号。

    udp=True 时按 UDP 探测（SIP 信令 / 媒体 UDP），否则按 TCP（TCP 媒体）。
    """
    kind = socket.SOCK_DGRAM if udp else socket.SOCK_STREAM
    s = socket.socket(socket.AF_INET, kind)
    try:
        s.bind((host, 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def is_port_free(port: int, udp: bool = True, host: str = "0.0.0.0") -> bool:
    """判断本机某端口当前是否可绑定（用于「配置了固定端口但已被占用」的探测）。"""
    if not port:
        return False
    kind = socket.SOCK_DGRAM if udp else socket.SOCK_STREAM
    s = socket.socket(socket.AF_INET, kind)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()
