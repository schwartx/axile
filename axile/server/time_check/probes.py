"""有界 NTP 采样，直接与选定参考源比对时间。"""

import asyncio
import socket
import struct
import time
from collections.abc import Callable
from typing import Any

from axile.executor.algorithms.utils.clock import Clock, get_default_clock

_NTP_EPOCH = 2208988800
_NTP_ERA = 2**32


def _stamp(value: float) -> bytes:
    """编码 NTP 64 位时间戳，支持 2036 年后的 era 翻转。"""
    value += _NTP_EPOCH
    return struct.pack("!II", int(value) % _NTP_ERA, int((value % 1) * _NTP_ERA))


def _seconds(data: bytes, reference: float) -> float:
    """用本机年代展开 NTP era；支持合理年代内的大幅时间偏差。"""
    seconds, fraction = struct.unpack("!II", data)
    era = round((reference + _NTP_EPOCH - seconds) / _NTP_ERA)
    return seconds + era * _NTP_ERA + fraction / _NTP_ERA - _NTP_EPOCH


def decode_sample(packet: bytes, sent: bytes, t1: float, t4: float, elapsed: float) -> dict[str, float]:
    """验证响应关联与同步状态后，用四时间戳估算偏差。"""
    if len(packet) < 48:
        raise ValueError("NTP 响应长度不足")
    if packet[0] & 7 != 4 or (packet[0] >> 3) & 7 not in (3, 4):
        raise ValueError("NTP 响应模式或版本无效")
    if packet[1] == 0:
        raise PermissionError(f"NTP 服务器拒绝请求：{packet[12:16].decode('ascii', errors='replace')}")
    if packet[0] >> 6 == 3 or not 1 <= packet[1] <= 15:
        raise ValueError("NTP 服务器尚未同步")
    if packet[24:32] != sent or packet[32:40] == bytes(8) or packet[40:48] == bytes(8):
        raise ValueError("NTP 响应时间戳无效或不属于本次请求")
    if abs((t4 - t1) - elapsed) > 0.05:
        raise ValueError("测量期间系统时间发生跳变")
    t2 = _seconds(packet[32:40], t1)
    t3 = _seconds(packet[40:48], t1)
    delay = elapsed - (t3 - t2)
    if t3 < t2 or delay < -0.001:
        raise ValueError("NTP 响应时序无效")
    # 正数表示本机领先参考源，与界面“快 / 慢”保持一致。
    return {"offset_ms": ((t1 - t2) + (t4 - t3)) * 500, "delay_ms": max(0, delay * 1000)}


async def _sample(
    address: tuple[Any, ...], family: int, clock: Clock, monotonic: Callable[[], float]
) -> dict[str, float]:
    """连接 UDP socket 将应答限制到目标端点。"""
    loop = asyncio.get_running_loop()
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.setblocking(False)
        await loop.sock_connect(sock, address)
        t1 = clock.time()
        sent = _stamp(t1)
        packet = bytes([0x23]) + bytes(39) + sent
        start = monotonic()
        await loop.sock_sendall(sock, packet)
        response = await loop.sock_recv(sock, 512)
        elapsed = monotonic() - start
        return decode_sample(response, sent, t1, clock.time(), elapsed)


async def ntp_probe(
    host: str, port: int, clock: Clock | None = None, monotonic: Callable[[], float] = time.monotonic
) -> dict[str, Any]:
    """包括 DNS 在内最多八秒；采样失败不切换服务器。"""
    clock = clock or get_default_clock()
    samples: list[dict[str, float]] = []
    errors: list[str] = []
    try:
        async with asyncio.timeout(8):
            loop = asyncio.get_running_loop()
            addresses = await loop.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
            unique = list(dict.fromkeys((family, address) for family, _, _, _, address in addresses))
            if not unique:
                raise ValueError("时间服务器未解析到可用地址")
            for index in range(3):
                family, address = unique[index % len(unique)]
                try:
                    async with asyncio.timeout(2):
                        samples.append(await _sample(address, family, clock, monotonic))
                except PermissionError:
                    raise
                except (OSError, TimeoutError, ValueError) as exc:
                    errors.append(str(exc) or "NTP 请求超时")
                if index < 2:
                    await asyncio.sleep(1)
    except (OSError, TimeoutError, ValueError, PermissionError) as exc:
        errors.append(str(exc) or "NTP 检查超时")
    if not samples or any("跳变" in error or "拒绝" in error for error in errors):
        return {"status": "error", "error": "; ".join(errors) or "没有有效 NTP 样本"}
    best = min(samples, key=lambda sample: sample["delay_ms"])
    return {"status": "ok", **best, "samples": len(samples), "sample_errors": errors}

