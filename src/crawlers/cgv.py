"""
CGV 상영 일정 크롤러
- 1순위: CGV 모바일 상영시간 AJAX 응답 직접 조회
- 2순위: Selenium 화면 파싱 fallback
"""

import re
import logging
from datetime import datetime, timedelta
from collections import defaultdict

logger = logging.getLogger(__name__)

MOBILE_SCHEDULE_URL = "https://m.cgv.co.kr/Schedule/cont/ajaxMovieSchedule.aspx"


class CGVCrawler:
    def __init__(self, settings: dict):
        self.theater_code = settings.get("theater_code", "0013")
        self.area_code = settings.get("area_code", "01")
        self.days_ahead = settings.get("days_ahead", 14)
        self.hall_keywords = [kw.upper() for kw in settings.get("hall_keywords", ["IMAX"])]
        self.movie_keywords = [kw.upper() for kw in settings.get("movie_keywords", [])]
        self._driver = None

    def _wanted_movie(self, text: str) -> bool:
        up = (text or "").upper()
        return not self.movie_keywords or any(k in up for k in self.movie_keywords)

    def _wanted_hall(self, text: str) -> bool:
        up = (text or "").upper()
        return any(k in up for k in self.hall_keywords)

    def check(self) -> dict:
        # 1) 모바일 AJAX 엔드포인트 직접 조회
        schedules, raw_parts = self._check_mobile_ajax()
        if schedules:
            logger.info(f"CGV 모바일 조회 성공: 매칭 일정 {len(schedules)}건")
            return {"schedules": schedules, "raw_data": "\n".join(raw_parts)}

        logger.warning("CGV 모바일 조회에서 매칭 0건. Selenium fallback 시도")

        # 2) Selenium fallback
        driver = self._get_driver()
        if not driver:
            return {"schedules": [], "raw_data": "\n".join(raw_parts) or "driver-error"}
        try:
            s2, r2 = self._check_with_selenium(driver)
            logger.info(f"CGV Selenium 검사 완료: 매칭 일정 {len(s2)}건")
            return {"schedules": s2, "raw_data": "\n".join(raw_parts + r2)}
        except Exception as e:
            logger.error(f"Selenium 크롤링 실패: {e}")
            self.close()
            return {"schedules": [], "raw_data": "\n".join(raw_parts + [f"error:{e}"])}

    def _check_mobile_ajax(self):
        import requests
        from bs4 import BeautifulSoup

        headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; Mobile) AppleWebKit/537.36 Chrome/131.0.0.0 Mobile Safari/537.36",
            "Referer": f"https://m.cgv.co.kr/WebAPP/TheaterV4/TheaterDetail.aspx?tc={self.theater_code}",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }

        all_schedules = []
        raw_parts = []
        today = datetime.now()

        for i in range(self.days_ahead + 1):
            target_date = today + timedelta(days=i)
            date_str = target_date.strftime("%Y%m%d")
            date_display = target_date.strftime("%Y-%m-%d (%a)")

            try:
                resp = requests.post(
                    MOBILE_SCHEDULE_URL,
                    data={"theaterCd": self.theater_code, "playYMD": date_str},
                    headers=headers,
                    timeout=20,
                )
                text = resp.text or ""
                logger.info(
                    f"CGV 모바일 {date_str}: HTTP {resp.status_code}, 응답 {len(text)}자, "
                    f"오디세이={'Y' if '오디세이' in text else 'N'}, IMAX={'Y' if 'IMAX' in text.upper() else 'N'}"
                )
                raw_parts.append(f"[{date_str}]status={resp.status_code};len={len(text)};" + text[:5000])

                if resp.status_code != 200 or not text:
                    continue

                soup = BeautifulSoup(text, "html.parser")
                grouped = {}

                # CGV 모바일 응답의 상영시간 링크: javascript:popupSchedule('영화명','관명','시간', ...)
                for a in soup.find_all("a"):
                    js = (a.get("href") or "") + " " + (a.get("onclick") or "")
                    if "popupSchedule" not in js:
                        continue

                    args = re.findall(r"'([^']*)'", js)
                    if len(args) < 3:
                        continue

                    movie = args[0].strip()
                    hall = args[1].strip()
                    start_time = args[2].strip()

                    if not self._wanted_movie(movie):
                        continue
                    if not self._wanted_hall(hall):
                        continue
                    if not re.fullmatch(r"[0-2]?\d:[0-5]\d", start_time):
                        continue

                    key = (movie, hall)
                    if key not in grouped:
                        grouped[key] = {
                            "movie": movie,
                            "hall": hall,
                            "times": [],
                            "date": date_display,
                            "date_raw": date_str,
                        }
                    if start_time not in [t["start"] for t in grouped[key]["times"]]:
                        grouped[key]["times"].append({"start": start_time})

                day_results = list(grouped.values())

                # popupSchedule 구조가 달라진 경우: 응답 텍스트 전체에서 보강 검색
                if not day_results and self._wanted_movie(text) and self._wanted_hall(text):
                    day_results = self._parse_text_fallback(text, date_display, date_str)

                if day_results:
                    logger.info(f"CGV 모바일 {date_str}: 오디세이 IMAX {len(day_results)}건 발견")
                    all_schedules.extend(day_results)

            except Exception as e:
                logger.warning(f"CGV 모바일 {date_str} 조회 실패: {e}")
                raw_parts.append(f"[{date_str}]mobile-error:{e}")

        return self._dedupe(all_schedules), raw_parts

    def _parse_text_fallback(self, text: str, date_display: str, date_raw: str):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(text, "html.parser")
        visible = soup.get_text("\n", strip=True)
        lines = [re.sub(r"\s+", " ", x).strip() for x in visible.splitlines() if x.strip()]
        results = []

        for i, line in enumerate(lines):
            if not self._wanted_movie(line):
                continue
            block_lines = lines[max(0, i - 5): min(len(lines), i + 50)]
            block = " | ".join(block_lines)
            if not self._wanted_hall(block):
                continue
            times = []
            for t in re.findall(r"(?<!\d)([0-2]?\d:[0-5]\d)(?!\d)", block):
                if t not in times:
                    times.append(t)
            if not times:
                continue
            hall = next((x for x in block_lines if self._wanted_hall(x)), "IMAX")
            results.append({
                "movie": line,
                "hall": hall[:100],
                "times": [{"start": t} for t in times],
                "date": date_display,
                "date_raw": date_raw,
            })
        return results

    def _get_driver(self):
        if self._driver is not None:
            return self._driver
        try:
            import os
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service

            options = Options()
            options.add_argument("--headless=new")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--window-size=1920,1080")
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option("useAutomationExtension", False)

            chrome_path = os.environ.get("CHROME_PATH")
            if chrome_path:
                options.binary_location = chrome_path
            driver_path = os.environ.get("CHROMEDRIVER_PATH")
            service = Service(driver_path) if driver_path else Service()

            self._driver = webdriver.Chrome(service=service, options=options)
            self._driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
            )
            return self._driver
        except Exception as e:
            logger.error(f"Selenium WebDriver 생성 실패: {e}")
            return None

    def _check_with_selenium(self, driver):
        import time
        schedules = []
        raw_parts = []
        today = datetime.now()

        for i in range(self.days_ahead + 1):
            target_date = today + timedelta(days=i)
            date_str = target_date.strftime("%Y%m%d")
            date_display = target_date.strftime("%Y-%m-%d (%a)")
            url = (
                f"https://www.cgv.co.kr/reserve/show-times/?areacode={self.area_code}"
                f"&theaterCode={self.theater_code}&date={date_str}"
            )
            try:
                driver.get(url)
                time.sleep(3)
                visible = driver.find_element("tag name", "body").text
                raw_parts.append(f"[{date_str}]selenium:" + visible[:12000])
                parsed = self._parse_text_fallback(visible, date_display, date_str)
                schedules.extend(parsed)
            except Exception as e:
                logger.warning(f"Selenium 날짜 {date_str} 조회 실패: {e}")

        return self._dedupe(schedules), raw_parts

    def close(self):
        if self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None

    def _dedupe(self, schedules):
        out = []
        seen = set()
        for r in schedules:
            key = (
                r.get("date_raw"),
                r.get("movie"),
                r.get("hall"),
                tuple(t.get("start") for t in r.get("times", [])),
            )
            if key not in seen:
                seen.add(key)
                out.append(r)
        return out

    def format_message(self, schedules: list[dict]) -> str:
        if not schedules:
            return ""

        now_str = _esc(datetime.now().strftime("%Y-%m-%d %H:%M"))
        lines = [
            "🚨 *CGV 오디세이 IMAX 발견\\!*",
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
                lines.append(f"  ⏰ {_esc(', '.join(t['start'] for t in item.get('times', [])))}")
            lines.append("")

        lines.append("👇 *CGV에서 바로 예매를 확인하세요\\!*")
        return "\n".join(lines)


def _esc(text: str) -> str:
    text = str(text)
    for ch in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text
