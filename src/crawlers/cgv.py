"""
CGV 상영 일정 크롤러 (Selenium 기반)

Cloudflare 보호를 우회하기 위해 실제 브라우저를 사용합니다.
참고: https://github.com/0w0i0n0g0/cgv-open-push

지원 모드:
  1. Selenium (기본) - Cloudflare 우회 가능, Chrome 필요
  2. requests fallback - Selenium 실패 시 시도
"""

import re
import logging
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Optional

logger = logging.getLogger(__name__)

# CGV 극장 코드 사전 (자주 쓰이는 극장)
THEATER_CODES = {
    "용산아이파크몰": "0013",
    "영등포": "0059",
    "왕십리": "0074",
    "강남": "0056",
    "여의도": "0112",
    "수원": "0012",
    "부산센텀시티": "0061",
}

# CGV 상영시간표 URL
CGV_SCHEDULE_URL = "https://www.cgv.co.kr/reserve/show-times/"
CGV_BOOKING_URL = "https://www.cgv.co.kr/reserve/show-times/?areacode={area}&theaterCode={theater}"


class CGVCrawler:
    """CGV 상영 일정 크롤러"""

    def __init__(self, settings: dict):
        self.theater_code = settings.get("theater_code", "0013")
        self.area_code = settings.get("area_code", "01")
        self.days_ahead = settings.get("days_ahead", 14)
        self.hall_keywords = [
            kw.upper() for kw in settings.get("hall_keywords", ["IMAX"])
        ]
        self.movie_keywords = [
            kw.upper() for kw in settings.get("movie_keywords", [])
        ]
        self._driver = None

    def _get_driver(self):
        """Selenium WebDriver를 생성합니다."""
        if self._driver is not None:
            return self._driver

        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service

            options = Options()
            options.add_argument("--headless=new")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--window-size=1920,1080")
            options.add_argument(
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            )
            # Cloudflare 감지 우회를 위한 추가 설정
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option("useAutomationExtension", False)

            self._driver = webdriver.Chrome(options=options)
            # navigator.webdriver 속성 제거
            self._driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": """
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined
                    })
                    """
                },
            )
            return self._driver
        except Exception as e:
            logger.error(f"Selenium WebDriver 생성 실패: {e}")
            return None

    def close(self):
        """WebDriver를 종료합니다."""
        if self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None

    def check(self) -> dict:
        """
        상영 일정을 조회하여 결과를 반환합니다.

        Returns:
            {
                "schedules": [...],  # 매칭된 상영 일정 목록
                "raw_data": str,     # 변경 감지용 원본 데이터
            }
        """
        schedules = []
        raw_parts = []

        driver = self._get_driver()
        if not driver:
            logger.warning("Selenium 사용 불가. requests fallback 시도.")
            return self._check_with_requests()

        try:
            schedules, raw_parts = self._check_with_selenium(driver)
        except Exception as e:
            logger.error(f"Selenium 크롤링 실패: {e}")
            self.close()
            logger.info("requests fallback 시도...")
            return self._check_with_requests()

        return {
            "schedules": schedules,
            "raw_data": "\n".join(raw_parts),
        }

    def _check_with_selenium(self, driver) -> tuple[list, list]:
        """Selenium으로 상영 일정을 조회합니다."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        import time

        schedules = []
        raw_parts = []
        today = datetime.now()

        for i in range(self.days_ahead + 1):
            target_date = today + timedelta(days=i)
            date_str = target_date.strftime("%Y%m%d")
            date_display = target_date.strftime("%Y-%m-%d (%a)")

            url = (
                f"https://www.cgv.co.kr/reserve/show-times/"
                f"?areacode={self.area_code}"
                f"&theaterCode={self.theater_code}"
                f"&date={date_str}"
            )

            try:
                driver.get(url)
                # 페이지 로딩 대기 (최대 15초)
                time.sleep(3)

                # 페이지 소스 가져오기
                page_source = driver.page_source
                raw_parts.append(f"[{date_str}]{page_source[:200]}")

                # 상영 일정 파싱
                parsed = self._parse_page_source(page_source, date_display, date_str)
                schedules.extend(parsed)

            except Exception as e:
                logger.warning(f"날짜 {date_str} 조회 실패: {e}")
                continue

        return schedules, raw_parts

    def _check_with_requests(self) -> dict:
        """requests + cloudscraper로 상영 일정을 조회합니다 (fallback)."""
        try:
            import cloudscraper
        except ImportError:
            logger.error("cloudscraper가 설치되지 않았습니다: pip install cloudscraper")
            return {"schedules": [], "raw_data": ""}

        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )

        schedules = []
        raw_parts = []
        today = datetime.now()

        for i in range(self.days_ahead + 1):
            target_date = today + timedelta(days=i)
            date_str = target_date.strftime("%Y%m%d")
            date_display = target_date.strftime("%Y-%m-%d (%a)")

            url = (
                f"https://www.cgv.co.kr/common/showtimes/iframeTheater.aspx"
                f"?areacode={self.area_code}"
                f"&theatercode={self.theater_code}"
                f"&date={date_str}"
            )

            try:
                resp = scraper.get(url, timeout=15)
                if resp.status_code == 200 and "col-times" in resp.text:
                    raw_parts.append(f"[{date_str}]{resp.text[:200]}")
                    parsed = self._parse_legacy_html(resp.text, date_display, date_str)
                    schedules.extend(parsed)
                else:
                    raw_parts.append(f"[{date_str}]empty")
            except Exception as e:
                logger.warning(f"requests 조회 실패 (날짜: {date_str}): {e}")

        return {
            "schedules": schedules,
            "raw_data": "\n".join(raw_parts),
        }

    def _parse_page_source(
        self, html: str, date_display: str, date_raw: str
    ) -> list[dict]:
        """
        CGV 페이지 소스에서 상영 정보를 추출합니다.
        새 사이트(React)와 레거시 사이트 모두 지원합니다.
        """
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        results = []

        # ── 새 사이트 (React/Next.js) 파싱 ──
        # 상영관 정보가 포함된 요소 탐색
        hall_sections = soup.select("[class*='hall'], [class*='screen'], [class*='theater']")
        for section in hall_sections:
            text = section.get_text(" ", strip=True)
            # IMAX 등 특별관 키워드 매칭
            if any(kw in text.upper() for kw in self.hall_keywords):
                # 영화 제목과 시간 추출 시도
                movie_el = section.find_previous(["h3", "h4", "strong", "span"], class_=re.compile(r"movie|title|name", re.I))
                movie_title = movie_el.get_text(strip=True) if movie_el else "영화 제목 확인 필요"

                times = re.findall(r"(\d{1,2}:\d{2})", text)
                if times:
                    results.append({
                        "movie": movie_title,
                        "hall": text[:50],
                        "times": [{"start": t} for t in times],
                        "date": date_display,
                        "date_raw": date_raw,
                    })

        # ── 레거시 사이트 파싱 (fallback) ──
        if not results:
            results = self._parse_legacy_html(html, date_display, date_raw)

        # 영화 키워드 필터링
        if self.movie_keywords:
            results = [
                r for r in results
                if any(kw in r["movie"].upper() for kw in self.movie_keywords)
            ]

        return results

    def _parse_legacy_html(
        self, html: str, date_display: str, date_raw: str
    ) -> list[dict]:
        """CGV 레거시 상영시간표 HTML을 파싱합니다."""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        results = []

        movie_sections = soup.select("div.col-times")
        for section in movie_sections:
            title_el = section.select_one("div.info-movie a strong")
            if not title_el:
                title_el = section.select_one("div.info-movie strong")
            movie_title = title_el.get_text(strip=True) if title_el else "알 수 없음"

            type_halls = section.select("div.type-hall")
            for hall_section in type_halls:
                hall_name_el = hall_section.select_one("div.info-hall li:first-child")
                hall_name = (
                    hall_name_el.get_text(strip=True) if hall_name_el else "알 수 없음"
                )

                # 특별관 키워드 매칭
                if not any(kw in hall_name.upper() for kw in self.hall_keywords):
                    continue

                time_entries = []
                time_links = hall_section.select(
                    "div.info-timetable a, div.info-timetable em"
                )
                for tl in time_links:
                    time_text = tl.get_text(strip=True)
                    time_match = re.search(r"(\d{1,2}:\d{2})", time_text)
                    if time_match:
                        time_entries.append({"start": time_match.group(1)})

                if time_entries:
                    results.append({
                        "movie": movie_title,
                        "hall": hall_name,
                        "times": time_entries,
                        "date": date_display,
                        "date_raw": date_raw,
                    })

        # 영화 키워드 필터링
        if self.movie_keywords:
            results = [
                r for r in results
                if any(kw in r["movie"].upper() for kw in self.movie_keywords)
            ]

        return results

    def format_message(self, schedules: list[dict]) -> str:
        """알림 메시지를 포맷팅합니다 (Telegram MarkdownV2)."""
        if not schedules:
            return ""

        now_str = _esc(datetime.now().strftime("%Y\\-%m\\-%d %H:%M"))
        lines = [
            "🚨 *CGV 새 상영 일정 오픈\\!*",
            f"⏰ 감지 시각: {now_str}",
            "━━━━━━━━━━━━━━━━━━━━",
            "",
        ]

        # 날짜별로 그룹핑
        by_date: dict = defaultdict(list)
        for item in schedules:
            by_date[item["date"]].append(item)

        for date, items in by_date.items():
            date_esc = _esc(date)
            lines.append(f"📅 *{date_esc}*")
            for item in items:
                movie = _esc(item["movie"])
                hall = _esc(item["hall"])
                times_str = _esc(", ".join(t["start"] for t in item.get("times", [])))
                lines.append(f"  🎥 {movie}")
                lines.append(f"  🏛 {hall}")
                lines.append(f"  ⏰ {times_str}")
            lines.append("")

        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("👇 *아래 버튼을 눌러 바로 예매하세요\\!*")
        return "\n".join(lines)


def _esc(text: str) -> str:
    """Telegram MarkdownV2 이스케이프"""
    for ch in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text
