"""Hardware probe CLI: python -m govee_win11.probe [--address XX:XX:XX:XX:XX:XX] <cmd>.

Without ``--address`` it scans for an H6125 like the app does (strongest signal)."""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time

from bleak import BleakClient

from .discovery import choose, normalise_address, scan
from .paths import resource
from .protocol import (WRITE_CHAR, brightness_packet, power_packet,
                       scene_packets, segment_packet)

SCENES_PATH = resource("data", "scenes_H6125.json")
COLORS = [("红", (255, 0, 0)), ("橙", (255, 128, 0)), ("黄", (255, 255, 0)),
          ("绿", (0, 255, 0)), ("青", (0, 255, 255)), ("蓝", (0, 0, 255)),
          ("紫", (160, 0, 255)), ("粉", (255, 105, 180))]
FULL = 0xFFFF


def say(msg: str) -> None:
    print(msg, flush=True)


async def _write(c: BleakClient, pkt: bytes) -> None:
    await c.write_gatt_char(WRITE_CHAR, pkt, response=False)


async def cmd_bits(c: BleakClient, args) -> None:
    await _write(c, power_packet(True))
    await _write(c, segment_packet((20, 20, 20), FULL))
    say("全条设为暗灰")
    await asyncio.sleep(1.0)
    for i in range(16):
        name, rgb = COLORS[i % len(COLORS)]
        await _write(c, segment_packet((20, 20, 20), FULL))
        await _write(c, segment_packet(rgb, 1 << i))
        say(f"bit {i} = {name}")
        await asyncio.sleep(1.5)


async def cmd_brightness(c: BleakClient, args) -> None:
    await _write(c, power_packet(True))
    await _write(c, segment_packet((255, 255, 255), FULL))
    await _write(c, brightness_packet(args.raw))
    say(f"白色，亮度原始值 = {args.raw}")


async def cmd_throughput(c: BleakClient, args) -> None:
    pair = [(255, 0, 0), (0, 0, 255)]
    await _write(c, power_packet(True))
    t0 = time.perf_counter()
    for i in range(200):
        await _write(c, segment_packet(pair[i % 2], FULL))
    dt = time.perf_counter() - t0
    say(f"200 个包用时 {dt:.2f} 秒，约 {200 / dt:.1f} 包/秒")
    for rate in (10, 20, 30, 40):
        say(f"当前档位：{rate} 包/秒（红蓝交替 3 秒）")
        interval = 1.0 / rate
        start = time.perf_counter()
        n = 0
        while time.perf_counter() - start < 3.0:
            await _write(c, segment_packet(pair[n % 2], FULL))
            n += 1
            nxt = start + n * interval
            await asyncio.sleep(max(0.0, nxt - time.perf_counter()))
        say(f"  已发送 {n} 包")
        await asyncio.sleep(1.0)


async def cmd_scene(c: BleakClient, args) -> None:
    data = json.loads(SCENES_PATH.read_text(encoding="utf-8-sig"))
    found = None
    for cat in data["data"]["categories"]:
        for sc in cat["scenes"]:
            if sc["sceneName"] == args.name:
                found = sc
                break
        if found:
            break
    if not found:
        say(f"找不到情景：{args.name}")
        return
    effects = found["lightEffects"]
    if not 0 <= args.variant < len(effects):
        say(f"变体序号超出范围（共 {len(effects)} 个）")
        return
    eff = effects[args.variant]
    param = base64.b64decode(eff.get("scenceParam") or "")
    code = eff["sceneCode"]
    say(f"情景 {args.name} 变体 {args.variant}：sceneCode={code} "
        f"cmdVersion={eff.get('cmdVersion')} 参数 {len(param)} 字节")
    await _write(c, power_packet(True))
    for pkt in scene_packets(code, param):
        await _write(c, pkt)
        await asyncio.sleep(0.03)
    say("已发送")


async def find_address() -> str | None:
    say("正在搜索 H6125 灯带 ...")
    chosen, others = choose(await scan())
    if chosen is None:
        say("没有找到 H6125 灯带，请确认灯带通电、手机 Govee App 已断开")
        return None
    say(f"找到 {chosen.name} ({chosen.address}, {chosen.rssi} dBm)")
    for d in others:
        say(f"  另有 {d.name} ({d.address}, {d.rssi} dBm)，可用 --address 指定")
    return chosen.address


async def run(args) -> None:
    address = args.address or await find_address()
    if address is None:
        sys.exit(1)
    say(f"连接 {address} ...")
    async with BleakClient(address, timeout=20.0) as c:
        say("已连接")
        await args.func(c, args)
    say("已断开")


def _address(value: str) -> str:
    a = normalise_address(value)
    if a is None:
        raise argparse.ArgumentTypeError(f"地址格式应为 XX:XX:XX:XX:XX:XX：{value}")
    return a


def main(argv: list[str] | None = None) -> None:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="govee_win11.probe", description="灯带硬件实测")
    ap.add_argument("--address", default=None, type=_address,
                    help="灯带蓝牙地址（不填则自动搜索 H6125）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("bits", help="依次点亮 bit 0..15").set_defaults(func=cmd_bits)
    p = sub.add_parser("brightness", help="白色并设置亮度原始值")
    p.add_argument("raw", type=int)
    p.set_defaults(func=cmd_brightness)
    sub.add_parser("throughput", help="吞吐测试").set_defaults(func=cmd_throughput)
    p = sub.add_parser("scene", help="发送情景变体")
    p.add_argument("name")
    p.add_argument("variant", type=int)
    p.set_defaults(func=cmd_scene)
    args = ap.parse_args(argv)
    try:
        asyncio.run(run(args))
    except Exception as e:  # noqa: BLE001
        say("连不上灯带：可能被手机占用，请关闭手机蓝牙或 Govee App")
        say(f"错误：{e!r}")
        sys.exit(1)


if __name__ == "__main__":
    main()
