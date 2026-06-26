import asyncio
import json
import math
import os
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

import httpx

KMA_API_KEY = os.environ.get("KMA_API_KEY", "")
KMA_URL = "http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"

STADIUMS = [
    {"id": "jamsil",   "name": "잠실야구장",            "teams": "LG · 두산", "lat": 37.5122, "lon": 127.0717, "is_dome": False},
    {"id": "gocheok",  "name": "고척스카이돔",           "teams": "키움",      "lat": 37.4982, "lon": 126.8672, "is_dome": True},
    {"id": "suwon",    "name": "수원 KT위즈파크",        "teams": "KT",        "lat": 37.2997, "lon": 127.0095, "is_dome": False},
    {"id": "incheon",  "name": "인천 SSG랜더스필드",     "teams": "SSG",       "lat": 37.4370, "lon": 126.6932, "is_dome": False},
    {"id": "daejeon",  "name": "대전 한화생명이글스파크", "teams": "한화",      "lat": 36.3174, "lon": 127.4287, "is_dome": False},
    {"id": "gwangju",  "name": "광주 기아챔피언스필드",   "teams": "KIA",       "lat": 35.1681, "lon": 126.8887, "is_dome": False},
    {"id": "daegu",    "name": "대구 삼성라이온즈파크",   "teams": "삼성",      "lat": 35.8412, "lon": 128.6818, "is_dome": False},
    {"id": "sajik",    "name": "부산 사직야구장",         "teams": "롯데",      "lat": 35.1940, "lon": 129.0611, "is_dome": False},
    {"id": "changwon", "name": "창원 NC파크",             "teams": "NC",        "lat": 35.2225, "lon": 128.5819, "is_dome": False},
]

PTY_ICON = {0: ("☀️","맑음"), 1: ("🌧️","비"), 2: ("🌨️","비/눈"), 3: ("❄️","눈"), 4: ("🌦️","소나기")}
SKY_ICON = {1: ("☀️","맑음"), 3: ("⛅","구름많음"), 4: ("☁️","흐림")}

_cache: dict = {}
CACHE_TTL = 600


def latlon_to_grid(lat: float, lon: float) -> tuple:
    DEGRAD = math.pi / 180.0
    re = 6371.00877 / 5.0
    slat1 = 30.0 * DEGRAD
    slat2 = 60.0 * DEGRAD
    olon  = 126.0 * DEGRAD
    olat  = 38.0  * DEGRAD

    sn = math.log(math.cos(slat1) / math.cos(slat2)) / \
         math.log(math.tan(math.pi * 0.25 + slat2 * 0.5) /
                  math.tan(math.pi * 0.25 + slat1 * 0.5))
    sf = math.pow(math.tan(math.pi * 0.25 + slat1 * 0.5), sn) * math.cos(slat1) / sn
    ro = re * sf / math.pow(math.tan(math.pi * 0.25 + olat * 0.5), sn)

    ra = re * sf / math.pow(math.tan(math.pi * 0.25 + lat * DEGRAD * 0.5), sn)
    theta = lon * DEGRAD - olon
    if theta >  math.pi: theta -= 2.0 * math.pi
    if theta < -math.pi: theta += 2.0 * math.pi
    theta *= sn

    nx = int(ra * math.sin(theta) + 43 + 0.5)
    ny = int(ro - ra * math.cos(theta) + 136 + 0.5)
    return nx, ny


def base_time_for(date_str: str, game_hour: int) -> tuple:
    """현재 시각 기준 가장 최근 기상청 발표 시각 반환 (발표 후 10분 뒤 제공)"""
    base_times = [2, 5, 8, 11, 14, 17, 20, 23]
    now = datetime.now()
    now_hour   = now.hour
    now_minute = now.minute

    chosen = base_times[0]
    chosen_date = now
    for t in base_times:
        if (now_hour, now_minute) >= (t, 10):
            chosen = t
            chosen_date = now

    if now_hour < 2 or (now_hour == 2 and now_minute < 10):
        chosen = 23
        chosen_date = now - timedelta(days=1)

    return chosen_date.strftime("%Y%m%d"), f"{chosen:02d}00"


def _parse_pcp(raw: str) -> float:
    if not raw or raw in ("강수없음", "0"):
        return 0.0
    raw = str(raw).replace("mm", "").strip()
    if "미만" in raw:
        return 0.5
    if "~" in raw:
        parts = raw.replace("~", "").split()
        nums = [float(p) for p in parts if p.replace(".", "").isdigit()]
        return sum(nums) / len(nums) if nums else 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


async def fetch_weather(client: httpx.AsyncClient, nx: int, ny: int, base_date: str, base_time: str) -> list:
    key = f"{nx}_{ny}_{base_date}_{base_time}"
    now = time.time()
    if key in _cache:
        data, ts = _cache[key]
        if now - ts < CACHE_TTL:
            return data

    query = (
        f"?serviceKey={KMA_API_KEY}"
        f"&pageNo=1&numOfRows=1000&dataType=JSON"
        f"&base_date={base_date}&base_time={base_time}"
        f"&nx={nx}&ny={ny}"
    )
    r = await client.get(KMA_URL + query)
    r.raise_for_status()

    try:
        resp_json = r.json()
    except Exception:
        raise Exception(f"JSON 파싱 실패: {r.text[:200]}")

    result_code = resp_json.get("response", {}).get("header", {}).get("resultCode", "")
    if result_code != "00":
        result_msg = resp_json.get("response", {}).get("header", {}).get("resultMsg", "API 오류")
        raise Exception(f"기상청 API 오류: {result_msg} (code={result_code})")

    body = resp_json["response"].get("body")
    if not body:
        raise Exception(f"응답 body 없음: {resp_json}")

    items = body["items"]["item"]

    by_time: dict = {}
    for item in items:
        dt = item["fcstDate"]
        tm = item["fcstTime"]
        cat = item["category"]
        val = item["fcstValue"]
        by_time.setdefault(dt, {}).setdefault(tm, {})[cat] = val

    result = []
    for fdate in sorted(by_time):
        for ftime in sorted(by_time[fdate]):
            d = by_time[fdate][ftime]
            result.append({
                "fcst_date": fdate,
                "fcst_time": ftime,
                "pop":  int(d.get("POP", 0)),
                "pcp":  _parse_pcp(d.get("PCP", "0")),
                "wsd":  float(d.get("WSD", 0)),
                "pty":  int(d.get("PTY", 0)),
                "sky":  int(d.get("SKY", 1)),
            })

    _cache[key] = (result, now)
    return result


def extract_slots(items: list, date_str: str, game_hour: int) -> list:
    target_date = date_str.replace("-", "")
    slots = []
    for item in items:
        if item["fcst_date"] != target_date:
            continue
        h = int(item["fcst_time"][:2])
        if game_hour <= h <= game_hour + 4:
            pty = item["pty"]
            sky = item["sky"]
            if pty != 0:
                icon, desc = PTY_ICON.get(pty, ("🌧️", "비"))
            else:
                icon, desc = SKY_ICON.get(sky, ("☁️", "흐림"))
            slots.append({
                "time":        f"{h:02d}:00",
                "precip_prob": item["pop"],
                "precip":      item["pcp"],
                "wind":        item["wsd"],
                "icon":        icon,
                "desc":        desc,
            })
    return slots


def evaluate(slots: list, is_dome: bool) -> dict:
    if is_dome:
        return {"level":"DOME","label":"실내 돔 — 취소 없음","avg_precip_prob":0,"total_precip":0,"max_wind":0}
    if not slots:
        return {"level":"ERROR","label":"예보 데이터 없음","avg_precip_prob":0,"total_precip":0,"max_wind":0}

    avg_prob   = sum(s["precip_prob"] for s in slots) / len(slots)
    total_mm   = sum(s["precip"]      for s in slots)
    max_wind   = max(s["wind"]        for s in slots)
    max_precip = max(s["precip"]      for s in slots)

    if avg_prob >= 75 or max_precip >= 10: level, label = "HIGH",    "취소 유력"
    elif avg_prob >= 60 or max_precip >= 5:level, label = "DELAY",   "중단 후 재개 가능"
    elif avg_prob >= 40 or max_precip >= 3:level, label = "CAUTION", "취소 가능성 있음"
    elif avg_prob >= 20 or max_precip >= 1:level, label = "LOW",     "우산 지참 권장"
    else:                                  level, label = "OK",      "경기 정상 예상"

    return {"level":level,"label":label,"avg_precip_prob":round(avg_prob,1),
            "total_precip":round(total_mm,1),"max_wind":round(max_wind,1)}


async def fetch_stadium(client: httpx.AsyncClient, s: dict, base_date: str, base_time: str, date: str, game_hour: int) -> dict:
    if s["is_dome"]:
        return {**s, "prediction": evaluate([], True), "hourly_slots": []}
    try:
        nx, ny = latlon_to_grid(s["lat"], s["lon"])
        items  = await fetch_weather(client, nx, ny, base_date, base_time)
        slots  = extract_slots(items, date, game_hour)
        return {**s, "prediction": evaluate(slots, False), "hourly_slots": slots}
    except Exception as e:
        return {**s, "prediction": {"level":"ERROR","label":str(e),
            "avg_precip_prob":0,"total_precip":0,"max_wind":0}, "hourly_slots":[]}


async def build_response(date: str, game_hour: int) -> dict:
    base_date, base_time = base_time_for(date, game_hour)
    async with httpx.AsyncClient(timeout=10) as client:
        results = await asyncio.gather(
            *[fetch_stadium(client, s, base_date, base_time, date, game_hour) for s in STADIUMS]
        )
    return {"date": date, "game_hour": game_hour, "stadiums": list(results)}


def cors_headers():
    return {"Access-Control-Allow-Origin":"*","Access-Control-Allow-Methods":"GET,OPTIONS",
            "Access-Control-Allow-Headers":"Content-Type","Content-Type":"application/json"}


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        [self.send_header(k, v) for k, v in cors_headers().items()]
        self.end_headers()

    def do_GET(self):
        params    = parse_qs(urlparse(self.path).query)
        date      = (params.get("date")      or [datetime.now().strftime("%Y-%m-%d")])[0]
        game_hour = int((params.get("game_hour") or ["18"])[0])

        if not KMA_API_KEY:
            self._err(500, "KMA_API_KEY 환경변수가 설정되지 않았습니다")
            return

        try:
            body = asyncio.run(build_response(date, game_hour))
            self._ok(body)
        except Exception as e:
            self._err(500, str(e))

    def _ok(self, body):
        payload = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(200)
        [self.send_header(k, v) for k, v in cors_headers().items()]
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _err(self, code, msg):
        payload = json.dumps({"detail": msg}, ensure_ascii=False).encode()
        self.send_response(code)
        [self.send_header(k, v) for k, v in cors_headers().items()]
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a): pass
