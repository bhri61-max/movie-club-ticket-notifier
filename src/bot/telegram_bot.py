"""
텔레그램 봇 핵심 엔진 v2

기능:
  - /start: 봇 시작 및 채팅 ID 자동 등록
  - /status: 현재 모니터링 상태 확인
  - /check: 즉시 전체 모니터링 실행 (예쁜 포맷팅 + 진행 상황 표시)
  - /add <url>: 사용자가 직접 URL을 추가하여 모니터링
  - /list: 내가 추가한 모니터링 목록 확인
  - /remove <번호>: 내가 추가한 모니터링 제거
  - /help: 도움말
  - 알림 시 웹/앱 바로가기 버튼 제공 (open_mode: web/app/both)
"""

import logging
import asyncio
import json
import os
from datetime import datetime
from typing import Any

from telegram import (
    Update,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)
from telegram.constants import ParseMode

logger = logging.getLogger(__name__)

# CGV 딥링크 / 웹 URL
CGV_WEB_LOGIN_URL = "https://cgv.co.kr/mem/login?returnUrl=%2Ftme%2FtmeShowMore"
CGV_APP_DEEPLINK = "intent://cgv.co.kr/reserve/show-times/#Intent;scheme=https;package=com.cgv.android.movieapp;end"
CGV_APP_STORE_IOS = "https://apps.apple.com/kr/app/cgv/id388521649"
CGV_APP_STORE_ANDROID = "market://details?id=com.cgv.android.movieapp"


class MovieClubBot:
    """영화 동아리 알림 텔레그램 봇 v2"""

    def __init__(self, config: dict):
        self.config = config
        self.token = config["telegram"]["bot_token"]
        self.chat_ids: set[str] = set(
            str(cid) for cid in config["telegram"].get("chat_ids", [])
        )
        self.watchers_config = config.get("watchers", [])
        self.advanced = config.get("advanced", {})
        self.app: Application | None = None

        # 크롤러 인스턴스 캐시
        self._crawlers: dict[str, Any] = {}

        # 상태 관리자
        from src.state import StateManager
        state_dir = self.advanced.get("state_dir", "./data/state")
        self.state_mgr = StateManager(state_dir)

        # 사용자 추가 watcher 저장 경로
        self._user_watchers_path = os.path.join(
            self.advanced.get("state_dir", "./data/state"), "user_watchers.json"
        )
        self._user_watchers: dict[str, list] = self._load_user_watchers()

    # ── 사용자 추가 watcher 관리 ──────────────────────────────

    def _load_user_watchers(self) -> dict:
        """사용자가 추가한 watcher 목록을 파일에서 불러옵니다."""
        if os.path.exists(self._user_watchers_path):
            try:
                with open(self._user_watchers_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_user_watchers(self):
        """사용자가 추가한 watcher 목록을 파일에 저장합니다."""
        os.makedirs(os.path.dirname(self._user_watchers_path), exist_ok=True)
        with open(self._user_watchers_path, "w", encoding="utf-8") as f:
            json.dump(self._user_watchers, f, ensure_ascii=False, indent=2)

    def _get_all_watchers(self) -> list:
        """기본 watcher + 사용자 추가 watcher를 합쳐서 반환합니다."""
        base = [w for w in self.watchers_config if w.get("enabled", True)]
        user_all = []
        for watchers in self._user_watchers.values():
            user_all.extend(watchers)
        return base + user_all

    # ── 크롤러 팩토리 ──────────────────────────────────────────

    def _get_crawler(self, watcher: dict):
        """watcher 설정에 따라 적절한 크롤러를 반환합니다."""
        name = watcher["name"]
        if name in self._crawlers:
            return self._crawlers[name]

        wtype = watcher.get("type", "webpage")
        settings = watcher.get("settings", {})

        if wtype == "cgv":
            from src.crawlers.cgv import CGVCrawler
            crawler = CGVCrawler(settings)
        elif wtype == "webpage":
            from src.crawlers.webpage import WebpageCrawler
            crawler = WebpageCrawler(settings)
        else:
            logger.error(f"알 수 없는 watcher 타입: {wtype}")
            return None

        self._crawlers[name] = crawler
        return crawler

    # ── 알림 전송 ──────────────────────────────────────────────

    async def _send_alert(
        self,
        text: str,
        context: ContextTypes.DEFAULT_TYPE,
        keyboard: InlineKeyboardMarkup | None = None,
    ):
        """등록된 모든 채팅방에 알림을 전송합니다."""
        for chat_id in self.chat_ids:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    disable_web_page_preview=True,
                    reply_markup=keyboard,
                )
            except Exception as e:
                logger.error(f"알림 전송 실패 (chat_id={chat_id}): {e}")
                # MarkdownV2 파싱 실패 시 일반 텍스트로 재시도
                try:
                    plain = text
                    for ch in r"\_*[]()~`>#+-=|{}.!":
                        plain = plain.replace(f"\\{ch}", ch)
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=plain,
                        reply_markup=keyboard,
                    )
                except Exception as e2:
                    logger.error(f"일반 텍스트 전송도 실패: {e2}")

    def _build_keyboard(self, watcher: dict, link: str = "") -> InlineKeyboardMarkup | None:
        """
        watcher 설정의 open_mode에 따라 인라인 키보드를 생성합니다.

        open_mode:
          - "web"  : 웹 브라우저 버튼만
          - "app"  : 앱 딥링크 버튼만
          - "both" : 웹 + 앱 버튼 모두
          - "none" : 버튼 없음 (기본)
        """
        settings = watcher.get("settings", {})
        open_mode = settings.get("open_mode", "web")
        wtype = watcher.get("type", "webpage")

        if open_mode == "none":
            return None

        buttons = []

        # 웹 URL 결정
        if wtype == "cgv":
            web_url = settings.get(
                "web_url",
                f"https://cgv.co.kr/mem/login?returnUrl=%2Ftme%2FtmeShowMore"
            )
            app_url = settings.get("app_url", CGV_APP_DEEPLINK)
            app_store_url = CGV_APP_STORE_IOS
        else:
            web_url = link or settings.get("url", "")
            app_url = settings.get("app_url", "")
            app_store_url = settings.get("app_store_url", "")

        row = []
        if open_mode in ("web", "both") and web_url:
            row.append(
                InlineKeyboardButton("🌐 웹에서 열기", url=web_url)
            )
        if open_mode in ("app", "both") and app_url:
            # 앱 딥링크: Android intent:// 스킴은 텔레그램에서 직접 지원 안 됨
            # → 앱스토어 링크로 대체하거나 universal link 사용
            # iOS/Android 공용 universal link 우선, 없으면 앱스토어
            app_open_url = app_store_url if app_store_url else web_url
            row.append(
                InlineKeyboardButton("📱 앱에서 열기", url=app_open_url)
            )

        if row:
            buttons.append(row)
            return InlineKeyboardMarkup(buttons)
        return None

    # ── 단일 watcher 실행 ──────────────────────────────────────

    async def _run_single_watcher(
        self,
        watcher: dict,
        context: ContextTypes.DEFAULT_TYPE,
        send_alert: bool = True,
    ) -> dict:
        """
        단일 watcher를 실행합니다.

        Returns:
            {
                "name": str,
                "changed": bool,
                "msg": str | None,
                "keyboard": InlineKeyboardMarkup | None,
                "error": str | None,
            }
        """
        name = watcher["name"]
        wtype = watcher.get("type", "webpage")
        crawler = self._get_crawler(watcher)

        result_info = {
            "name": name,
            "changed": False,
            "msg": None,
            "keyboard": None,
            "error": None,
        }

        if not crawler:
            result_info["error"] = "크롤러 생성 실패"
            return result_info

        try:
            result = crawler.check()
        except Exception as e:
            logger.error(f"[{name}] 크롤링 실패: {e}")
            result_info["error"] = str(e)
            return result_info

        raw_data = result.get("raw_data", "")
        if not raw_data:
            logger.debug(f"[{name}] 데이터 없음")
            return result_info

        # 변경 감지
        if not self.state_mgr.has_changed(name, raw_data):
            logger.debug(f"[{name}] 변경 없음")
            return result_info

        logger.info(f"[{name}] 변경 감지!")
        result_info["changed"] = True

        # 알림 메시지 생성
        link = ""
        if wtype == "cgv":
            schedules = result.get("schedules", [])
            msg = crawler.format_message(schedules) if schedules else None
        elif wtype == "webpage":
            items = result.get("items", [])
            old_state = self.state_mgr.get_state(name)
            old_titles = set(old_state.get("titles", []))
            new_items = [i for i in items if i["title"] not in old_titles]
            if new_items:
                msg = crawler.format_message(name, new_items)
                link = new_items[0].get("link", "") if new_items else ""
            else:
                msg = None

            state = self.state_mgr.get_state(name)
            state["titles"] = [i["title"] for i in items]
            self.state_mgr.save_state(name, state)
        else:
            msg = None

        # 상태 업데이트
        self.state_mgr.update_hash(name, raw_data)

        if msg:
            result_info["msg"] = msg
            result_info["keyboard"] = self._build_keyboard(watcher, link)

            if send_alert:
                await self._send_alert(msg, context, result_info["keyboard"])

        return result_info

    async def _scheduled_check(self, context: ContextTypes.DEFAULT_TYPE):
        """스케줄러에 의해 호출되는 주기적 모니터링"""
        job_data = context.job.data or {}
        watcher = job_data.get("watcher")
        if not watcher:
            return

        name = watcher["name"]
        logger.info(f"[{name}] 주기적 모니터링 실행")
        await self._run_single_watcher(watcher, context, send_alert=True)

    # ── 명령어 핸들러 ──────────────────────────────────────────

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """봇 시작 및 채팅 ID 자동 등록"""
        chat_id = str(update.effective_chat.id)
        self.chat_ids.add(chat_id)
        logger.info(f"새 채팅 등록: {chat_id}")

        enabled_count = len([w for w in self.watchers_config if w.get("enabled", True)])

        welcome = (
            "🎬 *영화 동아리 알림 봇에 오신 것을 환영합니다\\!*\n\n"
            "예매 전쟁에서 동아리원 모두가 이길 수 있도록 🎟\n"
            "새 상영 일정과 영화제 공지를 실시간으로 알려드립니다\\.\n\n"
            f"📡 현재 모니터링 중: *{enabled_count}개* 항목\n"
            f"📌 내 채팅 ID: `{_esc(chat_id)}`\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📋 *명령어 목록*\n"
            "/check \\- 지금 즉시 전체 확인\n"
            "/status \\- 모니터링 상태 보기\n"
            "/add \\<url\\> \\- 내 알림 URL 추가\n"
            "/list \\- 내가 추가한 알림 목록\n"
            "/remove \\<번호\\> \\- 내 알림 제거\n"
            "/help \\- 도움말"
        )
        await update.message.reply_text(welcome, parse_mode=ParseMode.MARKDOWN_V2)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """현재 모니터링 상태 확인"""
        all_watchers = self._get_all_watchers()
        now_str = _esc(datetime.now().strftime("%Y\\-%m\\-%d %H:%M:%S"))

        lines = [
            "📊 *모니터링 현황*",
            f"⏰ {now_str}",
            f"👥 등록된 채팅: {len(self.chat_ids)}개",
            "",
            "━━━━━━━━━━━━━━━━━━━━",
        ]

        if not all_watchers:
            lines.append("활성화된 모니터링이 없습니다\\.")
        else:
            for w in all_watchers:
                name = w["name"]
                interval = w.get("interval_minutes", 5)
                wtype = w.get("type", "webpage")
                state = self.state_mgr.get_state(name)
                last_hash = state.get("hash", "")
                last_check = state.get("last_check", "")

                type_icon = "🎬" if wtype == "cgv" else "📢"
                status_icon = "🟢"

                interval_str = _esc(f"{interval}분")
                name_esc = _esc(name)
                hash_str = _esc(last_hash[:8] if last_hash else "미확인")
                check_str = _esc(last_check[:16] if last_check else "미확인")

                lines.append(
                    f"{status_icon} {type_icon} *{name_esc}*\n"
                    f"   ⏱ 간격: {interval_str} \\| 🔍 마지막: `{check_str}`\n"
                    f"   🔑 해시: `{hash_str}`"
                )

        await update.message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2
        )

    async def cmd_check(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """즉시 전체 모니터링 실행 — 예쁜 포맷팅 + 진행 상황 표시"""
        all_watchers = self._get_all_watchers()
        total = len(all_watchers)

        if total == 0:
            await update.message.reply_text("⚠️ 모니터링 중인 항목이 없습니다\\.", parse_mode=ParseMode.MARKDOWN_V2)
            return

        # 시작 메시지
        progress_msg = await update.message.reply_text(
            f"🔍 전체 모니터링 시작\\.\\.\\.\n"
            f"총 *{total}개* 항목을 확인합니다\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

        results = []
        for idx, watcher in enumerate(all_watchers, 1):
            name = watcher["name"]
            wtype = watcher.get("type", "webpage")
            type_icon = "🎬" if wtype == "cgv" else "📢"

            # 진행 상황 업데이트
            try:
                await progress_msg.edit_text(
                    f"🔍 확인 중\\.\\.\\. \\({idx}/{total}\\)\n"
                    f"{type_icon} *{_esc(name)}*",
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
            except Exception:
                pass

            info = await self._run_single_watcher(watcher, context, send_alert=True)
            results.append(info)

        # 결과 요약 메시지
        changed_items = [r for r in results if r["changed"]]
        error_items = [r for r in results if r["error"]]
        ok_items = [r for r in results if not r["changed"] and not r["error"]]

        summary_lines = [
            "━━━━━━━━━━━━━━━━━━━━",
            "✅ *전체 확인 완료\\!*",
            f"⏰ {_esc(datetime.now().strftime('%H:%M:%S'))}",
            "",
        ]

        if changed_items:
            summary_lines.append(f"🔔 *변경 감지: {len(changed_items)}건*")
            for r in changed_items:
                wtype = next(
                    (w.get("type") for w in all_watchers if w["name"] == r["name"]),
                    "webpage"
                )
                icon = "🎬" if wtype == "cgv" else "📢"
                summary_lines.append(f"  {icon} {_esc(r['name'])}")
            summary_lines.append("")

        if ok_items:
            summary_lines.append(f"✔️ *변경 없음: {len(ok_items)}건*")
            for r in ok_items:
                summary_lines.append(f"  • {_esc(r['name'])}")
            summary_lines.append("")

        if error_items:
            summary_lines.append(f"❌ *오류: {len(error_items)}건*")
            for r in error_items:
                summary_lines.append(f"  • {_esc(r['name'])}: {_esc(str(r['error'])[:50])}")

        try:
            await progress_msg.edit_text(
                "\n".join(summary_lines),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception:
            await update.message.reply_text(
                "\n".join(summary_lines),
                parse_mode=ParseMode.MARKDOWN_V2,
            )

    async def cmd_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """사용자가 직접 URL을 추가하여 모니터링"""
        chat_id = str(update.effective_chat.id)

        if not context.args:
            help_text = (
                "📌 *URL 모니터링 추가 방법*\n\n"
                "사용법: `/add <URL>`\n\n"
                "예시:\n"
                "`/add https://example.com/notice`\n\n"
                "추가된 URL은 30분마다 자동으로 확인하며,\n"
                "페이지 내용이 변경되면 알림을 보내드립니다\\.\n\n"
                "⚠️ 주의: 로그인이 필요한 페이지나\n"
                "JavaScript로만 로딩되는 페이지는 감지가 어려울 수 있습니다\\."
            )
            await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN_V2)
            return

        url = context.args[0].strip()
        if not url.startswith(("http://", "https://")):
            await update.message.reply_text(
                "❌ URL은 `http://` 또는 `https://`로 시작해야 합니다\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        # 이미 등록된 URL 확인
        user_watchers = self._user_watchers.get(chat_id, [])
        for w in user_watchers:
            if w.get("settings", {}).get("url") == url:
                await update.message.reply_text(
                    "⚠️ 이미 등록된 URL입니다\\.",
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
                return

        # 최대 5개 제한
        if len(user_watchers) >= 5:
            await update.message.reply_text(
                "⚠️ 최대 5개까지 추가할 수 있습니다\\.\n"
                "/remove 명령어로 기존 항목을 삭제해 주세요\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        # watcher 생성
        name = f"[{chat_id[:6]}] {url[:40]}"
        new_watcher = {
            "name": name,
            "type": "webpage",
            "interval_minutes": 30,
            "enabled": True,
            "settings": {
                "url": url,
                "selector": "body",
                "open_mode": "web",
            },
        }

        if chat_id not in self._user_watchers:
            self._user_watchers[chat_id] = []
        self._user_watchers[chat_id].append(new_watcher)
        self._save_user_watchers()

        # 스케줄러에 즉시 등록
        if self.app:
            self.app.job_queue.run_repeating(
                self._scheduled_check,
                interval=30 * 60,
                first=5,
                name=f"user_{chat_id}_{len(self._user_watchers[chat_id])}",
                data={"watcher": new_watcher},
            )

        await update.message.reply_text(
            f"✅ *모니터링 추가 완료\\!*\n\n"
            f"🔗 URL: `{_esc(url[:60])}`\n"
            f"⏱ 확인 주기: 30분마다\n\n"
            f"페이지 내용이 변경되면 알림을 보내드립니다\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    async def cmd_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """내가 추가한 모니터링 목록 확인"""
        chat_id = str(update.effective_chat.id)
        user_watchers = self._user_watchers.get(chat_id, [])

        if not user_watchers:
            await update.message.reply_text(
                "📭 추가한 모니터링이 없습니다\\.\n"
                "`/add <URL>`로 추가해 보세요\\!",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        lines = ["📋 *내가 추가한 모니터링 목록*\n"]
        for idx, w in enumerate(user_watchers, 1):
            url = w.get("settings", {}).get("url", "")
            lines.append(f"*{idx}\\.* `{_esc(url[:60])}`")

        lines.append("\n`/remove <번호>`로 삭제할 수 있습니다\\.")
        await update.message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2
        )

    async def cmd_remove(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """내가 추가한 모니터링 제거"""
        chat_id = str(update.effective_chat.id)
        user_watchers = self._user_watchers.get(chat_id, [])

        if not context.args:
            await update.message.reply_text(
                "사용법: `/remove <번호>`\n`/list`로 번호를 확인하세요\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        try:
            idx = int(context.args[0]) - 1
            if idx < 0 or idx >= len(user_watchers):
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                f"❌ 올바른 번호를 입력해 주세요 \\(1\\~{len(user_watchers)}\\)\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        removed = user_watchers.pop(idx)
        self._user_watchers[chat_id] = user_watchers
        self._save_user_watchers()

        url = removed.get("settings", {}).get("url", "")
        await update.message.reply_text(
            f"🗑 *제거 완료*\n`{_esc(url[:60])}`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """도움말"""
        help_text = (
            "📖 *영화 동아리 알림 봇 도움말*\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🎯 *기본 명령어*\n"
            "/start \\- 봇 시작 및 알림 등록\n"
            "/check \\- 지금 즉시 전체 확인\n"
            "/status \\- 모니터링 상태 보기\n"
            "/help \\- 이 도움말\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🔧 *내 알림 관리*\n"
            "/add \\<url\\> \\- URL 모니터링 추가\n"
            "/list \\- 내가 추가한 목록 보기\n"
            "/remove \\<번호\\> \\- 내 알림 제거\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🔔 *기본 알림 항목*\n"
            "🎟 CGV 특별관 \\(IMAX 등\\) 새 상영 일정\n"
            "📢 영화제 공지사항 새 글\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "💡 *알림 수신 시*\n"
            "버튼을 눌러 웹 또는 앱으로 바로 이동할 수 있습니다\\.\n\n"
            "📂 [GitHub](https://github.com/kimble125/movie\\-club\\-ticket\\-notifier)"
        )
        await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN_V2)

    # ── 봇 실행 ──────────────────────────────────────────────

    async def _post_init(self, application: Application):
        """봇 초기화 후 명령어 목록 설정 및 스케줄러 등록"""
        commands = [
            BotCommand("start", "봇 시작 및 알림 등록"),
            BotCommand("check", "즉시 전체 확인"),
            BotCommand("status", "모니터링 상태 확인"),
            BotCommand("add", "URL 모니터링 추가"),
            BotCommand("list", "내 알림 목록"),
            BotCommand("remove", "내 알림 제거"),
            BotCommand("help", "도움말"),
        ]
        await application.bot.set_my_commands(commands)

        job_queue = application.job_queue
        enabled_watchers = [w for w in self.watchers_config if w.get("enabled", True)]

        for watcher in enabled_watchers:
            interval = watcher.get("interval_minutes", 5) * 60
            name = watcher["name"]
            job_queue.run_repeating(
                self._scheduled_check,
                interval=interval,
                first=10,
                name=f"watch_{name}",
                data={"watcher": watcher},
            )
            logger.info(f"스케줄 등록: [{name}] 매 {watcher.get('interval_minutes', 5)}분")

        # 사용자 추가 watcher 스케줄 등록
        for chat_id, watchers in self._user_watchers.items():
            for idx, watcher in enumerate(watchers):
                interval = watcher.get("interval_minutes", 30) * 60
                job_queue.run_repeating(
                    self._scheduled_check,
                    interval=interval,
                    first=15,
                    name=f"user_{chat_id}_{idx}",
                    data={"watcher": watcher},
                )



    def run(self):
        """봇을 실행합니다."""
        logger.info("텔레그램 봇 시작 중...")

        self.app = (
            Application.builder()
            .token(self.token)
            .post_init(self._post_init)
            .build()
        )

        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("status", self.cmd_status))
        self.app.add_handler(CommandHandler("check", self.cmd_check))
        self.app.add_handler(CommandHandler("add", self.cmd_add))
        self.app.add_handler(CommandHandler("list", self.cmd_list))
        self.app.add_handler(CommandHandler("remove", self.cmd_remove))
        self.app.add_handler(CommandHandler("help", self.cmd_help))

        logger.info("봇이 실행 중입니다. Ctrl+C로 종료합니다.")
        self.app.run_polling(drop_pending_updates=True)


def _esc(text: str) -> str:
    """Telegram MarkdownV2 이스케이프"""
    for ch in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text
