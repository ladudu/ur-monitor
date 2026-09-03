#!/usr/bin/env python3
"""Check vacancy information for a UR rental property page."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime


API_URL = "https://chintai.r6.ur-net.go.jp/chintai/api/bukken/detail/detail_bukken_room/"
UR_ORIGIN = "https://www.ur-net.go.jp"
DEFAULT_URL = "https://www.ur-net.go.jp/chintai/kanto/saitama/50_4090.html"


def property_ids(url: str) -> tuple[str, str, str]:
    match = re.search(r"/(\d{2})_(\d{3})(\d)\.html", urllib.parse.urlparse(url).path)
    if not match:
        raise ValueError("无法从链接识别物件编号（期望类似 50_4090.html）")
    return match.group(1), match.group(2), match.group(3)


def yen(value: str | None) -> int:
    return int(re.sub(r"\D", "", value or "0"))


def fetch_rooms(url: str, timeout: float = 15, attempts: int = 3) -> list[dict]:
    shisya, danchi, shikibetu = property_ids(url)
    form = urllib.parse.urlencode({
        "shisya": shisya,
        "danchi": danchi,
        "shikibetu": shikibetu,
        "orderByField": "0",
        "orderBySort": "0",
        "pageIndex": "0",
        "sp": "",
    }).encode()
    request = urllib.request.Request(
        API_URL,
        data=form,
        headers={
            "User-Agent": "Mozilla/5.0 UR-Vacancy-Checker/1.0",
            "Referer": url,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    last_error: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = json.load(response)
            return [] if raw is None else raw
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(2 ** (attempt - 1))
    assert last_error is not None
    raise last_error


def normalize(room: dict) -> dict:
    detail = urllib.parse.urljoin(UR_ORIGIN, room.get("roomDetailLink", ""))
    systems = [item.get("制度名", "") for item in room.get("system", [])]
    return {
        "id": room.get("id", ""),
        "room": room.get("name", ""),
        "rent_yen": yen(room.get("rent")),
        "common_fee_yen": yen(room.get("commonfee")),
        "layout": room.get("type", ""),
        "area": html.unescape(room.get("floorspace", "")).replace("㎡", "m²"),
        "floor": room.get("floor", ""),
        "building_floors": room.get("floorAll", ""),
        "deposit": room.get("shikikin", ""),
        "key_money": room.get("requirement", ""),
        "systems": systems,
        "detail_url": detail,
    }


def filtered(rooms: list[dict], min_rent: int | None, max_rent: int | None,
             layouts: list[str]) -> list[dict]:
    result = [normalize(room) for room in rooms]
    if min_rent is not None:
        result = [room for room in result if room["rent_yen"] >= min_rent]
    if max_rent is not None:
        result = [room for room in result if room["rent_yen"] <= max_rent]
    if layouts:
        wanted = {item.upper() for item in layouts}
        result = [room for room in result if room["layout"].upper() in wanted]
    return result


def print_table(rooms: list[dict], checked_at: str) -> None:
    print(f"\n检查时间: {checked_at}  符合条件: {len(rooms)} 套")
    if not rooms:
        print("目前没有符合条件的空房。")
        return
    print("-" * 76)
    for room in rooms:
        monthly = room["rent_yen"] + room["common_fee_yen"]
        print(f"{room['room']} | {room['layout']} | {room['area']} | {room['floor']}/{room['building_floors']}")
        print(f"  房租 ¥{room['rent_yen']:,} + 共益费 ¥{room['common_fee_yen']:,} = ¥{monthly:,}/月")
        print(f"  押金 {room['deposit']} / 礼金 {room['key_money']}  {room['detail_url']}")


def check_once(args: argparse.Namespace) -> list[dict]:
    raw = fetch_rooms(args.url, args.timeout)
    return filtered(raw, args.min_rent, args.max_rent, args.layout)


def main() -> int:
    # Windows terminals often default to cp932, which cannot print m² or some
    # Japanese API characters. UTF-8 also keeps redirected JSON valid.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="检查 UR 物件页面的最新空房")
    parser.add_argument("url", nargs="?", default=DEFAULT_URL, help="UR 物件页面链接")
    parser.add_argument("--min-rent", type=int, help="最低房租（日元，不含共益费）")
    parser.add_argument("--max-rent", type=int, help="最高房租（日元，不含共益费）")
    parser.add_argument("--layout", action="append", default=[], help="户型，可重复，如 --layout 1LDK")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--watch", type=int, metavar="SECONDS", help="循环检查间隔；建议不少于 300 秒")
    parser.add_argument("--timeout", type=float, default=15, help="网络超时秒数（默认 15）")
    args = parser.parse_args()

    if args.watch is not None and args.watch < 60:
        parser.error("--watch 为避免给官网造成压力，不能少于 60 秒")

    previous: set[str] = set()
    first = True
    while True:
        checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
        try:
            rooms = check_once(args)
            if args.json:
                print(json.dumps({"checked_at": checked_at, "count": len(rooms), "rooms": rooms}, ensure_ascii=False, indent=2))
            else:
                print_table(rooms, checked_at)
            current = {room["id"] for room in rooms}
            new_ids = current - previous
            if not first and new_ids:
                print(f"\a发现 {len(new_ids)} 套新房源！", flush=True)
            previous, first = current, False
        except (ValueError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"检查失败: {exc}", file=sys.stderr)
            if not args.watch:
                return 1

        if not args.watch:
            return 0
        time.sleep(args.watch)


if __name__ == "__main__":
    raise SystemExit(main())
