"""CGV 상영 일정 크롤러
- 2026년 CGV JSON API 사용
- curl_cffi로 실제 Chrome TLS 지문을 사용해 최신 예매 데이터를 조회
- 광교(0257)에서 오디세이 + IMAX 상영회차를 직접 조회
"""

import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

API_URL = "https://cgv.co.kr/api/v1/booking/searchMovScnInfo"
BOOKING_URL = "https://cgv.co.kr/cnm/movieBook"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
    "Referer": BOOKING_URL,
}


class CGVCrawler:
    def __init__(self, settings: dict):
        self.theater_code = settings.get("theater_code", "0257")
        self.area_code = settings.get("area_code", "12")
        self.days_ahead = settings.get("days_ahead", 14)
        self.hall_keywords = [
            kw.upper() for kw in settings.get("hall_keywords", ["IMAX"])
        ]
        self.movie_keywords = [
            kw.upper() for kw in settings.get("movie_keywords", ["오디세이"])
        ]
        self._session = None
        self._session_created_at = 0.0
        self._session_refresh_seconds = 1800

    def _wanted_movie(self, text: str) -> bool:
        up = (text or "").upper()
        return not self.movie_keywords or any(k in up for k in self.movie_keywords)

    def _wanted_hall(self, text: str) -> bool:
        up = (text or "").upper()
        return any(k in up for k in self.hall_keywords)

    def _ensure_session(self):
        from curl_cffi import requests as cf_requests

        now = time.monotonic()
        if (
            self._session is None
            or (now - self._session_created_at) > self._session_refresh_seconds
        ):
            logger.info("CGV 브라우저 세션 새로 발급")
            session = cf_requests.Session(impersonate="chrome")
            page = session.get(BOOKING_URL, timeout=20)
            if page.status_code != 200:
                raise RuntimeError(
                    f"CGV 예매 페이지 방문 실패: HTTP {page.status_code}"
                )
            self._session = session
            self._session_created_at = now

        return self._session

    def _fetch_day(self, ymd: str):
        """하루 시간표를 최신 CGV 브라우저 세션으로 조회한다."""
        last_error = None

        for attempt in range(2):
            try:
                session = self._ensure_session()
                resp = session.get(
                    API_URL,
                    params={
                        "coCd": "A420",
                        "siteNo": self.theater_code,
                        "scnYmd": ymd,
                        "rtctlScopCd": "08",
                    },
                    headers=HEADERS,
                    timeout=20,
                )

                text = resp.text or ""

                if resp.status_code in (403, 429):
                    logger.warning(
                        f"CGV API {ymd}: HTTP {resp.status_code}, 세션 재발급 후 재시도"
                    )
                    self._session = None
                    last_error = RuntimeError(f"HTTP {resp.status_code}")
                    continue

                if resp.status_code != 200:
                    raise RuntimeError(
                        f"CGV API {ymd}: HTTP {resp.status_code}, 응답 {len(text)}자"
                    )

                if text.lstrip().startswith("<"):
                    raise RuntimeError(f"CGV API {ymd}: HTML 차단 응답")

                payload = resp.json()
                if payload.get("statusCode") not in (None, 0):
                    raise RuntimeError(
                        f"CGV API {ymd}: {payload.get('statusMessage')}"
                    )

                rows = payload.get("data") or []
                if not isinstance(rows, list):
                    rows = []
                return rows

            except Exception as e:
                last_error = e
                self._session = None
                if attempt == 0:
                    continue

        raise last_error or RuntimeError(f"CGV API {ymd} 조회 실패")

    def check(self) -> dict:
        schedules = []
        raw_rows = []
        today = datetime.now(ZoneInfo("Asia/Seoul")).date()

        for i in range(self.days_ahead + 1):
            d = today + timedelta(days=i)
            ymd = d.strftime("%Y%m%d")
            date_display = d.strftime("%Y-%m-%d")

            try:
                rows = self._fetch_day(ymd)
                logger.info(f"CGV API {ymd}: 전체 상영 {len(rows)}건")

                grouped = {}
                odi_count = 0
                imax_count = 0

                for r in rows:
                    mov = (
                        str(r.get("movNm") or "")
                        + " "
                        + str(r.get("expoProdNm") or "")
                    ).strip()

                    scr = (
                        str(r.get("scnsNm") or "")
                        + " "
                        + str(r.get("movkndDsplNm") or "")
                    ).strip()

                    start = str(r.get("scnsrtTm") or "").strip()

                    if self._wanted_movie(mov):
                        odi_count += 1
                    if self._wanted_hall(scr):
                        imax_count += 1

                    if not self._wanted_movie(mov):
                        continue
                    if not self._wanted_hall(scr):
                        continue
                    if len(start) < 4:
                        continue

                    digits = "".join(ch for ch in start if ch.isdigit())
                    if len(digits) < 4:
                        continue
                    digits = digits[-4:]
                    start_time = f"{digits[:2]}:{digits[2:]}"

                    screen_name = str(r.get("scnsNm") or "").strip()
                    kind_name = str(r.get("movkndDsplNm") or "").strip()
                    hall = " ".join(x for x in [screen_name, kind_name] if x).strip()
                    if not hall:
                        hall = "IMAX"

                    key = (mov, hall)
                    if key not in grouped:
                        grouped[key] = {
                            "movie": mov,
                            "hall": hall,
                            "times": [],
                            "date": date_display,
                            "date_raw": ymd,
                        }

                    time_entry = {"start": start_time}
                    free = r.get("frSeatCnt")
                    total = r.get("stcnt")
                    if free is not None:
                        time_entry["free"] = free
                    if total is not None:
                        time_entry["total"] = total

                    if start_time not in [t["start"] for t in grouped[key]["times"]]:
                        grouped[key]["times"].append(time_entry)

                day_results = list(grouped.values())

                logger.info(
                    f"CGV API {ymd}: 오디세이 후보 {odi_count}건, "
                    f"IMAX 후보 {imax_count}건, 최종 매칭 {len(day_results)}건"
                )

                if day_results:
                    sample = []
                    for item in day_results[:3]:
                        sample.append(
                            f"{item['movie']} | {item['hall']} | "
                            + ",".join(t["start"] for t in item["times"])
                        )
                    logger.info(
                        f"CGV API {ymd}: 오디세이 IMAX 발견 -> "
                        + " || ".join(sample)
                    )
                    schedules.extend(day_results)

                raw_rows.append(
                    {
                        "date": ymd,
                        "match": [
                            {
                                "movie": item["movie"],
                                "hall": item["hall"],
                                "times": [
                                    {
                                        "start": t["start"],
                                        "free": t.get("free"),
                                        "total": t.get("total"),
                                    }
                                    for t in item["times"]
                                ],
                            }
                            for item in day_results
                        ],
                    }
                )

            except Exception as e:
                logger.warning(f"CGV API {ymd} 조회 실패: {e}")
                raw_rows.append({"date": ymd, "exception": str(e)})

        logger.info(f"CGV 검사 완료: 오디세이 IMAX 매칭 총 {len(schedules)}건")

        return {
            "schedules": schedules,
            "raw_data": json.dumps(raw_rows, ensure_ascii=False, sort_keys=True),
        }

    def close(self):
        try:
            if self._session is not None:
                self._session.close()
        except Exception:
            pass
        self._session = None

    def format_message(self, schedules: list[dict]) -> str:
        if not schedules:
            return ""

        now_str = _esc(
            datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")
        )

        lines = [
            "🚨 *CGV 광교 오디세이 IMAX 오픈\\!*",
            f"⏰ 감지 시각: {now_str}",
            "━━━━━━━━━━━━━━━━━━━━",
            "",
        ]

        by_date = defaultdict(list)
        for item in schedules:
            by_date[item["date"]].append(item)

        for date, items in by_date.items():
            lines.append(f"📅 *{_esc(date)}*")
            for item in items:
                lines.append(f"  🎥 {_esc(item['movie'])}")
                lines.append(f"  🏛 {_esc(item['hall'])}")

                time_parts = []
                for t in item.get("times", []):
                    s = t["start"]
                    if "free" in t and "total" in t:
                        s += f" ({t['free']}/{t['total']}석)"
                    time_parts.append(s)

                lines.append(f"  ⏰ {_esc(', '.join(time_parts))}")
            lines.append("")

        lines.append("👇 *지금 바로 CGV에서 확인하세요\\!*")
        return "\n".join(lines)


def _esc(text: str) -> str:
    text = str(text)
    for ch in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text
