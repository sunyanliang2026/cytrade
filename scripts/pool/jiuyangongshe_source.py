from __future__ import annotations

import html
import re
from collections.abc import Iterable
from datetime import datetime
from typing import Optional

from scripts.pool.common import (
    DEFAULT_SECTOR,
    JIUYANGONGSHE_HOST,
    PoolCandidate,
    is_main_board_code,
    normalize_stock_code,
)
from scripts.pool.qmt_source import xt_name


STOCK_TOKEN_RE = re.compile(r"[\u4e00-\u9fffA-Za-z*]{2,12}(?:\s*A)?")
CHINESE_STOCK_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]{2,8}(?:\s*A)?")
STOP_STOCK_TOKENS = {
    "SpaceX",
    "Nebius",
    "Rubin",
    "Optimus",
    "Gen",
    "IBM",
    "D-Wave",
    "Quantum",
    "Infleqtion",
    "Rigetti",
    "Computing",
    "No",
    "事件",
    "日常公告",
    "公告精选",
    "盘前热点事件",
    "涨停事件",
    "连板梯队",
    "昨日热点",
    "行业要闻",
    "商业航天并购",
    "自动驾驶",
    "玻璃基板",
    "机器人",
    "摘帽",
    "量子科技",
    "存储芯片",
    "算力租赁",
    "金刚石",
    "公告涨停",
    "控股股东",
    "董事长增持",
    "公司取得",
    "拟收购",
    "拟发行",
    "拟出资",
    "复牌",
    "停牌",
}

JIUYANGONGSHE_NODE_LABELS = {
    "hot_events": "No.1 盘前热点事件",
    "daily_announcements": "No.2 公告精选->一、日常公告",
    "limit_events": "No.4 连板梯队和涨停事件->三、涨停事件",
}


def fetch_text(url: str, timeout: int = 20) -> str:
    from urllib.request import Request, urlopen

    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
            )
        },
    )
    with urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return raw.decode("utf-8", errors="replace")


def extract_latest_jiuyangongshe_article(page: str) -> tuple[str, str]:
    href_match = re.search(r'href="(/a/[0-9A-Za-z]+)"', page)
    if not href_match:
        href_match = re.search(r'canonical"\s+href="(https://www\.jiuyangongshe\.com/a/[0-9A-Za-z]+)"', page)
    if not href_match:
        return "", ""
    href = href_match.group(1)
    before_href = page[max(0, href_match.start() - 2000) : href_match.start()]
    date_matches = re.findall(r'(\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}:\d{2}', before_href)
    article_date = date_matches[-1] if date_matches else ""
    return (href if href.startswith("http") else f"{JIUYANGONGSHE_HOST}{href}"), article_date


def resolve_latest_jiuyangongshe_article_url(user_url: str, *, require_today: bool = True) -> str:
    page = fetch_text(user_url)
    article_url, article_date = extract_latest_jiuyangongshe_article(page)
    if not article_url:
        raise RuntimeError(f"未能从用户页识别最新文章链接: {user_url}")
    today = datetime.now().strftime("%Y-%m-%d")
    if require_today and article_date != today:
        raise RuntimeError(f"韭研公社最新文章不是当天文章 article_date={article_date or 'unknown'} today={today} url={article_url}")
    return article_url


def extract_jiuyangongshe_article_html(page: str) -> str:
    start = page.find('content:"')
    if start < 0:
        return ""
    start += len('content:"')
    end = page.find('",url:', start)
    if end < 0:
        return ""
    raw = page[start:end]
    decoded = raw.encode("utf-8").decode("unicode_escape")
    return html.unescape(decoded)


def normalize_article_plain_text(article_html: str) -> str:
    plain = re.sub(r"<[^>]+>", "\n", article_html)
    plain = html.unescape(plain)
    if "忙" in plain or "茫" in plain:
        try:
            plain = plain.encode("latin1", errors="ignore").decode("utf-8", errors="ignore")
        except Exception:
            pass
    lines = [re.sub(r"\s+", " ", line).strip() for line in plain.splitlines()]
    lines = [line for line in lines if line and line != "&nbsp;"]
    text = "\n".join(lines)
    text = re.sub(r"京东方\s*\n\s*A", "京东方A", text)
    text = re.sub(r"南玻\s*\n\s*A", "南玻A", text)
    text = re.sub(r"\n+", "\n", text)
    return text


def slice_between(text: str, start_marker: str, end_marker: str = "") -> str:
    start = text.find(start_marker)
    if start < 0:
        return ""
    start += len(start_marker)
    if not end_marker:
        return text[start:]
    end = text.find(end_marker, start)
    if end < 0:
        return text[start:]
    return text[start:end]


def extract_target_sections(text: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    no1 = slice_between(text, "No.1", "No.2")
    if no1:
        sections.append(("No.1 盘前热点事件", no1))

    no2 = slice_between(text, "No.2", "No.3")
    daily = slice_between(no2, "一、日常", "二、")
    if not daily:
        daily = slice_between(no2, "一、日常公告", "二、")
    if daily:
        sections.append(("No.2 公告精选/一、日常公告", daily))

    no4 = slice_between(text, "No.4", "No.5")
    limit_events = slice_between(no4, "三、涨停事件", "")
    if limit_events:
        sections.append(("No.4 连板梯队和涨停事件/三、涨停事件", limit_events))
    return sections


def extract_jiuyangongshe_node_sections(text: str) -> dict[str, tuple[str, str]]:
    sections = extract_target_sections(text)
    mapping: dict[str, tuple[str, str]] = {}
    for index, section in enumerate(sections):
        if index == 0:
            mapping["hot_events"] = section
        elif index == 1:
            mapping["daily_announcements"] = section
        elif index == 2:
            mapping["limit_events"] = section
    return mapping


def clean_stock_token(value: str) -> str:
    token = re.sub(r"\s+", "", str(value or "").strip())
    token = token.strip("；、，,。）（()：:“”\"'")
    if token.endswith("A") and len(token) > 2:
        return token
    return token


def looks_like_stock_name(token: str, known_names: Optional[set[str]] = None) -> bool:
    token = clean_stock_token(token)
    if not token or token in STOP_STOCK_TOKENS:
        return False
    if known_names is not None:
        return token in known_names
    if len(token) < 2 or len(token) > 8:
        return False
    if re.search(r"^[一二三四五六七八九十]+$", token):
        return False
    if token.endswith(("事件", "公告", "股份事项", "动态更新")):
        return False
    return True


def extract_known_stock_names(section: str, known_names: set[str]) -> list[str]:
    matches: list[tuple[int, int, str]] = []
    for name in known_names:
        if not name:
            continue
        pattern = r"\s*".join(re.escape(char) for char in name)
        for match in re.finditer(pattern, section):
            matches.append((match.start(), -len(match.group(0)), name))

    names: list[str] = []
    seen: set[str] = set()
    accepted_spans: list[tuple[int, int]] = []
    for index, negative_length, name in sorted(matches):
        if name in seen:
            continue
        end = index - negative_length
        if any(index < accepted_end and end > accepted_start for accepted_start, accepted_end in accepted_spans):
            continue
        seen.add(name)
        accepted_spans.append((index, end))
        names.append(name)
    return names


def build_qmt_name_code_map(sector: str = DEFAULT_SECTOR) -> dict[str, str]:
    from xtquant import xtdata

    try:
        xtdata.download_sector_data()
    except Exception as exc:
        raise RuntimeError("韭研公社正式股票池需要连接 QMT/xtdata 以便把股票名称解析成代码。") from exc
    mapping: dict[str, str] = {}
    for xt_code in list(xtdata.get_stock_list_in_sector(sector) or []):
        code = normalize_stock_code(xt_code)
        if not code or not is_main_board_code(code):
            continue
        name = xt_name(xtdata, xt_code)
        if name:
            mapping[name] = code
    return mapping


def extract_stock_names_from_sections(sections: list[tuple[str, str]], known_names: Optional[set[str]] = None) -> list[str]:
    seen: set[str] = set()
    names: list[str] = []
    if known_names is not None:
        for _, section in sections:
            for name in extract_known_stock_names(section, known_names):
                if name in seen:
                    continue
                seen.add(name)
                names.append(name)
        return names

    for _, section in sections:
        for raw in CHINESE_STOCK_TOKEN_RE.findall(section):
            token = clean_stock_token(raw)
            if not looks_like_stock_name(token, known_names=known_names):
                continue
            if token in seen:
                continue
            seen.add(token)
            names.append(token)
    return names


def collect_from_jiuyangongshe(
    *,
    user_url: str,
    article_url: str,
    sector: str,
    resolve_codes: bool = True,
    require_today: bool = True,
) -> tuple[list[PoolCandidate], str, list[tuple[str, str]]]:
    resolved_url = article_url or resolve_latest_jiuyangongshe_article_url(user_url, require_today=require_today)
    page = fetch_text(resolved_url)
    article_html = extract_jiuyangongshe_article_html(page)
    if not article_html:
        raise RuntimeError(f"未能解析文章正文: {resolved_url}")
    text = normalize_article_plain_text(article_html)
    sections = extract_target_sections(text)

    name_code_map = build_qmt_name_code_map(sector) if resolve_codes else {}
    known_names = set(name_code_map) if name_code_map else None
    names = extract_stock_names_from_sections(sections, known_names=known_names)

    candidates: list[PoolCandidate] = []
    for name in names:
        code = name_code_map.get(name, "") if name_code_map else ""
        candidates.append(
            PoolCandidate(
                code=code or name,
                name=name,
                pct_change=0.0,
                last_price=0.0,
                pre_close=0.0,
                amount=0.0,
            )
        )
    return candidates, resolved_url, sections


def collect_from_jiuyangongshe_nodes(
    *,
    user_url: str,
    article_url: str,
    sector: str,
    nodes: Iterable[str],
    resolve_codes: bool = True,
    require_today: bool = True,
) -> tuple[dict[str, list[PoolCandidate]], str, dict[str, tuple[str, str]]]:
    resolved_url = article_url or resolve_latest_jiuyangongshe_article_url(user_url, require_today=require_today)
    page = fetch_text(resolved_url)
    article_html = extract_jiuyangongshe_article_html(page)
    if not article_html:
        raise RuntimeError(f"未能解析文章正文: {resolved_url}")
    text = normalize_article_plain_text(article_html)
    sections_by_node = extract_jiuyangongshe_node_sections(text)
    name_code_map = build_qmt_name_code_map(sector) if resolve_codes else {}
    known_names = set(name_code_map) if name_code_map else None
    wanted_nodes = list(nodes)
    results: dict[str, list[PoolCandidate]] = {}
    for node in wanted_nodes:
        section = sections_by_node.get(node)
        if not section:
            results[node] = []
            continue
        names = extract_stock_names_from_sections([section], known_names=known_names)
        results[node] = [
            PoolCandidate(
                code=(name_code_map.get(name, "") if name_code_map else "") or name,
                name=name,
                pct_change=0.0,
                last_price=0.0,
                pre_close=0.0,
                amount=0.0,
            )
            for name in names
        ]
    return results, resolved_url, sections_by_node
