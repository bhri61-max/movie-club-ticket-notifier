"""
CGV 상영 일정 크롤러 (Selenium 기반)
"""

import re
import logging
from datetime import datetime, timedelta
from collections import defaultdict

logger = logging.getLogger(__name__)

CGV_BOOKING_URL = "https://www.cgv.co.kr/reserve/show-times/?areacode={area}&theaterCode={theater}"


class CGVCrawler:
    def __init__(self, settings: dict):
        self.theater_code = settings.get("theater_code", "0013")
        self.area_code = settings.get("area_code", "01")
        self.days_ahead = settings.get("days_ahead", 14)
        self.hall_keywords = [kw.upper() for kw in settings.get("hall_keywords", ["IMAX"])]
        self.movie_keywords = [kw.upper() for kw in settings.get("movie_keywords", [])]
        self._driver = None

    def _get_driver(self):
        if self._driver is not None:
            return self._driver
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service
            import os

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
            self._driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"})
            return self._driver
        except Exception as e:
            logger.error(f"Selenium WebDriver 생성 실패: {e}")
            return None

    def close(self):
        if self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None

    def check(self) -> dict:
        driver = self._get_driver()
        if not driver:
            return {"schedules": [], "raw_data": "driver-error"}
        try:
            schedules, raw_parts = self._check_with_selenium(driver)
            logger.info(f"CGV 검사 완료: 매칭 일정 {len(schedules)}건")
            return {"schedules": schedules, "raw_data": "\n".join(raw_parts)}
        except Exception as e:
            logger.error(f"Selenium 크롤링 실패: {e}")
            self.close()
            return {"schedules": [], "raw_data": f"error:{e}"}

    def _check_with_selenium(self, driver):
        import time
        schedules = []
        raw_parts = []
        today = datetime.now()

        for i in range(self.days_ahead + 1):
            target_date = today + timedelta(days=i)
            date_str = target_date.strftime("%Y%m%d")
            date_display = target_date.strftime("%Y-%m-%d (%a)")
            url = (f"https://www.cgv.co.kr/reserve/show-times/?areacode={self.area_code}"
                   f"&theaterCode={self.theater_code}&date={date_str}")
            try:
                driver.get(url)
                time.sleep(3)
                html = driver.page_source
                visible = driver.find_element("tag name", "body").text
                # 변경 감지에 페이지 앞 200자만 쓰던 문제를 수정: 검색 대상 텍스트 전체의 핵심값 사용
                raw_parts.append(f"[{date_str}]" + visible[:12000])
                parsed = self._parse_visible_text(visible, date_display, date_str)
                if not parsed:
                    parsed = self._parse_page_source(html, date_display, date_str)
                if parsed:
                    logger.info(f"CGV {date_str}: 오디세이/IMAX 후보 {len(parsed)}건 발견")
                schedules.extend(parsed)
            except Exception as e:
                logger.warning(f"날짜 {date_str} 조회 실패: {e}")
        return self._dedupe(schedules), raw_parts

    def _wanted_movie(self, text: str) -> bool:
        up = text.upper()
        return not self.movie_keywords or any(k in up for k in self.movie_keywords)

    def _wanted_hall(self, text: str) -> bool:
        up = text.upper()
        return any(k in up for k in self.hall_keywords)

    def _parse_visible_text(self, text: str, date_display: str, date_raw: str):
        """현재 CGV 화면의 실제 보이는 텍스트를 이용한 보강 파서."""
        results = []
        lines = [re.sub(r"\s+", " ", x).strip() for x in text.splitlines() if x.strip()]
        movie_indexes = [i for i, line in enumerate(lines) if self._wanted_movie(line)]
        for mi in movie_indexes:
            # 영화명 주변 블록에서 IMAX와 시간을 함께 찾는다.
            start = max(0, mi - 3)
            end = min(len(lines), mi + 35)
            block_lines = lines[start:end]
            block = " | ".join(block_lines)
            if not self._wanted_hall(block):
                continue
            times = []
            for t in re.findall(r"(?<!\d)([0-2]?\d:[0-5]\d)(?!\d)", block):
                if t not in times:
                    times.append(t)
            if not times:
                continue
            movie = lines[mi]
            hall = next((x for x in block_lines if self._wanted_hall(x)), "IMAX")
            results.append({"movie": movie, "hall": hall[:100], "times": [{"start": t} for t in times], "date": date_display, "date_raw": date_raw})
        return results

    def _parse_page_source(self, html: str, date_display: str, date_raw: str):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text("\n", strip=True)
        return self._parse_visible_text(text, date_display, date_raw)

    def _dedupe(self, schedules):
        out = []
        seen = set()
        for r in schedules:
            key = (r.get("date_raw"), r.get("movie"), r.get("hall"), tuple(t.get("start") for t in r.get("times", [])))
            if key not in seen:
                seen.add(key)
                out.append(r)
        return out

    def format_message(self, schedules: list[dict]) -> str:
        if not schedules:
            return ""
        now_str = _esc(datetime.now().strftime("%Y-%m-%d %H:%M"))
        lines = ["🚨 *CGV 오디세이 IMAX 발견\\!*", f"⏰ 감지 시각: {now_str}", "━━━━━━━━━━━━━━━━━━━━", ""]
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
