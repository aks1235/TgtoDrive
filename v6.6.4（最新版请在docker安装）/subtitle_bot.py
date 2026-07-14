#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
字幕搜索下载 Telegram Bot

独立的 Telegram Bot，处理 /subtitle 命令，交互式查找字幕并上传到 115 网盘。

使用独立 Bot Token（与主 bot tgto123 不冲突）。

依赖（复用 Docker 镜像现有配置）：
  ENV_SUBTITLE_BOT_TOKEN — 字幕 Bot 的 Telegram Token（必填，新建一个 Bot 获取）
  ENV_115_COOKIES — 115 网盘登录 Cookie（与主 Bot 共用）
  ENV_115_ORGANIZE_TARGET_PID — 115 整理目标目录 CID
  ENV_SUBTITLE_OPENSUB_API_KEY — OpenSubtitles API Key（可选，复用 ENV_AI_MEDIA_PARSER_API_KEY）

交互流程：
  1. /subtitle 关键词 → 搜索 115 已整理目录（关键词过滤）
  2. Inline 按钮选择目录 → 提取目录名搜索字幕
  3. 返回字幕候选列表 → 用户选择
  4. 下载字幕文件 → 构建 Emby 兼容文件名 → 上传到 115 对应目录
"""

import logging
import os
import re
import threading
from typing import Optional

import telebot
from telebot.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from dotenv import load_dotenv

from subtitle_service import (
    SubtitleSearcher,
    DirectoryResolver,
    SubtitleDownloader,
    FileUploader,
    SubtitleResult,
    DirectoryEntry,
)

# 加载 Docker 镜像内的环境变量
load_dotenv(dotenv_path="db/user.env", override=True)
load_dotenv(dotenv_path="sys.env", override=True)

logger = logging.getLogger(__name__)

# ============================================================
# 配置
# ============================================================

SUBTITLE_BOT_TOKEN = os.getenv("ENV_SUBTITLE_BOT_TOKEN", "")

DIRS_PER_PAGE = 10       # 目录列表每页显示数
RESULTS_PER_PAGE = 8     # 字幕候选每页显示数

# ============================================================
# 用户会话管理
# ============================================================

class UserSession:
    """单个用户的交互状态（内存存储，重启丢失）"""

    def __init__(self):
        self.state: str = "idle"
        self.keyword: str = ""
        self.directories: list[DirectoryEntry] = []
        self.selected_dir: Optional[DirectoryEntry] = None
        self.subtitle_results: list[SubtitleResult] = []
        self.page: int = 0


_user_sessions: dict[int, UserSession] = {}
_sessions_lock = threading.Lock()


def get_session(chat_id: int) -> UserSession:
    """获取或创建用户会话"""
    with _sessions_lock:
        if chat_id not in _user_sessions:
            _user_sessions[chat_id] = UserSession()
        return _user_sessions[chat_id]


# ============================================================
# 服务单例（懒加载，启动时不校验 115 Cookie）
# ============================================================

_searcher: Optional[SubtitleSearcher] = None
_resolver: Optional[DirectoryResolver] = None
_downloader: Optional[SubtitleDownloader] = None
_uploader: Optional[FileUploader] = None


def _get_searcher() -> SubtitleSearcher:
    global _searcher
    if _searcher is None:
        _searcher = SubtitleSearcher()
    return _searcher


def _get_resolver() -> DirectoryResolver:
    global _resolver
    if _resolver is None:
        _resolver = DirectoryResolver()
    return _resolver


def _get_downloader() -> SubtitleDownloader:
    global _downloader
    if _downloader is None:
        _downloader = SubtitleDownloader(_get_searcher())
    return _downloader


def _get_uploader() -> FileUploader:
    global _uploader
    if _uploader is None:
        _uploader = FileUploader()
    return _uploader


# ============================================================
# Bot 初始化
# ============================================================

bot = telebot.TeleBot(SUBTITLE_BOT_TOKEN, threaded=True, num_threads=4)


# ============================================================
# /start 命令
# ============================================================

@bot.message_handler(commands=["start"])
def handle_start(message):
    chat_id = message.chat.id
    session = get_session(chat_id)
    session.state = "idle"

    bot.send_message(
        chat_id,
        (
            "🎬 <b>字幕搜索下载 Bot</b>\n\n"
            "使用方法：\n"
            "<code>/subtitle 关键词</code> — 搜索匹配的影视目录并下载字幕\n"
            "<code>/subtitle list</code> — 列出最近整理的目录\n\n"
            "示例：\n"
            "  <code>/subtitle 子弹</code>\n"
            "  <code>/subtitle 流浪地球</code>\n\n"
            "支持 OpenSubtitles / SubHD / Zimuku 多源搜索\n"
            "自动下载并上传到 115 网盘对应目录"
        ),
        parse_mode="HTML",
    )


# ============================================================
# /subtitle 命令
# ============================================================

@bot.message_handler(commands=["subtitle"])
def handle_subtitle(message):
    chat_id = message.chat.id
    session = get_session(chat_id)

    text = message.text.strip()
    cmd_match = re.match(r"/subtitle\s*(.*)", text, re.IGNORECASE)
    keyword = (cmd_match.group(1) or "").strip()

    if not keyword:
        bot.send_message(
            chat_id,
            (
                "请输入搜索关键词，例如：\n"
                "<code>/subtitle 子弹</code>\n\n"
                "或输入 <code>/subtitle list</code> 查看最近整理的目录"
            ),
            parse_mode="HTML",
        )
        return

    if keyword.lower() == "list":
        keyword = ""

    session.state = "selecting_dir"
    session.keyword = keyword
    session.page = 0
    session.selected_dir = None
    session.subtitle_results = []

    status_msg = bot.send_message(chat_id, "🔍 正在扫描 115 网盘目录...")

    try:
        resolver = _get_resolver()
        dirs = resolver.filter_by_keyword(keyword) if keyword else resolver.get_all_directories()[:30]
        session.directories = dirs

        if not dirs:
            bot.edit_message_text(
                (
                    f"❌ 未找到匹配「<b>{keyword}</b>」的目录\n\n"
                    "请检查：\n"
                    "1. 115 整理功能是否已运行过\n"
                    "2. 关键词拼写是否正确\n"
                    "3. ENV_115_ORGANIZE_TARGET_PID 是否已配置"
                ),
                chat_id, status_msg.message_id, parse_mode="HTML",
            )
            session.state = "idle"
            return

        _render_directory_page(chat_id, session, status_msg.message_id)

    except Exception as e:
        logger.error(f"目录扫描失败: {e}")
        bot.edit_message_text(
            f"❌ 目录扫描失败: {str(e)[:200]}\n\n请检查 115 Cookie 和网络连接",
            chat_id, status_msg.message_id,
        )
        session.state = "idle"


# ============================================================
# 目录列表渲染（Inline Keyboard 分页）
# ============================================================

def _render_directory_page(chat_id: int, session: UserSession, msg_id: int = 0):
    dirs = session.directories
    total = len(dirs)
    start = session.page * DIRS_PER_PAGE
    end = min(start + DIRS_PER_PAGE, total)
    page_dirs = dirs[start:end]

    lines = [
        f"🔍 搜索「<b>{session.keyword or '全部'}</b>」",
        f"找到 <b>{total}</b> 个匹配目录 (第{session.page + 1}页)\n",
    ]
    keyboard = InlineKeyboardMarkup(row_width=1)

    for i, d in enumerate(page_dirs):
        idx = start + i + 1
        label = f"{idx}. {d.name}"
        if len(label) > 60:
            label = label[:57] + "..."
        path_info = f"  [{d.path}]" if d.path != d.name else ""
        lines.append(f"{idx}. {d.name}{path_info}")
        keyboard.add(InlineKeyboardButton(label, callback_data=f"dir_{idx}"))

    nav_buttons = []
    if session.page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ 上一页", callback_data="dir_prev"))
    if end < total:
        nav_buttons.append(InlineKeyboardButton("▶️ 下一页", callback_data="dir_next"))
    if nav_buttons:
        keyboard.add(*nav_buttons)
    keyboard.add(InlineKeyboardButton("❌ 取消", callback_data="dir_cancel"))

    if not session.keyword:
        lines.append("\n💡 提示：使用 <code>/subtitle 关键词</code> 可以过滤目录")

    text = "\n".join(lines)

    if msg_id:
        try:
            bot.edit_message_text(text, chat_id, msg_id, reply_markup=keyboard, parse_mode="HTML")
        except Exception:
            bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="HTML")
    else:
        bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="HTML")


# ============================================================
# 目录选择回调处理
# ============================================================

@bot.callback_query_handler(func=lambda call: call.data.startswith("dir_"))
def handle_directory_callback(call):
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    session = get_session(chat_id)

    data = call.data

    if data == "dir_cancel":
        session.state = "idle"
        session.directories = []
        session.subtitle_results = []
        bot.edit_message_text("❌ 已取消", chat_id, msg_id)
        return

    if data == "dir_prev":
        session.page = max(0, session.page - 1)
        _render_directory_page(chat_id, session, msg_id)
        return

    if data == "dir_next":
        max_page = max(0, (len(session.directories) - 1) // DIRS_PER_PAGE)
        session.page = min(max_page, session.page + 1)
        _render_directory_page(chat_id, session, msg_id)
        return

    # 具体目录选择
    try:
        idx = int(data.split("_")[1]) - 1
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "无效选择")
        return

    if idx < 0 or idx >= len(session.directories):
        bot.answer_callback_query(call.id, "无效选择")
        return

    selected = session.directories[idx]
    session.selected_dir = selected
    session.state = "selecting_sub"

    clean_name = _clean_folder_name(selected.name)

    bot.edit_message_text(
        (
            f"✅ 已选择: <b>{selected.name}</b>\n"
            f"📁 路径: {selected.path}\n"
            f"🎬 视频: {', '.join(selected.video_files[:3])}"
            f"{'...' if len(selected.video_files) > 3 else ''}\n\n"
            f"🔍 正在搜索字幕 «{clean_name}» ..."
        ),
        chat_id, msg_id, parse_mode="HTML",
    )

    try:
        results = _get_searcher().search(clean_name)
        session.subtitle_results = results

        if not results:
            bot.send_message(
                chat_id,
                (
                    f"❌ 未找到「<b>{clean_name}</b>」的字幕\n\n"
                    "建议：\n"
                    "• 尝试英文名或其他关键词\n"
                    "• 检查 OpenSubtitles API Key 是否配置\n"
                    "• 手动到 SubHD / Zimuku 搜索"
                ),
                parse_mode="HTML",
            )
            session.state = "idle"
            return

        _render_subtitle_results(chat_id, session)

    except Exception as e:
        logger.error(f"字幕搜索失败: {e}")
        bot.send_message(chat_id, f"❌ 字幕搜索失败: {str(e)[:200]}")
        session.state = "idle"


def _clean_folder_name(name: str) -> str:
    """清理目录名为搜索关键词（去掉年份/TMDB ID/格式标记）"""
    cleaned = re.sub(r"\s*[{\[].*?[}\]]\s*", "", name)
    cleaned = re.sub(r"\s*\(\d{4}\)\s*", "", cleaned)
    cleaned = re.sub(r"\s*\d{3,4}[piPI]\s*", "", cleaned)
    cleaned = re.sub(r"\s*(BluRay|WEB-DL|BRRip|HDRip|REMUX|DVDRip)\s*", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


# ============================================================
# 字幕候选列表渲染
# ============================================================

SOURCE_LABELS = {
    "opensubtitles": "📡 OpenSubtitles",
    "subhd": "🌐 SubHD",
    "zimuku": "🌐 Zimuku",
}

LANG_LABELS = {
    "zh": "中文", "zh-cn": "简体", "zh-tw": "繁体",
    "zh-en": "中英双语", "dual": "中英双语",
    "en": "英文", "zho": "中文",
}


def _render_subtitle_results(chat_id: int, session: UserSession):
    results = session.subtitle_results
    total = len(results)

    lines = [
        f"🔍 搜索「<b>{session.selected_dir.name}</b>」的字幕",
        f"找到 <b>{total}</b> 个结果:\n",
    ]
    keyboard = InlineKeyboardMarkup(row_width=1)

    for i, r in enumerate(results[:RESULTS_PER_PAGE]):
        idx = i + 1
        source_label = SOURCE_LABELS.get(r.source, r.source)
        lang_display = LANG_LABELS.get(r.language, r.language)
        star = f" ⭐{r.rating:.1f}" if r.rating > 0 else ""
        size_info = f" [{r.size}]" if r.size else ""

        lines.append(
            f"{idx}. {r.title[:60]}{star}\n"
            f"   [{source_label}] {lang_display} | {r.file_format.upper()}{size_info}"
        )

        btn_label = f"{idx}. [{r.file_format.upper()}] {r.title[:40]}"
        if r.rating > 0:
            btn_label += f" ⭐{r.rating:.1f}"
        if len(btn_label) > 64:
            btn_label = btn_label[:61] + "..."
        keyboard.add(InlineKeyboardButton(btn_label, callback_data=f"sub_{idx}"))

    keyboard.add(InlineKeyboardButton("❌ 取消", callback_data="sub_cancel"))

    if total > RESULTS_PER_PAGE:
        lines.append(f"\n... 还有 {total - RESULTS_PER_PAGE} 个结果未显示")

    bot.send_message(chat_id, "\n".join(lines), reply_markup=keyboard, parse_mode="HTML")


# ============================================================
# 字幕选择回调处理
# ============================================================

@bot.callback_query_handler(func=lambda call: call.data.startswith("sub_"))
def handle_subtitle_callback(call):
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    session = get_session(chat_id)

    data = call.data

    if data == "sub_cancel":
        session.state = "idle"
        session.subtitle_results = []
        session.selected_dir = None
        bot.edit_message_text("❌ 已取消", chat_id, msg_id)
        return

    try:
        idx = int(data.split("_")[1]) - 1
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "无效选择")
        return

    if idx < 0 or idx >= len(session.subtitle_results):
        bot.answer_callback_query(call.id, "无效选择")
        return

    selected = session.subtitle_results[idx]
    directory = session.selected_dir

    if not directory:
        bot.edit_message_text("❌ 会话状态丢失，请重新发送 /subtitle", chat_id, msg_id)
        session.state = "idle"
        return

    # 更新进度
    try:
        bot.edit_message_text(
            f"⬇️ 正在下载字幕...\n📄 {selected.title[:60]}\n📡 来源: {selected.source}",
            chat_id, msg_id,
        )
    except Exception:
        pass

    # 下载字幕
    try:
        local_path = _get_downloader().download(selected)
    except Exception as e:
        logger.error(f"字幕下载失败: {e}")
        bot.send_message(
            chat_id,
            f"❌ 字幕下载失败: {str(e)[:200]}\n\n可能是下载链接失效，请尝试其他候选",
        )
        session.state = "idle"
        return

    # 构建 Emby 兼容文件名
    video_name = _get_resolver().get_first_video_filename(directory)
    uploader = _get_uploader()
    subtitle_filename = uploader.build_subtitle_filename(
        video_name, selected.language, selected.file_format,
    )

    # 上传到 115
    upload_success = False
    try:
        upload_success = uploader.upload_to_directory(local_path, directory.cid, subtitle_filename)
    except Exception as e:
        logger.error(f"115 上传失败: {e}")

    if upload_success:
        bot.send_message(
            chat_id,
            (
                f"✅ <b>字幕已上传!</b>\n\n"
                f"📄 文件: <code>{subtitle_filename}</code>\n"
                f"📁 目录: {directory.path}\n"
                f"📡 来源: {selected.source}\n"
                f"🌐 语言: {selected.language}\n"
                f"📎 格式: {selected.file_format.upper()}\n\n"
                "Emby 下次扫描时会自动识别该字幕文件。"
            ),
            parse_mode="HTML",
        )
    else:
        try:
            with open(local_path, "rb") as f:
                bot.send_document(
                    chat_id,
                    f,
                    caption=(
                        f"⚠️ 115 上传失败，字幕文件直接发送给你\n"
                        f"📄 建议命名: {subtitle_filename}\n"
                        f"📁 目标目录: {directory.path}"
                    ),
                )
        except Exception as e2:
            logger.error(f"发送文件也失败: {e2}")
            bot.send_message(
                chat_id,
                f"❌ 上传和发送都失败了\n下载链接: {selected.download_url}\n请手动下载",
            )

    session.state = "idle"
    session.subtitle_results = []
    session.selected_dir = None

    try:
        os.remove(local_path)
    except Exception:
        pass


# ============================================================
# /help 命令
# ============================================================

@bot.message_handler(commands=["help"])
def handle_help(message):
    bot.send_message(
        message.chat.id,
        (
            "🎬 <b>字幕搜索下载 Bot 帮助</b>\n\n"
            "<b>命令：</b>\n"
            "<code>/subtitle 关键词</code> — 搜索匹配目录并下载字幕\n"
            "<code>/subtitle list</code> — 列出所有已整理目录\n"
            "<code>/cancel</code> — 取消当前操作\n\n"
            "<b>使用流程：</b>\n"
            "1. 发送 <code>/subtitle 电影名</code>\n"
            "2. 在目录列表中选择对应的影视目录\n"
            "3. 在字幕候选列表中选择要下载的字幕\n"
            "4. Bot 自动下载并上传到 115\n\n"
            "<b>配置要求：</b>\n"
            "• 115 Cookie (ENV_115_COOKIES)\n"
            "• 整理目标目录 (ENV_115_ORGANIZE_TARGET_PID)\n"
            "• 推荐 OpenSubtitles API Key (ENV_SUBTITLE_OPENSUB_API_KEY)\n\n"
            "<b>字幕源：</b>\n"
            "📡 OpenSubtitles API（主源）\n"
            "🌐 SubHD（中文源）\n"
            "🌐 Zimuku（中文源）"
        ),
        parse_mode="HTML",
    )


# ============================================================
# /cancel 命令
# ============================================================

@bot.message_handler(commands=["cancel"])
def handle_cancel(message):
    chat_id = message.chat.id
    session = get_session(chat_id)
    session.state = "idle"
    session.directories = []
    session.subtitle_results = []
    session.selected_dir = None
    bot.send_message(chat_id, "✅ 已取消当前操作")


# ============================================================
# Bot 启动
# ============================================================

def set_bot_commands():
    """注册 Bot 命令菜单"""
    try:
        bot.set_my_commands([
            BotCommand("subtitle", "搜索字幕并下载到115网盘"),
            BotCommand("help", "使用帮助"),
            BotCommand("cancel", "取消当前操作"),
        ])
        logger.info("字幕 Bot 命令菜单已设置")
    except Exception as e:
        logger.warning(f"设置命令菜单失败: {e}")


def start_bot():
    """启动字幕 Bot polling"""
    if not SUBTITLE_BOT_TOKEN:
        logger.error("ENV_SUBTITLE_BOT_TOKEN 未配置，字幕 Bot 无法启动")
        return

    logger.info("🎬 字幕搜索下载 Bot 启动中...")
    set_bot_commands()

    try:
        bot.infinity_polling(timeout=30, long_polling_timeout=60)
    except Exception as e:
        logger.error(f"字幕 Bot 异常退出: {e}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    start_bot()