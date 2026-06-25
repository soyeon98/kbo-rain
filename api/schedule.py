import re
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
import json

import httpx
from bs4 import BeautifulSoup

STADIUMS = {
    "잠실": {"id": "jamsil", "name": "잠실야구장", "teams": ["LG", "두산"], "lat": 37.5122, "lon": 127.0717, "is_dome": False},
    "고척": {"id": "gocheok", "name": "고척스카이돔", "teams": ["키움"], "lat": 37.4982, "lon": 126.8672, "is_dome": True},
    "수원": {"id": "suwon", "name": "수원 KT위즈파크", "teams": ["KT"], "lat": 37.2997, "lon": 127.0095, "is_dome": False},
    "인천": {"id": "incheon", "name": "인천 SSG랜더스필드", "teams": ["SSG"], "lat": 37.4370, "lon": 126.6932, "is_dome": False},
    "대전": {"id": "daejeon", "name": "대전 한화생명이글스파크", "teams": ["한화"], "lat": 36.3174, "lon": 127.4287, "is_dome": False},
    "광주": {"id": "gwangju", "name": "광주 기아챔피언스필드", "teams": ["KIA"], "lat": 35.1681, "lon": 126.8887, "is_dome": False},
    "대구": {"id": "daegu", "name": "대구 삼성라이온즈파크", "teams": ["삼성"], "lat": 35.8412, "lon": 128.6818, "is_dome": False},
    "사직": {"id": "sajik", "name": "부산 사직야구장", "teams": ["롯데"], "lat": 35.1940, "lon": 129.0611, "is_dome": False},
    "창원": {"id": "changwon", "name": "창원 NC파크", "teams": ["NC"], "lat": 35.2225, "lon": 128.5819, "is_dome": False},
}

KBO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://www.koreabaseball.com/",
}

WEATHER_CODE_MAP = {
    0: ("☀️", "맑음"),
    1: ("🌤️", "대체로 맑음"), 2: ("⛅", "구름 조금"), 3: ("☁️", "흐림"),
    45: ("🌫️", "안개"), 48: ("🌫️", "안개"),
    51: ("🌦️", "이슬비"), 53: ("🌦️", "이슬비"), 55: ("🌧️", "이슬비"),
    61: ("🌧️", "비"), 63: ("🌧️", "비"), 65: ("🌧️", "강한 비"),
    71: ("❄️", "눈"), 73: ("❄️", "눈"), 75: ("❄️", "강한 눈"),
    77: ("🌨️", "눈"),
    80: ("🌦️", "소나기"), 81: ("🌧️", "소나기"), 82: ("⛈️", "강한 소나기"),
    85: ("🌨️", "눈 소나기"), 86: ("🌨️", "강한 눈 소나기"),
    95: ("⛈️", "뇌우"), 96: ("⛈️", "우박 동반 뇌우"), 99: ("⛈️", "강한 우박"),
}


def resolve_stadium(text):
    for keyword, info in STADIUMS.items():
        if keyword in text:
            return info
    return None


def parse_team_name(raw):
    raw = raw.strip()
    for k in ["LG", "두산", "키움", "KT", "SSG", "한화", "KIA", "삼성", "롯데", "NC"]:
        if k in raw:
            return k
    return raw


def evaluate_cancellation(precip_prob, precip, wind, is_dome):
    if is_dome:
        return {"level": "DOME", "label": "실내 돔 — 취소 없음", "score": 0,
                "avg_precip_prob": 0, "total_precip": 0, "max_wind": 0}

    avg_prob = sum(precip_prob) / len(precip_prob) if precip_prob else 0
    total_mm = sum(precip) if precip else 0
    max_wind = max(wind) if wind else 0

    if avg_prob >= 70 and total_mm >= 5:
        level, label = "HIGH", "취소 가능성 높음"
    elif avg_prob >= 50 or total_mm >= 3:
        level, label = "CAUTION", "취소 가능성 있음"
    elif avg_prob >= 30 or total_mm >= 1:
        level, label = "LOW", "취소 가능성 낮음"
    else:
        level, label = "OK", "경기 진행 예정"

    return {
        "level": level, "label": label,
        "avg_precip_prob": round(avg_prob, 1),
        "total_precip": round(total_mm, 1),
        "max_wind": round(max_wind, 1),
    }


def fetch_kbo_schedule(date_str):
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    url = (f"https://www.koreabaseball.com/Schedule/Schedule.aspx"
           f"?leId=1&srId=0&seasonId={dt.year}&gameMonth={dt.month:02d}")

    with httpx.Client(timeout=12, follow_redirects=True) as client:
        resp = client.get(url, headers=KBO_HEADERS)
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.select_one("#tblScheduleList") or soup.select_one("table.tbl-type01")
    if not table:
        return []

    rows = table.select("tbody tr")
    games = []
    current_date = ""

    for row in rows:
        cells = row.select("td")
        if not cells:
            continue
        if "cancel" in " ".join(row.get("class", [])).lower():
            continue

        if len(cells) >= 8:
            raw_date = cells[0].get_text(strip=True)
            if raw_date:
                current_date = raw_date
            time_text = cells[1].get_text(strip=True)
            game_cell = cells[2]
            stadium_cell = cells[7]
        elif len(cells) >= 7:
            time_text = cells[0].get_text(strip=True)
            game_cell = cells[1]
            stadium_cell = cells[6]
        else:
            continue

        if current_date:
            parts = current_date.split(" ")[0].split(".")
            if len(parts) == 2:
                try:
                    if int(parts[0]) != dt.month or int(parts[1]) != dt.day:
                        continue
                except ValueError:
                    continue

        game_text = game_cell.get_text(" ", strip=True)
        vs_match = re.search(r"(.+?)\s*[Vv][Ss]\s*(.+)", game_text)
        if vs_match:
            away_raw, home_raw = vs_match.group(1).strip(), vs_match.group(2).strip()
        else:
            spans = game_cell.select("span, strong, b")
            if len(spans) >= 2:
                away_raw, home_raw = spans[0].get_text(strip=True), spans[-1].get_text(strip=True)
            else:
                continue

        stadium_info = resolve_stadium(stadium_cell.get_text(strip=True))
        if not stadium_info:
            continue

        time_clean = re.sub(r"[^0-9:]", "", time_text)
        if len(time_clean) == 4 and ":" not in time_clean:
            time_clean = time_clean[:2] + ":" + time_clean[2:]

        games.append({
            "time": time_clean or "18:00",
            "home_team": parse_team_name(home_raw),
            "away_team": parse_team_name(away_raw),
            "stadium": stadium_info["name"],
            "stadium_id": stadium_info["id"],
            "lat": stadium_info["lat"],
            "lon": stadium_info["lon"],
            "is_dome": stadium_info["is_dome"],
        })

    return games


def fetch_weather(lat, lon):
    url = (f"https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}"
           f"&hourly=precipitation_probability,precipitation,wind_speed_10m,weather_code"
           f"&timezone=Asia%2FSeoul&forecast_days=7")
    with httpx.Client(timeout=10) as client:
        resp = client.get(url)
        resp.raise_for_status()
    return resp.json()


def extract_game_hours(weather, date_str, start_time):
    times = weather["hourly"]["time"]
    prob = weather["hourly"]["precipitation_probability"]
    precip = weather["hourly"]["precipitation"]
    wind = weather["hourly"]["wind_speed_10m"]
    code = weather["hourly"]["weather_code"]

    try:
        start_h = int(start_time.split(":")[0])
    except Exception:
        start_h = 18

    slots = []
    for i, t in enumerate(times):
        if not t.startswith(date_str):
            continue
        hour = int(t[11:13])
        if start_h <= hour <= start_h + 4:
            icon, desc = WEATHER_CODE_MAP.get(code[i], ("🌡️", "알 수 없음"))
            slots.append({
                "time": t[11:16],
                "precip_prob": prob[i],
                "precip": precip[i],
                "wind": wind[i],
                "weather_code": code[i],
                "icon": icon,
                "desc": desc,
            })

    return {
        "slots": slots,
        "precip_prob": [s["precip_prob"] for s in slots],
        "precip": [s["precip"] for s in slots],
        "wind": [s["wind"] for s in slots],
    }


def cors_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Content-Type": "application/json",
    }


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        for k, v in cors_headers().items():
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        date = (params.get("date") or [datetime.now().strftime("%Y-%m-%d")])[0]

        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            self._respond(400, {"error": "날짜 형식은 YYYY-MM-DD 이어야 합니다"})
            return

        try:
            games_raw = fetch_kbo_schedule(date)
        except Exception as e:
            self._respond(502, {"error": f"KBO 스크래핑 오류: {str(e)}"})
            return

        results = []
        for game in games_raw:
            try:
                weather = fetch_weather(game["lat"], game["lon"])
                hourly = extract_game_hours(weather, date, game["time"])
                prediction = evaluate_cancellation(
                    hourly["precip_prob"], hourly["precip"], hourly["wind"], game["is_dome"]
                )
                results.append({**game, "prediction": prediction, "hourly_slots": hourly["slots"]})
            except Exception:
                results.append({
                    **game,
                    "prediction": {"level": "ERROR", "label": "날씨 데이터 없음",
                                   "avg_precip_prob": 0, "total_precip": 0, "max_wind": 0},
                    "hourly_slots": [],
                })

        self._respond(200, {"date": date, "games": results})

    def _respond(self, status, body):
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        for k, v in cors_headers().items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass
