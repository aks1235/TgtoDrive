#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
字幕搜索下载核心服务模块

功能：
  1. SubtitleSearcher: 多源字幕搜索（OpenSubtitles API + SubHD/Zimuku 爬虫 fallback）
  2. DirectoryResolver: 115 网盘已整理目录遍历与关键词过滤
  3. SubtitleDownloader: 字幕文件下载与临时存储
  4. FileUploader: 字幕上传到 115 网盘对应视频目录

环境变量依赖（复用 Docker 镜像现有配置）：
  - ENV_115_COOKIES: 115 网盘登录 Cookie
  - ENV_115_ORGANIZE_TARGET_PID: 115 整理后目标目录 CID
  - ENV_AI_MEDIA_PARSER_API_KEY: 复用为 OpenSubtitles API Key（用户已在 AI 辅助识别中配置过）
  - ENV_SUBTITLE_OPENSUB_API_KEY: 可选，覆盖 OpenSubtitles API Key（如果和 AI 识别用的不同）
"""

import logging
import os
import re
import tempfile
import time
import zipfile
import shutil
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from p115client.client import P115Client

# 加载 Docker 镜像内的环境变量
load_dotenv(dotenv_path="db/user.env", override=True)
load_dotenv(dotenv_path="sys.env", override=True)

logger = logging.getLogger(__name__)

# ============================================================
# 配置常量
# ============================================================

# OpenSubtitles
OPENSUB_API_BASE = "https://api.opensubtitles.com/api/v1"
OPENSUB_API_KEY = os.getenv(
    "ENV_SUBTITLE_OPENSUB_API_KEY",
    os.getenv("ENV_AI_MEDIA_PARSER_API_KEY", ""),
)
# 免费层限制：20下载/天 + 40搜索/天
OPENSUB_MAX_SEARCH_RESULTS = 10

# 115 网盘
COOKIES_115 = os.getenv("ENV_115_COOKIES", "")
TARGET_PID = int(os.getenv("ENV_115_ORGANIZE_TARGET_PID", "0"))

# 字幕格式映射
SUBTITLE_EXT_MAP = {
    "srt": ".srt",
    "ass": ".ass",
    "ssa": ".ssa",
    "vtt": ".vtt",
    "sub": ".sub",
}

# 语言后缀映射（Emby/Jellyfin 识别标准）
LANG_SUFFIX_MAP = {
    "zh": ".zh",        # 简体中文
    "zh-cn": ".zh-cn",  # 简体中文（中国大陆）
    "zh-tw": ".zh-tw",  # 繁体中文（台湾）
    "chi": ".chi",      # 中文（通用）
    "zho": ".zho",      # 中文（ISO 639-2）
    "en": ".en",        # 英文
    "dual": ".zh-en",   # 中英双语
}

# HTTP 请求配置
HTTP_TIMEOUT = 15
HTTP_RETRIES = 2

# 115 目录缓存
_DIR_CACHE: Optional[list[dict]] = None
_DIR_CACHE_TIME: float = 0.0
_DIR_CACHE_TTL: float = 300.0  # 5分钟缓存


# ============================================================
# 数据结构
# ============================================================

@dataclass
class SubtitleResult:
    """字幕搜索结果"""
    source: str            # "opensubtitles" | "subhd" | "zimuku"
    title: str             # 显示标题
    language: str          # 语言标签 (zh, en, zh-en ...)
    file_format: str       # srt | ass | ssa | sub | vtt
    download_url: str      # 下载链接（如果有）
    file_id: str           # OpenSubtitles file_id 或爬虫页面 ID
    rating: float = 0.0    # 评分 (OpenSubtitles)
    size: str = ""         # 文件大小
    uploader: str = ""     # 上传者/字幕组


@dataclass
class DirectoryEntry:
    """115 网盘目录条目"""
    name: str              # 目录名（如 "让子弹飞 (2010)"）
    path: str              # 完整路径（如 "电影/国产/让子弹飞 (2010)"）
    cid: int               # 115 目录 CID
    parent_cid: int        # 父目录 CID
    video_files: list[str] = field(default_factory=list)  # 目录内视频文件名


# ============================================================
# SubtitleSearcher — 多源字幕搜索
# ============================================================

class SubtitleSearcher:
    """多源字幕搜索引擎"""

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or OPENSUB_API_KEY
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })

    def search(self, query: str, year: str = "", is_tv: bool = False) -> list[SubtitleResult]:
        """
        多源搜索字幕，OpenSubtitles 优先，SubHD/Zimuku 为 fallback。

        参数:
            query: 搜索关键词（电影/剧集名）
            year: 年份，用于精确匹配
            is_tv: 是否是剧集

        返回:
            SubtitleResult 列表，按评分降序排列
        """
        results: list[SubtitleResult] = []

        # 主源：OpenSubtitles API
        if self.api_key:
            try:
                opensub_results = self._search_opensubtitles(query, year, is_tv)
                results.extend(opensub_results)
                logger.info(f"[OpenSubtitles] 找到 {len(opensub_results)} 个字幕")
            except Exception as e:
                logger.warning(f"[OpenSubtitles] 搜索失败: {e}，fallback 到爬虫")

        # Fallback：SubHD
        if len(results) < 5:
            try:
                subhd_results = self._search_subhd(query)
                results.extend(subhd_results)
                logger.info(f"[SubHD] 找到 {len(subhd_results)} 个字幕")
            except Exception as e:
                logger.warning(f"[SubHD] 搜索失败: {e}")

        # Fallback：Zimuku
        if len(results) < 3:
            try:
                zimuku_results = self._search_zimuku(query)
                results.extend(zimuku_results)
                logger.info(f"[Zimuku] 找到 {len(zimuku_results)} 个字幕")
            except Exception as e:
                logger.warning(f"[Zimuku] 搜索失败: {e}")

        # 按评分降序
        results.sort(key=lambda r: r.rating, reverse=True)
        return results

    def _search_opensubtitles(
        self, query: str, year: str = "", is_tv: bool = False
    ) -> list[SubtitleResult]:
        """通过 OpenSubtitles REST API 搜索字幕"""
        headers = {"Api-Key": self.api_key, "Content-Type": "application/json"}

        # 搜索参数
        params: dict = {
            "query": query,
            "languages": "zh,zh-cn,zh-tw,en,zho",
            "moviehash": "",
            "order_by": "download_count",
            "page": 1,
        }
        if year:
            params["year"] = year

        resp = self.session.get(
            f"{OPENSUB_API_BASE}/subtitles",
            headers=headers,
            params=params,
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        results: list[SubtitleResult] = []
        for item in data.get("data", [])[:OPENSUB_MAX_SEARCH_RESULTS]:
            attrs = item.get("attributes", {})
            lang = attrs.get("language", "zh")
            fmt = attrs.get("format", "srt").lower()

            # 判断是否双语
            if attrs.get("hearing_impaired", False):
                lang_label = f"{lang}-hi"
            elif lang in ("zh", "zh-cn", "zh-tw", "zho"):
                lang_label = lang
            else:
                lang_label = f"{lang}"

            results.append(SubtitleResult(
                source="opensubtitles",
                title=attrs.get("release", attrs.get("feature_details", {}).get("title", query)),
                language=lang_label,
                file_format=fmt,
                download_url="",  # 需通过 /download 端点获取
                file_id=str(item.get("id", "")),
                rating=float(attrs.get("ratings", 0) or 0),
                size=_format_bytes(int(attrs.get("filesize", 0) or 0)),
                uploader=attrs.get("uploader", {}).get("name", ""),
            ))

        return results

    def _search_subhd(self, query: str) -> list[SubtitleResult]:
        """爬取 SubHD 搜索结果"""
        search_url = f"https://subhd.tv/search/{quote(query)}"
        resp = self.session.get(search_url, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        results: list[SubtitleResult] = []
        # SubHD 搜索结果通常在 .search-result 容器中
        items = soup.select(".search-result-item, .subtitle-item, .list-item")
        if not items:
            # 备用选择器
            items = soup.select("a[href*='/d/']")

        for item in items[:10]:
            link = item.select_one("a[href*='/d/'], a[href*='/detail/']")
            if not link:
                continue
            href = link.get("href", "")
            title_text = link.get_text(strip=True)

            # 提取格式和语言标签
            meta = item.get_text()
            fmt = _guess_format_from_text(meta)
            lang = _guess_language_from_text(meta)

            results.append(SubtitleResult(
                source="subhd",
                title=title_text or f"SubHD-{query}",
                language=lang,
                file_format=fmt,
                download_url=f"https://subhd.tv{href}" if href.startswith("/") else href,
                file_id=href.split("/")[-1],
                rating=0.0,
            ))

        return results

    def _search_zimuku(self, query: str) -> list[SubtitleResult]:
        """爬取 Zimuku 搜索结果"""
        search_url = f"https://zimuku.org/search?q={quote(query)}"
        resp = self.session.get(search_url, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        results: list[SubtitleResult] = []
        # Zimuku 使用 dedecms 结构
        items = soup.select(".list-item, .sub-item, .search-item")
        if not items:
            items = soup.select("a[href*='/detail/'], a[href*='/subs/']")

        for item in items[:10]:
            link = item.select_one("a[href*='/detail/'], a[href*='/subs/']")
            if not link:
                continue
            href = link.get("href", "")
            title_text = link.get_text(strip=True)

            meta = item.get_text()
            fmt = _guess_format_from_text(meta)
            lang = _guess_language_from_text(meta)

            results.append(SubtitleResult(
                source="zimuku",
                title=title_text or f"Zimuku-{query}",
                language=lang,
                file_format=fmt,
                download_url=href if href.startswith("http") else f"https://zimuku.org{href}",
                file_id=href.split("/")[-1],
                rating=0.0,
            ))

        return results

    def get_download_url(self, result: SubtitleResult) -> str:
        """
        获取字幕文件的直接下载链接。

        对于 OpenSubtitles，需要通过 /download 端点获取一次性下载链接。
        对于 SubHD/Zimuku，需要进入详情页提取真实下载链接。
        """
        if result.source == "opensubtitles":
            return self._get_opensub_download(result.file_id)
        elif result.source == "subhd":
            return self._get_subhd_download(result.download_url)
        elif result.source == "zimuku":
            return self._get_zimuku_download(result.download_url)
        return result.download_url

    def _get_opensub_download(self, file_id: str) -> str:
        """OpenSubtitles 下载端点"""
        headers = {"Api-Key": self.api_key, "Content-Type": "application/json"}
        payload = {"file_id": int(file_id)}

        resp = self.session.post(
            f"{OPENSUB_API_BASE}/download",
            headers=headers,
            json=payload,
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("link", "")

    def _get_subhd_download(self, detail_url: str) -> str:
        """SubHD 详情页提取下载链接"""
        resp = self.session.get(detail_url, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # 找下载按钮
        dl_link = soup.select_one("a.download-btn, a[href*='download'], a[href*='/dl/']")
        if dl_link:
            href = dl_link.get("href", "")
            return href if href.startswith("http") else f"https://subhd.tv{href}"

        # 备用：找任何可能的下载链接
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if any(ext in href.lower() for ext in (".zip", ".rar", ".srt", ".ass", ".ssa")):
                return href if href.startswith("http") else f"https://subhd.tv{href}"

        return detail_url

    def _get_zimuku_download(self, detail_url: str) -> str:
        """Zimuku 详情页提取下载链接"""
        resp = self.session.get(detail_url, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        dl_link = soup.select_one("a[href*='down'], a[href*='/dl/'], a.download")
        if dl_link:
            href = dl_link.get("href", "")
            return href if href.startswith("http") else f"https://zimuku.org{href}"

        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if any(ext in href.lower() for ext in (".zip", ".rar", ".srt", ".ass")):
                return href if href.startswith("http") else f"https://zimuku.org{href}"

        return detail_url

    def download_subtitle(self, result: SubtitleResult, output_dir: str = "") -> str:
        """
        下载字幕文件到本地目录，返回本地文件路径。

        对 zip/rar 包自动解压提取 .srt/.ass 文件。
        """
        if not output_dir:
            output_dir = tempfile.mkdtemp(prefix="subtitle_")

        dl_url = self.get_download_url(result)
        if not dl_url:
            raise RuntimeError(f"无法获取 {result.source} 字幕下载链接")

        resp = self.session.get(dl_url, timeout=HTTP_TIMEOUT * 2)
        resp.raise_for_status()

        # 判断文件类型
        content_type = resp.headers.get("Content-Type", "")
        disposition = resp.headers.get("Content-Disposition", "")

        # 从 Content-Disposition 提取文件名
        filename_match = re.search(r'filename[^;=\n]*=((["\']).*?\2|[^;\n]*)', disposition)
        if filename_match:
            raw_name = filename_match.group(1).strip('"\'')
        else:
            # 从 URL 提取
            raw_name = dl_url.split("/")[-1].split("?")[0] or f"subtitle_{result.file_id}"

        filepath = os.path.join(output_dir, raw_name)
        with open(filepath, "wb") as f:
            f.write(resp.content)

        # 如果是压缩包，解压
        if raw_name.endswith((".zip", ".rar")):
            extracted = _extract_subtitle_from_archive(filepath, output_dir)
            if extracted:
                os.remove(filepath)  # 删除压缩包
                return extracted

        # 如果不是字幕文件格式，尝试改名
        ext = os.path.splitext(raw_name)[1].lower()
        if ext not in (".srt", ".ass", ".ssa", ".vtt", ".sub"):
            new_path = filepath + ".srt"
            os.rename(filepath, new_path)
            return new_path

        return filepath


# ============================================================
# DirectoryResolver — 115 目录遍历与过滤
# ============================================================

class DirectoryResolver:
    """115 网盘已整理目录解析器"""

    def __init__(self, cookies: str = "", target_pid: int = 0):
        self.cookies = cookies or COOKIES_115
        self.target_pid = target_pid or TARGET_PID
        self._client: Optional[P115Client] = None
        self._dir_cache: list[DirectoryEntry] = []
        self._cache_time: float = 0.0

    @property
    def client(self) -> P115Client:
        """懒加载 115 客户端"""
        if self._client is None:
            if not self.cookies:
                raise RuntimeError("115 Cookie 未配置 (ENV_115_COOKIES)")
            self._client = P115Client(cookies=self.cookies)
            self._client.user_info()  # 验证有效性
        return self._client

    def get_all_directories(self, force_refresh: bool = False) -> list[DirectoryEntry]:
        """
        递归获取整理目标目录下所有叶子目录（包含视频文件的目录）。
        结果会被缓存 5 分钟。
        """
        global _DIR_CACHE, _DIR_CACHE_TIME

        now = time.time()
        if not force_refresh and _DIR_CACHE and (now - _DIR_CACHE_TIME) < _DIR_CACHE_TTL:
            return _DIR_CACHE

        if self.target_pid <= 0:
            logger.warning("ENV_115_ORGANIZE_TARGET_PID 未配置或为 0")
            return []

        entries = self._walk_directory(self.target_pid, "")

        _DIR_CACHE = entries
        _DIR_CACHE_TIME = now

        logger.info(f"已扫描 115 整理目录：共 {len(entries)} 个叶子目录")
        return entries

    def _walk_directory(self, cid: int, path_prefix: str) -> list[DirectoryEntry]:
        """递归遍历目录树"""
        entries: list[DirectoryEntry] = []

        try:
            resp = self.client.fs_list(cid)
            files = resp.get("data", []) if isinstance(resp, dict) else resp

            if not files:
                return entries

            # 收集当前目录的视频文件
            video_files: list[str] = []
            subdirs: list[dict] = []

            for f in files:
                name = f.get("file_name", f.get("n", ""))
                is_dir = f.get("is_dir", f.get("is_folder", False))

                if is_dir:
                    subdirs.append(f)
                else:
                    ext = os.path.splitext(name)[1].lower()
                    if ext in (".mp4", ".mkv", ".avi", ".mov", ".rmvb", ".wmv", ".ts", ".m2ts", ".flv"):
                        video_files.append(name)

            # 如果有视频文件，当前目录是叶子目录
            if video_files:
                entries.append(DirectoryEntry(
                    name=os.path.basename(path_prefix) or os.path.basename(str(cid)),
                    path=path_prefix or str(cid),
                    cid=cid,
                    parent_cid=0,
                    video_files=video_files,
                ))

            # 递归子目录
            for sub in subdirs:
                sub_name = sub.get("file_name", sub.get("n", ""))
                sub_cid = sub.get("file_id", sub.get("fid", sub.get("cid", 0)))
                sub_cid = int(sub_cid) if sub_cid else 0
                sub_path = f"{path_prefix}/{sub_name}" if path_prefix else sub_name
                entries.extend(self._walk_directory(sub_cid, sub_path))

        except Exception as e:
            logger.warning(f"遍历目录失败 (cid={cid}, path={path_prefix}): {e}")

        return entries

    def filter_by_keyword(self, keyword: str) -> list[DirectoryEntry]:
        """
        按关键词过滤目录列表。
        匹配规则：目录名包含关键词（不区分大小写），或目录内视频文件名包含关键词。
        """
        all_dirs = self.get_all_directories()
        kw_lower = keyword.lower().strip()

        if not kw_lower:
            return all_dirs[:20]  # 无条件时返回前 20 个

        matched: list[DirectoryEntry] = []
        for entry in all_dirs:
            # 目录名匹配
            if kw_lower in entry.name.lower():
                matched.append(entry)
                continue
            # 视频文件名匹配
            for vf in entry.video_files:
                if kw_lower in vf.lower():
                    matched.append(entry)
                    break

        return matched[:20]  # 最多返回 20 个

    def find_by_name(self, name: str) -> Optional[DirectoryEntry]:
        """按目录名精确查找"""
        all_dirs = self.get_all_directories()
        name_lower = name.lower().strip()
        for entry in all_dirs:
            if entry.name.lower() == name_lower:
                return entry
        return None

    def get_first_video_filename(self, entry: DirectoryEntry) -> str:
        """
        获取目录中第一个视频文件名（不含扩展名），用于字幕命名。

        如有多个视频，按文件大小排序取最大的（通常是正片）。
        """
        if not entry.video_files:
            return entry.name
        # 简单去扩展名
        video = entry.video_files[0]
        return os.path.splitext(video)[0]


# ============================================================
# SubtitleDownloader — 字幕文件下载
# ============================================================

class SubtitleDownloader:
    """字幕文件下载管理器"""

    def __init__(self, searcher: Optional[SubtitleSearcher] = None):
        self.searcher = searcher or SubtitleSearcher()
        self._download_dir = tempfile.mkdtemp(prefix="tgto_subtitle_")

    def download(self, result: SubtitleResult) -> str:
        """下载单个字幕，返回本地文件路径"""
        return self.searcher.download_subtitle(result, self._download_dir)

    def cleanup(self):
        """清理临时下载目录"""
        if os.path.exists(self._download_dir):
            shutil.rmtree(self._download_dir, ignore_errors=True)


# ============================================================
# FileUploader — 上传 115 网盘
# ============================================================

class FileUploader:
    """115 网盘文件上传器"""

    def __init__(self, cookies: str = "", target_pid: int = 0):
        self.cookies = cookies or COOKIES_115
        self.target_pid = target_pid or TARGET_PID
        self._client: Optional[P115Client] = None

    @property
    def client(self) -> P115Client:
        if self._client is None:
            if not self.cookies:
                raise RuntimeError("115 Cookie 未配置 (ENV_115_COOKIES)")
            self._client = P115Client(cookies=self.cookies)
            self._client.user_info()
        return self._client

    def build_subtitle_filename(
        self,
        video_name: str,
        lang: str,
        fmt: str,
    ) -> str:
        """
        构建 Emby 兼容的字幕文件名。

        规则: 视频文件名.语言后缀.格式扩展名

        示例:
          - 让子弹飞.2010.1080p.zh.srt
          - 让子弹飞.2010.1080p.chi.ass
          - 让子弹飞.2010.1080p.zh-en.srt (双语)
        """
        ext = SUBTITLE_EXT_MAP.get(fmt, f".{fmt}")
        # 规范化语言标签
        lang_key = lang.lower().replace("_", "-")
        lang_suffix = LANG_SUFFIX_MAP.get(
            lang_key,
            LANG_SUFFIX_MAP.get(lang_key.split("-")[0], f".{lang_key}"),
        )
        return f"{video_name}{lang_suffix}{ext}"

    def upload_to_directory(
        self,
        local_file_path: str,
        directory_cid: int,
        target_filename: str,
    ) -> bool:
        """
        上传本地字幕文件到 115 指定目录。

        返回 True/False 表示成功/失败。
        """
        try:
            # 使用 p115client 的上传功能
            # p115client 支持 fs_upload 或可通过 os_upload 上传
            result = self.client.fs_upload(
                local_file_path,
                pid=directory_cid,
                filename=target_filename,
            )
            logger.info(f"字幕上传成功: {target_filename} → cid={directory_cid}")
            return True
        except Exception as e:
            logger.error(f"字幕上传失败: {target_filename} → cid={directory_cid}: {e}")
            return False


# ============================================================
# 辅助函数
# ============================================================

def _format_bytes(size_bytes: int) -> str:
    """字节数 -> 可读大小"""
    if size_bytes <= 0:
        return ""
    if size_bytes < 1024:
        return f"{size_bytes}B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    return f"{size_bytes / (1024 * 1024):.1f}MB"


def _guess_format_from_text(text: str) -> str:
    """从文本中推断字幕格式"""
    text_lower = text.lower()
    for fmt in ("ass", "ssa", "srt", "vtt", "sub"):
        if fmt in text_lower:
            return fmt
    return "srt"  # 默认


def _guess_language_from_text(text: str) -> str:
    """从文本中推断字幕语言"""
    text_lower = text.lower()
    if any(w in text_lower for w in ("中英", "双语", "简英", "繁英", "chs&eng", "cn&en")):
        return "dual"
    if any(w in text_lower for w in ("简体", "简中", "chs", "zh-cn", "cn", "中文", "国语")):
        return "zh"
    if any(w in text_lower for w in ("繁体", "繁中", "cht", "zh-tw", "tw")):
        return "zh-tw"
    if any(w in text_lower for w in ("英文", "英语", "english", "eng")):
        return "en"
    return "zh"  # 默认中文


def _extract_subtitle_from_archive(archive_path: str, output_dir: str) -> str:
    """
    从 zip/rar 压缩包中提取字幕文件。
    优先提取 .ass/.srt，过滤掉非字幕文件。
    返回提取到的第一个字幕文件的路径。
    """
    extracted_path = ""

    if archive_path.endswith(".zip"):
        try:
            with zipfile.ZipFile(archive_path, "r") as zf:
                # 按优先级排序：ass > srt > ssa > vtt > sub
                names = zf.namelist()
                subtitle_names = sorted(
                    [n for n in names if os.path.splitext(n)[1].lower() in (".ass", ".srt", ".ssa", ".vtt", ".sub")],
                    key=lambda n: {".ass": 0, ".srt": 1, ".ssa": 2, ".vtt": 3, ".sub": 4}.get(
                        os.path.splitext(n)[1].lower(), 99
                    ),
                )
                if subtitle_names:
                    best = subtitle_names[0]
                    zf.extract(best, output_dir)
                    extracted_path = os.path.join(output_dir, best)
        except Exception as e:
            logger.warning(f"解压 zip 失败: {archive_path}: {e}")

    elif archive_path.endswith(".rar"):
        # rar 需要外部工具 unrar，尝试调用
        import subprocess
        try:
            result = subprocess.run(
                ["unrar", "x", "-y", archive_path, output_dir],
                capture_output=True, timeout=30,
            )
            if result.returncode == 0:
                for root, _, files in os.walk(output_dir):
                    for f in files:
                        if os.path.splitext(f)[1].lower() in (".ass", ".srt", ".ssa", ".vtt", ".sub"):
                            candidate = os.path.join(root, f)
                            if not extracted_path:
                                extracted_path = candidate
                            elif os.path.splitext(candidate)[1].lower() == ".ass":
                                extracted_path = candidate  # .ass 优先
        except Exception as e:
            logger.warning(f"解压 rar 失败: {archive_path}: {e}")

    return extracted_path