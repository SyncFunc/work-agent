"""内建 MCP Server：天气查询。

通过 stdin/stdout 的 newline-delimited JSON-RPC 与 MCP 客户端通信，暴露工具：
- ``get_weather(city)``：查询某城市的当前天气。

数据源：
- 首选 **wttr.in**（免费、无需 key，``https://wttr.in/<city>?format=j1&lang=zh`` 返回 JSON，含中文天气描述）。
- 网络不可用时返回离线提示（标注 [离线数据]）。

城市支持：**不设白名单，任意城市名都直接查 wttr.in**（中文 / 拼音 / 英文原样传参，wttr.in 支持中文城市名）。
常见城市的拼音/英文会翻译成中文显示名（如 haerbin → 哈尔滨），未收录的拼音按原输入显示。

作为**内建 MCP**：McpManager 默认加载本 server（除非用户在 mcp.yaml 里覆盖同名 weather）。

用法（直接跑，作为子进程）：
    python -m agent.mcp.weather_server
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from typing import Any

PROTOCOL_VERSION = "2025-06-18"

# 常用城市 拼音/英文 → 中文名 翻译表（仅用于把拼音/英文输入显示成中文，**不是白名单**）。
# wttr.in 不返回中文城市名（lang=zh 下 areaName 仍是英文/拼音），所以常见城市做一次翻译；
# 未收录的拼音/英文直接查 wttr.in 并按原输入显示，绝不因此拒绝查询。
_PINYIN_ZH: dict[str, str] = {
    "beijing": "北京",
    "shanghai": "上海",
    "shenzhen": "深圳",
    "guangzhou": "广州",
    "hangzhou": "杭州",
    "shaoxing": "绍兴",
    "ningbo": "宁波",
    "suzhou": "苏州",
    "wuxi": "无锡",
    "chengdu": "成都",
    "wuhan": "武汉",
    "nanjing": "南京",
    "tianjin": "天津",
    "chongqing": "重庆",
    "xian": "西安",
    "qingdao": "青岛",
    "xiamen": "厦门",
    "changsha": "长沙",
    "zhengzhou": "郑州",
    "kunming": "昆明",
    "harbin": "哈尔滨",
    "haerbin": "哈尔滨",
    "urumqi": "乌鲁木齐",
    "wulumuqi": "乌鲁木齐",
    "lanzhou": "兰州",
    "shijiazhuang": "石家庄",
    "taiyuan": "太原",
    "shenyang": "沈阳",
    "dalian": "大连",
    "changchun": "长春",
    "jinan": "济南",
    "fuzhou": "福州",
    "nanchang": "南昌",
    "guiyang": "贵阳",
    "nanning": "南宁",
    "haikou": "海口",
    "sanya": "三亚",
    "hohhot": "呼和浩特",
    "lasa": "拉萨",
    "yinchuan": "银川",
    "xining": "西宁",
    "hefei": "合肥",
    "jiayuguan": "嘉峪关",
    "dunhuang": "敦煌",
}


def _has_cjk(s: str) -> bool:
    """判断字符串是否含中文（CJK 统一表意文字）。"""
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


def _display_name(raw: str) -> str:
    """计算展示用城市名：中文输入原样返回；拼音/英文查翻译表，查不到用原输入。"""
    s = str(raw).strip()
    if _has_cjk(s):
        return s
    return _PINYIN_ZH.get(s.lower(), s)


def _send(msg: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


# 常用英文天气 → 中文（wttr.in j1 的 weatherDesc 在 lang=zh 下仍返回英文，做一次粗翻译）。
_WEATHER_ZH: dict[str, str] = {
    "sunny": "晴",
    "clear": "晴",
    "clear sky": "晴",
    "partly cloudy": "多云",
    "partly cloudy day": "多云",
    "partly cloudy night": "多云",
    "overcast": "阴",
    "cloudy": "多云",
    "mostly cloudy": "多云",
    "light rain": "小雨",
    "patchy rain": "零星小雨",
    "rain": "雨",
    "light drizzle": "毛毛雨",
    "moderate rain": "中雨",
    "heavy rain": "大雨",
    "torrential rain": "暴雨",
    "thundery outbreaks in nearby": "雷暴",
    "thunderstorm": "雷阵雨",
    "light thunderstorm": "小雷阵雨",
    "moderate or heavy rain with thunder": "雷阵雨伴大雨",
    "mist": "薄雾",
    "fog": "雾",
    "smoky haze": "薄雾",
    "haze": "雾霾",
    "snow": "雪",
    "light snow": "小雪",
    "heavy snow": "大雪",
    "windy": "大风",
    "light wind": "微风",
}


def _weather_zh(desc: str) -> str:
    """把 wttr.in 英文天气描述翻译成中文；翻译不到则保留原样。"""
    d = (desc or "").strip().lower()
    if d in _WEATHER_ZH:
        return _WEATHER_ZH[d]
    # 模糊包含：如 "Thundery outbreaks in nearby" → "雷暴"
    for key, zh in _WEATHER_ZH.items():
        if key in d:
            return zh
    return desc or "未知"


def _query_wttr(en_name: str) -> dict[str, Any] | None:
    """用 wttr.in 查真实天气（中文描述）。失败返回 None，由调用方 fallback。"""
    url = f"https://wttr.in/{urllib.parse.quote(en_name)}?format=j1&lang=zh"
    req = urllib.request.Request(url, headers={"User-Agent": "work-agent/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read(64 * 1024)
        data = json.loads(raw.decode("utf-8", errors="replace"))
        cc = (data or {}).get("current_condition") or []
        if not cc:
            return None
        c0 = cc[0]
        desc_list = c0.get("weatherDesc") or []
        desc = desc_list[0].get("value", "") if desc_list else ""
        return {
            "weather": _weather_zh(desc),
            "temp": c0.get("temp_C"),
            "hum": c0.get("humidity"),
        }
    except Exception:  # noqa: BLE001 - 网络/解析失败交给调用方 fallback
        return None


def _handle_tools_call(params: dict[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
    name = params.get("name", "")
    args = params.get("arguments") or {}
    if name == "get_weather":
        raw = str(args.get("city", "")).strip()
        if not raw:
            return True, [{"type": "text", "text": "city required"}]
        query = raw  # 中文 / 拼音 / 英文 原样交给 wttr.in，wttr.in 支持中文城市名
        disp = _display_name(raw)
        # 直查真实天气（不设白名单，任意城市都查）。
        live = _query_wttr(query)
        if live:
            cond = live["weather"] or "未知"
            temp = live["temp"] if live["temp"] is not None else "?"
            hum = live["hum"] if live["hum"] is not None else "?"
            return False, [
                {"type": "text", "text": f"{disp}（实时）: {cond}, {temp}°C, 湿度 {hum}%"}
            ]
        return False, [
            {
                "type": "text",
                "text": f"{disp}（离线数据）: 暂时无法获取实时天气，请稍后重试。",
            }
        ]
    return True, [{"type": "text", "text": f"unknown tool: {name}"}]


def main() -> None:
    # 关键：MCP stdio 传输用 UTF-8。Windows 下子进程 stdin/stdout 默认 GBK，
    # 不重配置会导致中文 JSON 乱码（客户端按 UTF-8 写，子进程按 GBK 读 → 乱码）。
    for _stream in (sys.stdin, sys.stdout):
        _reconf = getattr(_stream, "reconfigure", None)
        if callable(_reconf):
            try:
                _reconf(encoding="utf-8")
            except (TypeError, ValueError):
                pass
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        rid = msg.get("id")
        if method == "initialize":
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                    },
                }
            )
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "tools": [
                            {
                                "name": "get_weather",
                                "description": "查询某个城市的实时天气（只读，返回天气/温度/湿度；支持中文、拼音、英文城市名）",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"city": {"type": "string"}},
                                    "required": ["city"],
                                },
                            }
                        ]
                    },
                }
            )
        elif method == "tools/call":
            is_error, content = _handle_tools_call(msg.get("params") or {})
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {"isError": is_error, "content": content},
                }
            )
        else:
            if rid is not None:
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "error": {"code": -32601, "message": f"method not found: {method}"},
                    }
                )


if __name__ == "__main__":
    main()
