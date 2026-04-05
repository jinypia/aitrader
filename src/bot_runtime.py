from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
import fcntl
import csv
import io
import html
import urllib.parse
import xml.etree.ElementTree as ET
import requests
import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from config import load_settings, selection_universe_symbols
from kiwoom_api import KiwoomAPI
from strategy import bearish_long_exception_ready, decide_action, trend_runtime_diagnostics, trend_runtime_signal
from scalping_strategy import ScalpParams, calculate_scalp_metrics, scalp_entry_signal, scalp_exit_signal


def _is_scalping_mode(settings: Any) -> bool:
    """Check if scalping mode is enabled"""
    return str(getattr(settings, "strategy_mode", "AUTO")).upper() == "SCALPING"


def _get_scalping_params(settings: Any) -> ScalpParams:
    """Get scalping parameters from settings"""
    return ScalpParams(
        rsi_entry_min=float(getattr(settings, "scalping_rsi_entry_min", 30.0)),
        rsi_entry_max=float(getattr(settings, "scalping_rsi_entry_max", 70.0)),
        rsi_exit_oversold=float(getattr(settings, "scalping_rsi_exit_min", 25.0)),
        rsi_exit_overbought=float(getattr(settings, "scalping_rsi_exit_max", 75.0)),
        volume_spike_threshold=float(getattr(settings, "scalping_volume_spike_ratio", 2.0)),
        profit_target_pct=float(getattr(settings, "scalping_profit_target_pct", 0.8)),
        stop_loss_pct=abs(float(getattr(settings, "scalping_stop_loss_pct", -0.5))),
        max_hold_bars=int(getattr(settings, "scalping_max_hold_bars", 6)),
        trend_strength_threshold=float(getattr(settings, "scalping_min_trend_strength", 0.1)),
        min_volume_ratio=float(getattr(settings, "scalping_min_volume_ratio", 1.5)),
    )


def _get_symbol_intraday_bars(
    symbol: str,
    max_bars: int = 50,
    *,
    api: KiwoomAPI | None = None,
    bar_interval_minutes: int = 2,
    prefer_live: bool = True,
) -> tuple[list[dict[str, float]], str]:
    """Get recent intraday bars for scalping analysis.

    During market session, this should prefer live API bars.
    Outside market session, local stored history is an acceptable fallback.
    """
    if prefer_live and api is not None:
        try:
            live_rows = api.get_intraday_bars(
                symbol,
                interval=max(1, int(bar_interval_minutes)),
                limit=max(20, int(max_bars)),
            )
            live_rows = [row for row in list(live_rows or []) if isinstance(row, dict)]
            if live_rows:
                bars: list[dict[str, float]] = []
                for row in live_rows[-max_bars:]:
                    close = float(row.get("close", 0.0))
                    if close <= 0:
                        continue
                    open_p = float(row.get("open", close) or close)
                    high = float(row.get("high", max(open_p, close)) or max(open_p, close))
                    low = float(row.get("low", min(open_p, close)) or min(open_p, close))
                    bars.append(
                        {
                            "bar_ts": str(row.get("timestamp", "")),
                            "close": close,
                            "open": open_p,
                            "high": high,
                            "low": low,
                            "volume": float(row.get("volume", 0.0) or 0.0),
                        }
                    )
                if bars:
                    return bars, "LIVE_INTRADAY"
        except Exception:
            # If live is required for current session, fail closed.
            return [], "LIVE_ERROR"

    path = Path("data/selected_intraday_prices.json")
    if not path.exists():
        return [], "NO_HISTORY_FILE"

    try:
        payload = json.loads(path.read_text())
        rows = [row for row in list(payload.get("rows") or []) if isinstance(row, dict)]
        symbol_rows = [row for row in rows if str(row.get("symbol", "")).strip() == symbol]
        symbol_rows.sort(key=lambda x: str(x.get("bar_ts", "")))
        symbol_rows = symbol_rows[-max_bars:]

        bars = []
        for row in symbol_rows:
            close = float(row.get("price", 0.0))
            if close > 0:
                bars.append(
                    {
                        "bar_ts": str(row.get("bar_ts", "")),
                        "close": close,
                        "open": close,
                        "high": close,
                        "low": close,
                        "volume": float(row.get("quote_volume", 0.0)),
                    }
                )
        return bars, "HISTORY_INTRADAY"
    except Exception:
        return [], "HISTORY_ERROR"


def _scalping_decision(
    symbol: str,
    current_price: float,
    qty: int,
    entry_price: float,
    hold_bars: int,
    settings: Any,
    *,
    api: KiwoomAPI | None = None,
    prefer_live: bool = True,
    trade_policy: str = "NORMAL",
    risk_score: float = 0.0,
) -> tuple[str, bool, str]:
    """Make scalping decision for symbol"""
    if not _is_scalping_mode(settings):
        return "HOLD", False, "SCALPING_DISABLED"
    
    params = _get_scalping_params(settings)
    policy = str(trade_policy or "NORMAL").strip().upper()
    risk = max(0.0, min(100.0, float(risk_score)))
    if policy == "CAUTION":
        vol_boost = float(getattr(settings, "market_policy_scalping_min_volume_boost", 0.20))
        params.min_volume_ratio += vol_boost
        params.momentum_threshold *= 1.20
    elif policy == "HALT":
        # In HALT mode, scalping follows the same fail-closed stance as discretionary entries.
        return "HOLD", False, "POLICY_HALT"
    elif risk >= 60.0:
        params.min_volume_ratio += max(0.10, float(getattr(settings, "market_policy_scalping_min_volume_boost", 0.20)) * 0.5)
        params.momentum_threshold *= 1.10
    bars, data_source = _get_symbol_intraday_bars(
        symbol,
        api=api,
        bar_interval_minutes=max(1, int(getattr(settings, "bar_interval_minutes", 2))),
        prefer_live=prefer_live,
    )
    
    if len(bars) < 12:  # Need minimum bars for metrics
        return "HOLD", False, data_source
    
    # Calculate metrics
    metrics = calculate_scalp_metrics(bars, params)
    if not metrics:  # Insufficient data
        return "HOLD", False, data_source
    
    rsi = metrics.get("rsi", 50.0)
    
    if qty <= 0:  # No position - check entry
        if scalp_entry_signal(metrics, params):
            return "BUY", True, data_source
    else:  # Have position - check exit
        exit_reason = scalp_exit_signal(entry_price, current_price, hold_bars, rsi, params)
        if exit_reason:
            return "SELL", True, data_source
    
    return "HOLD", False, data_source


def _acquire_runtime_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_fp = path.open("a+")
    try:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            lock_fp.close()
        except Exception:
            pass
        return None
    lock_fp.seek(0)
    lock_fp.truncate()
    lock_fp.write(
        " ".join(
            [
                f"pid={os.getpid()}",
                f"cwd={Path.cwd()}",
                f"started={datetime.now().isoformat(timespec='seconds')}",
            ]
        )
    )
    lock_fp.flush()
    return lock_fp


def _runtime_lock_holder_hint(path: Path) -> str:
    """Best-effort human-readable lock holder hint from lock file contents."""
    raw = ""
    try:
        if path.exists():
            raw = path.read_text(encoding="utf-8").strip()
    except Exception:
        raw = ""

    owner_pid = ""
    try:
        out = subprocess.check_output(
            ["lsof", "-t", str(path)],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        owner_pid = next((line.strip() for line in out.splitlines() if line.strip().isdigit()), "")
    except Exception:
        owner_pid = ""

    if not owner_pid and raw:
        match = re.search(r"(?:^|\s)pid=(\d+)(?:\s|$)", raw)
        if match:
            owner_pid = match.group(1)

    owner_cmd = ""
    if owner_pid:
        try:
            owner_cmd = subprocess.check_output(
                ["ps", "-p", owner_pid, "-o", "command="],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except Exception:
            owner_cmd = ""

    parts: list[str] = []
    if raw:
        parts.append(raw)
    if owner_pid and ("pid=" not in raw):
        parts.append(f"pid={owner_pid}")
    if owner_cmd:
        parts.append(f"cmd={owner_cmd}")
    hint = " ".join(parts).strip()
    return hint[:420]


def _release_runtime_lock(lock_fp) -> None:
    if not lock_fp:
        return
    try:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        lock_fp.close()
    except Exception:
        pass


class SlackNotifier:
    def __init__(
        self,
        webhook_url: str,
        keywords: list[str],
        *,
        bot_token: str = "",
        channel_id: str = "",
        attach_web_capture: bool = False,
        capture_url: str = "http://127.0.0.1:8080/",
        capture_width: int = 1600,
        capture_height: int = 1100,
    ) -> None:
        self.webhook_url = webhook_url.strip()
        self.keywords = [x.strip() for x in keywords if x.strip()]
        self.bot_token = bot_token.strip()
        self.channel_id = channel_id.strip()
        self.attach_web_capture = bool(attach_web_capture)
        self.capture_url = capture_url.strip() or "http://127.0.0.1:8080/"
        self.capture_width = max(800, int(capture_width))
        self.capture_height = max(600, int(capture_height))

    def should_send(self, message: str) -> bool:
        if not self.webhook_url:
            return False
        if not self.keywords:
            return True
        return any(k in message for k in self.keywords)

    def send(self, message: str, *, force: bool = False) -> None:
        if (not force) and (not self.should_send(message)):
            return
        try:
            if self.webhook_url:
                requests.post(
                    self.webhook_url,
                    json={"text": f"[AITRADER] {message}"},
                    timeout=5,
                )
        except Exception:
            pass
        try:
            if self.attach_web_capture:
                self._upload_web_capture(message)
        except Exception:
            pass

    def _capture_dashboard_png(self) -> bytes | None:
        # Optional dependency: install playwright + chromium to enable web capture.
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except Exception:
            return None
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(
                    viewport={"width": self.capture_width, "height": self.capture_height}
                )
                page.goto(self.capture_url, wait_until="networkidle", timeout=15000)
                png = page.screenshot(full_page=True, type="png")
                browser.close()
                return png
        except Exception:
            return None

    def _upload_web_capture(self, message: str) -> None:
        if not self.bot_token or not self.channel_id:
            return
        png = self._capture_dashboard_png()
        if not png:
            return
        headers = {"Authorization": f"Bearer {self.bot_token}"}
        data = {
            "channels": self.channel_id,
            "filename": "aitrader_dashboard.png",
            "title": "AITRADER Dashboard Capture",
        }
        # Avoid duplicate text when webhook text already sent.
        if not self.webhook_url:
            data["initial_comment"] = f"[AITRADER] {message}"
        requests.post(
            "https://slack.com/api/files.upload",
            headers=headers,
            data=data,
            files={"file": ("aitrader_dashboard.png", png, "image/png")},
            timeout=20,
        )


_SLACK_NOTIFIER: SlackNotifier | None = None
TECH_VOLUME_SPIKE_MULT = 1.25
TECH_SHORT_BOTTOM_BB_MAX = 0.35
TECH_SHORT_TOP_BB_MIN = 0.68
TECH_GC_ENTRY_BB_MAX = 0.72
TECH_TREND_MIN_SMA_RATIO = 1.002


def _clean_headline(text: str) -> str:
    raw = html.unescape(str(text or ""))
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def _headline_dedupe_key(text: str) -> str:
    raw = _clean_headline(text).lower()
    # RSS titles often append a publisher after a dash; strip it so the same
    # story from the same feed does not appear as a "new" headline repeatedly.
    raw = re.sub(r"\s+[-|]\s+[^-|]{1,40}$", "", raw).strip()
    raw = re.sub(r"[^a-z0-9가-힣\s]", "", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def _news_item_key(item: dict[str, str] | object) -> str:
    if not isinstance(item, dict):
        return _headline_dedupe_key(str(item))
    link = str(item.get("link") or "").strip()
    pub = str(item.get("published") or "").strip()
    title = str(item.get("title") or "").strip()
    if link:
        return f"link:{link}"
    if pub and title:
        return f"pub:{pub}|title:{_headline_dedupe_key(title)}"
    return f"title:{_headline_dedupe_key(title)}"


def _truncate_text(text: str, limit: int) -> str:
    clean = _clean_headline(text)
    n = max(8, int(limit))
    if len(clean) <= n:
        return clean
    return clean[: max(0, n - 3)].rstrip() + "..."


def _chunk_lines(prefix: str, lines: list[str], *, max_chars: int = 2800) -> list[str]:
    head = str(prefix or "").strip()
    if not lines:
        return [head] if head else []
    chunks: list[str] = []
    current = head
    for line in lines:
        piece = str(line or "").strip()
        if not piece:
            continue
        candidate = piece if not current else current + "\n" + piece
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = piece if not head else head + "\n" + piece
            if len(current) > max_chars:
                current = _truncate_text(current, max_chars)
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _fetch_google_news_items(query: str, *, limit: int = 5) -> list[dict[str, str]]:
    q = str(query or "").strip()
    if not q:
        return []
    encoded = urllib.parse.quote(q)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=ko&gl=KR&ceid=KR:ko"
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "AITRADER/1.0"})
        r.raise_for_status()
        root = ET.fromstring(r.text)
    except Exception:
        return []
    items: list[dict[str, str]] = []
    for node in root.findall(".//item")[: max(1, int(limit))]:
        title = _clean_headline(node.findtext("title", ""))
        link = str(node.findtext("link", "") or "").strip()
        pub = str(node.findtext("pubDate", "") or "").strip()
        if title:
            items.append({"title": title, "link": link, "published": pub, "query": q})
    return items


def _parse_news_published_dt(published: str, *, tz_name: str = "Asia/Seoul") -> datetime | None:
    raw = str(published or "").strip()
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except Exception:
        return None
    try:
        target_tz = ZoneInfo(str(tz_name or "Asia/Seoul") or "Asia/Seoul")
    except Exception:
        target_tz = ZoneInfo("Asia/Seoul")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=target_tz)
    return dt.astimezone(target_tz)


def _format_news_published(published: str, *, tz_name: str = "Asia/Seoul") -> str:
    dt = _parse_news_published_dt(published, tz_name=tz_name)
    if dt is None:
        return "time=unknown"
    return dt.strftime("%Y-%m-%d %H:%M %Z")


def _news_item_quality_score(item: dict[str, str], *, now_tz: datetime, tz_name: str) -> float:
    title = str(item.get("title") or "").lower()
    query = str(item.get("query") or "").lower()
    blob = f"{title} | {query}"
    published_dt = _parse_news_published_dt(str(item.get("published") or ""), tz_name=tz_name)
    score = 0.0
    high_value_terms = {
        "kospi": 3.0,
        "kosdaq": 3.0,
        "krx": 2.5,
        "korea stock": 2.5,
        "stock market": 1.5,
        "증시": 2.5,
        "코스피": 3.0,
        "코스닥": 3.0,
        "반도체": 2.0,
        "semiconductor": 2.0,
        "samsung": 1.2,
        "sk hynix": 1.2,
        "fed": 1.5,
        "cpi": 1.2,
        "rates": 1.0,
        "금리": 1.2,
        "환율": 1.0,
        "외국인": 1.2,
        "기관": 1.0,
        "opening": 1.0,
        "overnight": 1.0,
    }
    low_value_terms = {
        "restaurant": -2.5,
        "demo lab": -2.0,
        "academic": -1.5,
        "tax agency": -1.5,
        "24-hour open": -1.5,
        "pushback": -1.0,
        "sports": -3.0,
    }
    for term, weight in high_value_terms.items():
        if term in blob:
            score += weight
    for term, weight in low_value_terms.items():
        if term in blob:
            score += weight
    if published_dt is None:
        score -= 4.0
    else:
        if published_dt.date() != now_tz.date():
            score -= 8.0
        age_hours = max(0.0, (now_tz - published_dt).total_seconds() / 3600.0)
        score += max(0.0, 6.0 - min(age_hours, 6.0))
    return score


def _news_risk_label(items: list[dict[str, str]]) -> str:
    risk_off_terms = {
        "급락": 3.0,
        "폭락": 4.0,
        "하락": 1.5,
        "급락세": 3.0,
        "패닉": 3.0,
        "매도": 1.5,
        "긴축": 1.5,
        "인플레": 1.5,
        "관세": 2.0,
        "전쟁": 2.5,
        "불확실": 1.0,
        "약세": 1.5,
        "쇼크": 3.0,
        "침체": 2.5,
        "급감": 2.5,
        "plunge": 4.0,
        "plunges": 4.0,
        "rout": 3.5,
        "crash": 4.0,
        "selloff": 3.0,
        "hawkish": 1.5,
        "inflation": 1.5,
        "tariff": 2.0,
        "war": 2.5,
        "drop": 1.5,
        "slump": 2.5,
        "tumble": 3.0,
        "circuit breaker": 4.0,
    }
    risk_on_terms = {
        "상승": 1.5,
        "반등": 2.0,
        "랠리": 2.0,
        "완화": 1.0,
        "수혜": 1.0,
        "강세": 1.5,
        "실적": 0.8,
        "surge": 2.0,
        "rally": 2.0,
        "gain": 1.0,
        "beat": 0.8,
        "easing": 1.0,
        "rebound": 2.0,
        "jump": 1.0,
        "climb": 1.0,
    }
    score = 0.0
    severe_off_hits = 0
    for item in items:
        title = str(item.get("title") or "").lower()
        query = str(item.get("query") or "").lower()
        blob = f"{title} | {query}"
        item_score = 0.0
        for term, weight in risk_off_terms.items():
            if term in blob:
                item_score -= weight
                if weight >= 3.0:
                    severe_off_hits += 1
        for term, weight in risk_on_terms.items():
            if term in blob:
                item_score += weight
        score += item_score
    if score <= -2.5 or severe_off_hits >= 2:
        return "risk_off"
    if score >= 2.5:
        return "risk_on"
    return "mixed"


def _build_morning_news_brief(settings, *, exclude_keys: set[str] | None = None) -> dict[str, object]:
    if not bool(getattr(settings, "morning_news_enabled", False)):
        return {"summary": "news=disabled", "items": []}
    queries = [x.strip() for x in str(getattr(settings, "morning_news_queries", "")).split(",") if x.strip()]
    limit = max(3, int(getattr(settings, "morning_news_limit", 5)))
    excluded = {str(x).strip() for x in (exclude_keys or set()) if str(x).strip()}
    collected: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    tz_name = str(getattr(settings, "market_timezone", "Asia/Seoul") or "Asia/Seoul")
    now_tz = datetime.now(ZoneInfo(tz_name))
    fetch_per_query = max(6, min(12, limit * 3))
    for q in queries[:6]:
        for item in _fetch_google_news_items(q, limit=fetch_per_query):
            title = str(item.get("title") or "").strip()
            item_key = _news_item_key(item)
            if not title or item_key in seen_keys or item_key in excluded:
                continue
            published_dt = _parse_news_published_dt(str(item.get("published") or ""), tz_name=tz_name)
            if published_dt is None or published_dt.date() != now_tz.date():
                continue
            seen_keys.add(item_key)
            enriched = dict(item)
            enriched["quality_score"] = round(
                _news_item_quality_score(item, now_tz=now_tz, tz_name=tz_name),
                3,
            )
            collected.append(enriched)
    collected.sort(
        key=lambda item: (
            float(item.get("quality_score", 0.0) or 0.0),
            _parse_news_published_dt(str(item.get("published") or ""), tz_name=tz_name)
            or datetime.min.replace(tzinfo=ZoneInfo("UTC")),
        ),
        reverse=True,
    )
    label = _news_risk_label(collected)
    if not collected:
        return {"summary": f"news={label} headlines=none day={now_tz.strftime('%Y-%m-%d')}", "items": []}
    latest_dt = _parse_news_published_dt(str(collected[0].get("published") or ""), tz_name=tz_name)
    latest_text = latest_dt.strftime("%Y-%m-%d %H:%M %Z") if latest_dt is not None else "unknown"
    summary = (
        f"news={label} headlines={len(collected[:limit])} "
        f"pool={len(collected)} day={now_tz.strftime('%Y-%m-%d')} latest={latest_text}"
    )
    return {"summary": summary, "items": collected[:limit]}


def _selection_report_snapshot(
    selected_symbols: list[str],
    *,
    current_day: str,
    last_selection_day: str,
    fallback_symbol: str,
) -> dict[str, object]:
    locked = bool(last_selection_day == current_day and selected_symbols)
    report_symbols = list(selected_symbols) if locked else []
    return {
        "locked": locked,
        "status": "LOCKED" if locked else "PENDING",
        "primary": report_symbols[0] if report_symbols else "SELECTION_PENDING",
        "selected": report_symbols,
        "fallback_symbol": str(fallback_symbol or "").strip() or "-",
    }


def _load_market_brief_history(path: Path) -> dict[str, object]:
    try:
        if not path.exists():
            return {"last_slot_key": "", "sent_news_keys_by_day": {}}
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            return {"last_slot_key": "", "sent_news_keys_by_day": {}}
        sent = raw.get("sent_news_keys_by_day")
        if not isinstance(sent, dict):
            sent = raw.get("sent_titles_by_day") if isinstance(raw.get("sent_titles_by_day"), dict) else {}
        clean_sent = {
            str(day): [str(x).strip() for x in list(vals or []) if str(x).strip()]
            for day, vals in sent.items()
        }
        return {
            "last_slot_key": str(raw.get("last_slot_key") or ""),
            "sent_news_keys_by_day": clean_sent,
        }
    except Exception:
        return {"last_slot_key": "", "sent_news_keys_by_day": {}}


def _save_market_brief_history(path: Path, history: dict[str, object]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(history, ensure_ascii=False, indent=2))
    except Exception:
        pass


def _load_opening_review_history(path: Path) -> dict[str, object]:
    try:
        if not path.exists():
            return {"days": []}
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            return {"days": []}
        rows = [row for row in list(raw.get("days") or []) if isinstance(row, dict)]
        rows.sort(key=lambda row: str(row.get("day") or ""))
        return {"days": rows[-260:]}
    except Exception:
        return {"days": []}


def _save_opening_review_history(path: Path, history: dict[str, object]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(history, ensure_ascii=False, indent=2))
    except Exception:
        pass


def _load_selected_intraday_prices(path: Path) -> dict[str, object]:
    try:
        if not path.exists():
            return {"updated_at": "", "rows": []}
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            return {"updated_at": "", "rows": []}
        rows = [row for row in list(raw.get("rows") or []) if isinstance(row, dict)]
        return {
            "updated_at": str(raw.get("updated_at") or ""),
            "bar_interval_minutes": int(raw.get("bar_interval_minutes") or 0),
            "rows": rows[-50000:],
        }
    except Exception:
        return {"updated_at": "", "rows": []}


def _save_selected_intraday_prices(path: Path, payload: dict[str, object]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    except Exception:
        pass


def _append_selected_intraday_snapshot(
    payload: dict[str, object],
    *,
    bar_ts: str,
    rows: list[dict[str, object]],
    bar_interval_minutes: int,
    max_rows: int = 50000,
) -> dict[str, object]:
    history_rows = [row for row in list(payload.get("rows") or []) if isinstance(row, dict)]
    for row in rows:
        item = dict(row)
        item["bar_ts"] = str(bar_ts)
        item["bar_interval_minutes"] = int(bar_interval_minutes)
        history_rows.append(item)
    history_rows = history_rows[-max_rows:]
    return {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "bar_interval_minutes": int(bar_interval_minutes),
        "rows": history_rows,
    }


def _parse_market_brief_times(raw: object) -> list[tuple[int, int]]:
    seen: set[tuple[int, int]] = set()
    parsed: list[tuple[int, int]] = []
    for part in str(raw or "").split(","):
        text = part.strip()
        if not text or ":" not in text:
            continue
        hh_text, mm_text = text.split(":", 1)
        try:
            hh = int(hh_text)
            mm = int(mm_text)
        except ValueError:
            continue
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            continue
        key = (hh, mm)
        if key in seen:
            continue
        seen.add(key)
        parsed.append(key)
    parsed.sort()
    return parsed


def _stooq_symbol_us(symbol: str) -> str:
    return f"{str(symbol).strip().lower()}.us"


def _fetch_stooq_daily_closes_us(symbol: str, *, limit: int = 260) -> list[float]:
    sym = _stooq_symbol_us(symbol)
    url = f"https://stooq.com/q/d/l/?s={sym}&i=d"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
    except Exception:
        return []
    rows = list(csv.DictReader(io.StringIO(r.text)))
    closes: list[float] = []
    for row in rows:
        raw = str(row.get("Close") or "").strip()
        if not raw:
            continue
        try:
            px = float(raw)
        except Exception:
            continue
        if px > 0:
            closes.append(px)
    return closes[-max(20, int(limit)) :]


def _us_market_sentiment_profile(market_closes: list[float], idx: int) -> str:
    if idx <= 0 or idx >= len(market_closes):
        return "BALANCED"
    prev = float(market_closes[idx - 1])
    cur = float(market_closes[idx])
    chg_pct = ((cur - prev) / prev * 100.0) if prev > 0 else 0.0
    window_start = max(1, idx - 10)
    rets: list[float] = []
    for j in range(window_start, idx + 1):
        p0 = float(market_closes[j - 1])
        p1 = float(market_closes[j])
        if p0 <= 0:
            continue
        rets.append(((p1 - p0) / p0) * 100.0)
    vol_pct = float(statistics.pstdev(rets)) if len(rets) >= 3 else 0.0
    if vol_pct >= 1.8 or abs(chg_pct) >= 1.3:
        return "VOLATILITY"
    if chg_pct >= 0.6:
        return "TREND"
    if chg_pct <= -0.6:
        return "DEFENSIVE"
    return "BALANCED"


def _us_legacy_threshold_params(profile: str, *, base_buy: float, base_sell: float) -> dict[str, float | bool]:
    p = str(profile).upper()
    if p == "TREND":
        return {
            "buy_drop_pct": base_buy * 0.80,
            "sell_rise_pct": base_sell * 1.80,
            "enable_trend_entry": True,
            "breakout_buy_pct": 0.40,
            "pullback_sell_pct": -1.80,
        }
    if p == "DEFENSIVE":
        return {
            "buy_drop_pct": base_buy * 1.40,
            "sell_rise_pct": base_sell * 0.90,
            "enable_trend_entry": False,
            "breakout_buy_pct": 0.45,
            "pullback_sell_pct": -1.20,
        }
    if p == "VOLATILITY":
        return {
            "buy_drop_pct": base_buy * 1.60,
            "sell_rise_pct": base_sell * 0.80,
            "enable_trend_entry": False,
            "breakout_buy_pct": 0.45,
            "pullback_sell_pct": -1.00,
        }
    return {
        "buy_drop_pct": base_buy * 1.10,
        "sell_rise_pct": base_sell * 1.15,
        "enable_trend_entry": False,
        "breakout_buy_pct": 0.45,
        "pullback_sell_pct": -1.60,
    }


def _simulate_us_symbol_legacy_threshold(
    closes: list[float],
    *,
    market_closes: list[float],
    initial_cash: float,
    position_size: int,
    base_buy_drop_pct: float,
    base_sell_rise_pct: float,
    signal_confirm_cycles: int = 2,
) -> dict[str, float]:
    # Legacy helper for the US mock report.
    # This path intentionally remains threshold-based and is separate from the
    # KRX live trend-following runtime.
    n = min(len(closes), len(market_closes))
    if n < 3:
        return {
            "final_equity": float(initial_cash),
            "trade_count": 0.0,
            "trade_count_last_session": 0.0,
            "sell_count_last_session": 0.0,
            "win_rate_pct": 0.0,
            "max_drawdown_pct": 0.0,
        }
    cs = closes[-n:]
    ms = market_closes[-n:]
    cash = float(initial_cash)
    qty = 0
    avg = 0.0
    peak = cash
    max_dd = 0.0
    sell_pnls: list[float] = []
    trade_count = 0
    trade_count_last_session = 0
    sell_count_last_session = 0
    confirm_needed = max(1, int(signal_confirm_cycles))
    streak_sig = ""
    streak_cnt = 0
    for i in range(1, n):
        prev_p = float(cs[i - 1])
        cur_p = float(cs[i])
        profile = _us_market_sentiment_profile(ms, i)
        params = _us_legacy_threshold_params(profile, base_buy=base_buy_drop_pct, base_sell=base_sell_rise_pct)
        lookback = min(20, i)
        base_idx = max(0, i - lookback)
        mom_base = float(cs[base_idx]) if base_idx < len(cs) else prev_p
        momentum_pct = (((cur_p / mom_base) - 1.0) * 100.0) if mom_base > 0 else 0.0
        sma_window = cs[max(0, i - 19): i + 1]
        sma = (sum(float(x) for x in sma_window) / float(len(sma_window))) if sma_window else cur_p
        trend_pct = ((cur_p - sma) / sma * 100.0) if sma > 0 else 0.0
        action = decide_action(
            prev_price=prev_p,
            current_price=cur_p,
            buy_drop_pct=float(params["buy_drop_pct"]),
            sell_rise_pct=float(params["sell_rise_pct"]),
            momentum_pct=momentum_pct,
            trend_pct=trend_pct,
            enable_trend_entry=bool(params["enable_trend_entry"]),
            breakout_buy_pct=float(params["breakout_buy_pct"]),
            pullback_sell_pct=float(params["pullback_sell_pct"]),
        )
        if action in {"BUY", "SELL"}:
            if action == streak_sig:
                streak_cnt += 1
            else:
                streak_sig = action
                streak_cnt = 1
            if streak_cnt < confirm_needed:
                action = "HOLD"
        else:
            streak_sig = ""
            streak_cnt = 0
        if action == "BUY" and qty <= 0:
            buy_qty = max(1, int(position_size))
            need = buy_qty * cur_p
            if cash >= need:
                cash -= need
                qty = buy_qty
                avg = cur_p
                trade_count += 1
                if i == (n - 1):
                    trade_count_last_session += 1
        elif action == "SELL" and qty > 0:
            proceeds = qty * cur_p
            pnl = (cur_p - avg) * qty
            cash += proceeds
            qty = 0
            avg = 0.0
            sell_pnls.append(pnl)
            trade_count += 1
            if i == (n - 1):
                trade_count_last_session += 1
                sell_count_last_session += 1
        equity = cash + (qty * cur_p)
        if equity > peak:
            peak = equity
        dd = ((equity - peak) / peak * 100.0) if peak > 0 else 0.0
        if dd < max_dd:
            max_dd = dd
    # Keep open position marked-to-market at the last close (no forced liquidation).
    last_px = float(cs[-1])
    final_equity = float(cash + (qty * last_px))
    wins = sum(1 for x in sell_pnls if x > 0)
    win_rate = (wins / float(len(sell_pnls)) * 100.0) if sell_pnls else 0.0
    return {
        "final_equity": final_equity,
        "trade_count": float(trade_count),
        "trade_count_last_session": float(trade_count_last_session),
        "sell_count_last_session": float(sell_count_last_session),
        "win_rate_pct": float(win_rate),
        "max_drawdown_pct": float(max_dd),
    }


def _run_us_mock_daily_report(settings) -> dict[str, object]:
    symbols = [x.strip().upper() for x in str(settings.us_mock_symbols or "").split(",") if x.strip()]
    if not symbols:
        return {"ok": False, "error": "US_MOCK_SYMBOLS empty"}
    bench = str(settings.us_mock_benchmark_symbol or "SPY").strip().upper()
    if bench not in symbols:
        symbols = [bench] + symbols
    days = max(40, int(settings.us_mock_lookback_days))
    market = _fetch_stooq_daily_closes_us(bench, limit=days + 25)
    if len(market) < 30:
        return {"ok": False, "error": f"benchmark closes unavailable: {bench}"}

    close_map: dict[str, list[float]] = {}
    for sym in symbols:
        closes = _fetch_stooq_daily_closes_us(sym, limit=days + 25)
        if len(closes) >= 30:
            close_map[sym] = closes
    if not close_map:
        return {"ok": False, "error": "no US symbols with enough closes"}

    top_n = max(1, min(int(settings.us_mock_top_n), len(close_map)))
    market_lookback = min(20, max(10, days // 4))
    market_index_pct = 0.0
    if len(market) >= market_lookback + 1:
        b = float(market[-(market_lookback + 1)])
        l = float(market[-1])
        if b > 0:
            market_index_pct = ((l - b) / b) * 100.0
    scored: list[tuple[float, str]] = []
    for sym, closes in close_map.items():
        if len(closes) < max(30, market_lookback + 5):
            continue
        last = float(closes[-1])
        base = float(closes[-(market_lookback + 1)])
        if last <= 0 or base <= 0:
            continue
        mom = ((last - base) / base) * 100.0
        rel = mom - market_index_pct
        sma_n = min(20, len(closes))
        sma = sum(float(x) for x in closes[-sma_n:]) / float(max(1, sma_n))
        trd = ((last - sma) / sma) * 100.0 if sma > 0 else 0.0
        rets: list[float] = []
        for i in range(1, len(closes[-(market_lookback + 1):])):
            p0 = float(closes[-(market_lookback + 1):][i - 1])
            p1 = float(closes[-(market_lookback + 1):][i])
            if p0 > 0:
                rets.append((p1 - p0) / p0)
        vol = float(statistics.pstdev(rets)) * 100.0 if len(rets) >= 3 else 0.0
        score = (0.50 * mom) + (0.25 * rel) + (0.20 * trd) - (0.15 * vol)
        scored.append((score, sym))
    scored.sort(reverse=True)
    ranked = [sym for _, sym in scored[:top_n]]
    if not ranked:
        ranked = sorted(close_map.keys())[:top_n]
    total_cash = float(max(1000.0, settings.us_mock_initial_cash))
    per_cash = total_cash / float(max(1, len(ranked)))
    total_final = 0.0
    total_trades = 0
    total_trades_last_session = 0
    total_sells_last_session = 0
    win_rates: list[float] = []
    mdds: list[float] = []
    rows: list[dict[str, object]] = []
    for sym in ranked:
        closes = close_map[sym]
        px = float(closes[-1])
        qty = max(1, int(per_cash / max(1.0, px)))
        r = _simulate_us_symbol_legacy_threshold(
            closes,
            market_closes=market,
            initial_cash=per_cash,
            position_size=qty,
            base_buy_drop_pct=float(settings.us_mock_buy_drop_pct),
            base_sell_rise_pct=float(settings.us_mock_sell_rise_pct),
            signal_confirm_cycles=int(settings.us_mock_signal_confirm_cycles),
        )
        final_eq = float(r["final_equity"])
        ret_pct = ((final_eq - per_cash) / per_cash * 100.0) if per_cash > 0 else 0.0
        rows.append(
            {
                "symbol": sym,
                "ret_pct": ret_pct,
                "trade_count": int(r["trade_count"]),
                "trade_count_last_session": int(r.get("trade_count_last_session", 0.0)),
                "sell_count_last_session": int(r.get("sell_count_last_session", 0.0)),
                "win_rate_pct": float(r["win_rate_pct"]),
                "mdd_pct": float(r["max_drawdown_pct"]),
            }
        )
        total_final += final_eq
        total_trades += int(r["trade_count"])
        total_trades_last_session += int(r.get("trade_count_last_session", 0.0))
        total_sells_last_session += int(r.get("sell_count_last_session", 0.0))
        win_rates.append(float(r["win_rate_pct"]))
        mdds.append(float(r["max_drawdown_pct"]))
    total_ret = ((total_final - total_cash) / total_cash * 100.0) if total_cash > 0 else 0.0
    best = max(rows, key=lambda x: float(x["ret_pct"]))
    worst = min(rows, key=lambda x: float(x["ret_pct"]))
    avg_win = (sum(win_rates) / float(len(win_rates))) if win_rates else 0.0
    avg_mdd = (sum(mdds) / float(len(mdds))) if mdds else 0.0
    return {
        "ok": True,
        "symbols": ranked,
        "lookback_days": days,
        "initial_cash": total_cash,
        "final_equity": total_final,
        "total_return_pct": total_ret,
        "trade_count": total_trades,
        "trade_count_last_session": total_trades_last_session,
        "sell_count_last_session": total_sells_last_session,
        "avg_win_rate_pct": avg_win,
        "avg_mdd_pct": avg_mdd,
        "best_symbol": str(best["symbol"]),
        "best_ret_pct": float(best["ret_pct"]),
        "worst_symbol": str(worst["symbol"]),
        "worst_ret_pct": float(worst["ret_pct"]),
    }


def _ledger_day_summary(ledger: Ledger, day_key: str) -> dict[str, float]:
    day = str(day_key).strip()
    trades = list(ledger.trades or [])
    day_rows: list[dict] = []
    for row in trades:
        ts = str(row.get("ts", ""))
        if not ts.startswith(day):
            continue
        day_rows.append(row)
    buy_rows = [r for r in day_rows if str(r.get("side", "")).upper() == "BUY"]
    sell_rows = [r for r in day_rows if str(r.get("side", "")).upper() == "SELL"]
    realized = sum(float(r.get("realized_pnl", 0.0)) for r in sell_rows)
    wins = sum(1 for r in sell_rows if float(r.get("realized_pnl", 0.0)) > 0.0)
    win_rate = (wins / float(len(sell_rows)) * 100.0) if sell_rows else 0.0
    return {
        "trades": float(len(day_rows)),
        "buys": float(len(buy_rows)),
        "sells": float(len(sell_rows)),
        "realized_pnl": float(realized),
        "sell_win_rate_pct": float(win_rate),
    }


@dataclass
class BotState:
    running: bool = False
    started_at: float | None = None
    last_price: float | None = None
    last_action: str = "INIT"
    order_count: int = 0
    token_expires: str | None = None
    last_error: str | None = None
    loop_count: int = 0
    position_qty: int = 0
    avg_price: float = 0.0
    cash_balance: float = 0.0
    equity: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    total_pnl: float = 0.0
    total_return_pct: float = 0.0
    perf_daily_pct: float = 0.0
    perf_weekly_pct: float = 0.0
    perf_monthly_pct: float = 0.0
    selected_symbol: str = ""
    market_regime: str = "UNKNOWN"
    strategy_reference: str = ""
    selection_score: float = 0.0
    selection_reason: str = ""
    selection_detail: dict[str, object] = field(default_factory=dict)
    auto_params: dict[str, object] = field(default_factory=dict)
    regime_confidence: float = 0.0
    risk_halt_active: bool = False
    risk_halt_reason: str = ""
    stale_data_active: bool = False
    stale_data_reason: str = ""
    data_freshness_sec: float = 0.0
    position_symbol: str = ""
    active_positions: int = 0
    monitored_symbols: str = ""
    positions_summary: str = ""
    stock_statuses: list[dict[str, object]] = field(default_factory=list)
    trade_mode: str = "DRY"
    live_armed: bool = False
    portfolio_heat_pct: float = 0.0
    max_portfolio_heat_pct: float = 0.0
    max_symbol_loss_pct: float = 0.0
    reason_histogram: dict[str, int] = field(default_factory=dict)
    factor_snapshot: list[dict[str, object]] = field(default_factory=list)
    order_journal: list[dict[str, object]] = field(default_factory=list)
    reconcile_stats: dict[str, object] = field(default_factory=dict)
    session_phase: str = "OFF_HOURS"
    session_profile: str = "CAPITAL_PRESERVATION"
    session_diag: str = ""
    daily_selection_done: bool = False
    daily_selection_day: str = ""
    daily_selection_status: str = ""
    no_trade_summary: str = ""
    market_flow_summary: str = "-"
    vi_summary: str = "-"
    opening_focus_summary: str = "-"
    opening_priority_summary: str = "-"
    opening_a_grade_summary: str = "-"
    opening_review_summary: str = "-"
    decision_activity_summary: str = "-"
    selection_history_stats: list[dict[str, object]] = field(default_factory=list)
    selection_turnover_pct: float = 0.0
    selection_turnover_note: str = ""
    broker_account_snapshot: dict[str, object] = field(default_factory=dict)
    perf_profile: dict[str, float] = field(default_factory=dict)
    events: deque[str] = field(default_factory=lambda: deque(maxlen=200))


def _event(state: BotState, message: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} {message}"
    state.events.append(line)
    logging.info(message)
    if _SLACK_NOTIFIER:
        force_notify = (
            message.startswith("ROTATE_TARGET")
            or message.startswith("A_GRADE_ENTRY")
            or message.startswith("SIM_FILL: SELL")
            or message.startswith("ROTATE_EXIT")
            or (message.startswith("ORDER RESULT") and "SELL" in message)
            or message.startswith("RISK_EXIT")
        )
        _SLACK_NOTIFIER.send(line, force=force_notify)


def _record_perf_phase(profile: dict[str, float], phase: str, started_at: float) -> float:
    elapsed = max(0.0, time.perf_counter() - started_at)
    profile[str(phase)] = round(elapsed, 4)
    return elapsed


@dataclass
class Ledger:
    initial_cash: float
    cash: float
    realized_pnl: float
    positions: dict[str, dict[str, float]]
    trades: list[dict]
    equity_history: list[dict]

    @classmethod
    def create(cls, initial_cash: float) -> "Ledger":
        return cls(
            initial_cash=initial_cash,
            cash=initial_cash,
            realized_pnl=0.0,
            positions={},
            trades=[],
            equity_history=[],
        )


def _load_selection_history(path: Path) -> dict[str, object]:
    try:
        if not path.exists():
            return {"days": []}
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            return {"days": []}
        return {"days": list(raw.get("days") or [])}
    except Exception:
        return {"days": []}


def _save_selection_history(path: Path, history: dict[str, object]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(history, ensure_ascii=False, indent=2))
    except Exception:
        pass


def _record_selection_history(
    history: dict[str, object],
    *,
    day: str,
    symbols: list[str],
    primary: str,
    regime: str,
    selection_basis: str,
) -> dict[str, object]:
    days = [row for row in list(history.get("days") or []) if isinstance(row, dict)]
    entry = {
        "day": str(day),
        "symbols": [str(x).strip() for x in symbols if str(x).strip()],
        "primary": str(primary or ""),
        "regime": str(regime or ""),
        "selection_basis": str(selection_basis or ""),
    }
    replaced = False
    for idx, row in enumerate(days):
        if str(row.get("day") or "") == str(day):
            days[idx] = entry
            replaced = True
            break
    if not replaced:
        days.append(entry)
    days.sort(key=lambda row: str(row.get("day") or ""))
    return {"days": days[-260:]}


def _selection_history_stats(
    history: dict[str, object],
    current_symbols: list[str],
) -> tuple[list[dict[str, object]], float, str]:
    days = [row for row in list(history.get("days") or []) if isinstance(row, dict)]
    days.sort(key=lambda row: str(row.get("day") or ""))
    current_clean = [str(sym).strip() for sym in current_symbols if str(sym).strip()]
    current_set = set(current_clean)
    stats: list[dict[str, object]] = []
    for sym in current_clean:
        selected_days = [
            str(row.get("day") or "")
            for row in days
            if sym in {str(x).strip() for x in list(row.get("symbols") or [])}
        ]
        if not selected_days:
            continue
        streak = 0
        for row in reversed(days):
            symbols = {str(x).strip() for x in list(row.get("symbols") or [])}
            if sym in symbols:
                streak += 1
            elif streak > 0:
                break
        stats.append(
            {
                "symbol": sym,
                "selected_count": len(selected_days),
                "current_streak_days": streak,
                "last_selected_day": selected_days[-1],
                "first_selected_day": selected_days[0],
            }
        )
    prev_set: set[str] = set()
    if len(days) >= 2:
        prev_set = {str(x).strip() for x in list(days[-2].get("symbols") or []) if str(x).strip()}
    union = current_set | prev_set
    changed = current_set.symmetric_difference(prev_set)
    turnover_pct = ((len(changed) / len(union)) * 100.0) if union else 0.0
    if not prev_set:
        note = "전일 비교 데이터가 아직 없습니다."
    elif turnover_pct <= 20.0:
        note = "전일 대비 교체율이 낮아 선정 안정성이 높습니다."
    elif turnover_pct <= 50.0:
        note = "전일 대비 일부 교체가 있었습니다."
    else:
        note = "전일 대비 교체율이 높습니다. 일일 로테이션이 과할 수 있습니다."
    return stats, turnover_pct, note


def _post_market_candidate_review(
    stock_statuses: list[dict[str, object]],
    ledger,
    day_key: str,
) -> str:
    if not isinstance(stock_statuses, list) or not stock_statuses:
        return "-"

    chase_risk_keys = {
        "late_chase",
        "mid_band_late_chase",
        "high_rsi_upper_band",
        "market_surge_chase",
        "strong_overextension",
        "overextended_continuation",
    }
    trades = list(getattr(ledger, "trades", []) or [])
    by_symbol: dict[str, list[dict[str, object]]] = {}
    for tr in trades:
        if not isinstance(tr, dict):
            continue
        symbol = str(tr.get("symbol") or "").strip()
        if symbol:
            by_symbol.setdefault(symbol, []).append(tr)

    def _blocker_keys(row: dict[str, object]) -> set[str]:
        return {
            str(x or "").split(":", 1)[-1].split("(", 1)[0].strip().lower()
            for x in list(row.get("entry_blockers") or [])
            if str(x).strip()
        }

    def _symbol_outcome(symbol: str) -> str:
        rows = [
            row for row in by_symbol.get(symbol, [])
            if str(row.get("ts") or "").startswith(day_key)
        ]
        if not rows:
            return "미체결"
        sells = [row for row in rows if str(row.get("side") or "").upper() == "SELL"]
        buys = [row for row in rows if str(row.get("side") or "").upper() == "BUY"]
        if sells:
            realized = sum(float(row.get("realized_pnl", 0.0) or 0.0) for row in sells)
            return f"청산 {realized:+.0f}"
        if buys:
            return f"진입 {len(buys)}건"
        return "체결"

    priority_rows = [
        row for row in stock_statuses
        if isinstance(row, dict)
        and float(row.get("foreign_net_qty", 0.0) or 0.0) > 0.0
        and float(row.get("institution_net_qty", 0.0) or 0.0) > 0.0
        and not (_blocker_keys(row) & chase_risk_keys)
    ]
    a_grade_rows = [
        row for row in priority_rows
        if not bool(row.get("vi_active"))
    ]
    priority_text = " | ".join(
        f"{str(row.get('symbol') or '').strip()}:{_symbol_outcome(str(row.get('symbol') or '').strip())}"
        for row in priority_rows[:3]
        if str(row.get("symbol") or "").strip()
    ) or "-"
    a_grade_text = " | ".join(
        f"{str(row.get('symbol') or '').strip()}:{_symbol_outcome(str(row.get('symbol') or '').strip())}"
        for row in a_grade_rows[:3]
        if str(row.get("symbol") or "").strip()
    ) or "-"
    return f"A급={a_grade_text} / 우선관찰={priority_text}"


def _a_grade_entry_summary(
    *,
    symbol: str,
    score: float,
    gap_pct: float,
    daily_rsi: float,
    attention_ratio: float,
    value_spike_ratio: float,
    foreign_net_qty: float,
    institution_net_qty: float,
) -> str:
    return (
        f"{symbol} "
        f"score={score:+.2f} "
        f"gap={gap_pct:+.2f}% "
        f"rsi={daily_rsi:.1f} "
        f"attn={attention_ratio:.2f} "
        f"spike={value_spike_ratio:.2f} "
        f"외인={_format_flow_qty(foreign_net_qty)} "
        f"기관={_format_flow_qty(institution_net_qty)}"
    )


def _normalize_trade_row(row: object) -> dict[str, object] | None:
    if not isinstance(row, dict):
        return None
    symbol = str(row.get("symbol", "")).strip()
    side = str(row.get("side", "")).strip().upper()
    if not symbol or side not in {"BUY", "SELL"}:
        return None
    entry_mode = str(row.get("entry_mode", row.get("strategy_profile", ""))).strip()
    setup_state = str(row.get("setup_state", row.get("sentiment_class", ""))).strip()
    out = dict(row)
    out["symbol"] = symbol
    out["side"] = side
    out["entry_mode"] = entry_mode
    out["setup_state"] = setup_state
    out["a_grade_opening"] = bool(row.get("a_grade_opening", False))
    # Keep legacy keys for older readers until the whole app is migrated.
    out["strategy_profile"] = entry_mode
    out["sentiment_class"] = setup_state
    return out


def _normalize_trade_rows(rows: object, *, limit: int) -> list[dict[str, object]]:
    if not isinstance(rows, list):
        return []
    normalized: list[dict[str, object]] = []
    for row in rows[-max(1, int(limit)) :]:
        item = _normalize_trade_row(row)
        if item is not None:
            normalized.append(item)
    return normalized


def _regime_risk_profile(regime: str) -> dict[str, float]:
    regime = str(regime or "").upper()
    if regime == "BULLISH":
        return {
            "stop_atr": 1.8,
            "take_atr": 3.0,
            "trailing_atr": 1.8,
            "stop_floor_pct": 3.5,
            "take_floor_pct": 7.0,
        }
    if regime == "BEARISH":
        return {
            "stop_atr": 1.0,
            "take_atr": 2.0,
            "trailing_atr": 1.2,
            "stop_floor_pct": 2.0,
            "take_floor_pct": 3.5,
        }
    return {
        "stop_atr": 1.4,
        "take_atr": 2.4,
        "trailing_atr": 1.4,
        "stop_floor_pct": 2.8,
        "take_floor_pct": 4.5,
    }


def _load_ledger(path: Path, initial_cash: float) -> Ledger:
    if not path.exists():
        return Ledger.create(initial_cash)
    try:
        data = json.loads(path.read_text())
        loaded_initial = float(data.get("initial_cash", initial_cash))
        loaded_cash = float(data.get("cash", initial_cash))
        if loaded_initial <= 0:
            loaded_initial = initial_cash
        if loaded_cash <= 0:
            loaded_cash = loaded_initial

        positions_raw = data.get("positions")
        positions: dict[str, dict[str, float]] = {}
        if isinstance(positions_raw, dict):
            for symbol, row in positions_raw.items():
                qty = int(float(row.get("qty", 0)))
                if qty <= 0:
                    continue
                item = {
                    "qty": float(qty),
                    "avg_price": float(row.get("avg_price", 0.0)),
                    "peak_price": float(row.get("peak_price", 0.0)),
                }
                for key in (
                    "entry_regime",
                    "entry_ts",
                    "entry_stop_atr",
                    "entry_take_atr",
                    "entry_trailing_atr",
                    "entry_stop_floor_pct",
                    "entry_take_floor_pct",
                ):
                    if key in row:
                        item[key] = row.get(key)
                positions[str(symbol)] = item

        # Backward compatibility with old single-position schema.
        if not positions:
            legacy_qty = int(data.get("position_qty", 0))
            legacy_symbol = str(data.get("position_symbol", ""))
            legacy_avg = float(data.get("avg_price", 0.0))
            if legacy_qty > 0 and legacy_symbol:
                positions[legacy_symbol] = {
                    "qty": float(legacy_qty),
                    "avg_price": legacy_avg,
                    "peak_price": legacy_avg,
                }

        return Ledger(
            initial_cash=loaded_initial,
            cash=loaded_cash,
            realized_pnl=float(data.get("realized_pnl", 0.0)),
            positions=positions,
            trades=_normalize_trade_rows(data.get("trades", []), limit=2000),
            equity_history=list(data.get("equity_history", []))[-5000:],
        )
    except Exception:
        return Ledger.create(initial_cash)


def _save_ledger(path: Path, ledger: Ledger) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep legacy fields for compatibility with older consumers.
    first_symbol = ""
    first_qty = 0
    first_avg = 0.0
    if ledger.positions:
        first_symbol = sorted(ledger.positions.keys())[0]
        row = ledger.positions[first_symbol]
        first_qty = int(row.get("qty", 0))
        first_avg = float(row.get("avg_price", 0.0))

    payload = {
        "initial_cash": ledger.initial_cash,
        "cash": ledger.cash,
        "realized_pnl": ledger.realized_pnl,
        "positions": ledger.positions,
        "position_qty": first_qty,
        "avg_price": first_avg,
        "position_symbol": first_symbol,
        "trades": _normalize_trade_rows(ledger.trades, limit=2000),
        "equity_history": ledger.equity_history[-5000:],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def _position_count(ledger: Ledger) -> int:
    return sum(1 for row in ledger.positions.values() if int(row.get("qty", 0)) > 0)


def _position_qty_total(ledger: Ledger) -> int:
    return sum(int(row.get("qty", 0)) for row in ledger.positions.values())


def _primary_position(ledger: Ledger) -> tuple[str, int, float]:
    if not ledger.positions:
        return "", 0, 0.0
    symbol = sorted(
        ledger.positions.keys(),
        key=lambda s: int(ledger.positions.get(s, {}).get("qty", 0)),
        reverse=True,
    )[0]
    row = ledger.positions[symbol]
    return symbol, int(row.get("qty", 0)), float(row.get("avg_price", 0.0))


def _mark_to_market(ledger: Ledger, price_map: dict[str, float]) -> tuple[float, float, float, float]:
    market_value = 0.0
    unrealized = 0.0
    for symbol, row in ledger.positions.items():
        qty = int(row.get("qty", 0))
        if qty <= 0:
            continue
        avg = float(row.get("avg_price", 0.0))
        price = float(price_map.get(symbol, avg))
        market_value += qty * price
        unrealized += (price - avg) * qty
    equity = ledger.cash + market_value
    total_pnl = equity - ledger.initial_cash
    return equity, unrealized, total_pnl, market_value


def _apply_fill(
    ledger: Ledger,
    *,
    side: str,
    qty: int,
    price: float,
    symbol: str,
    regime: str = "",
    entry_mode: str = "",
    setup_state: str = "",
    tags: dict[str, object] | None = None,
) -> dict:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    side = side.upper()
    symbol = symbol.strip()
    if qty <= 0:
        return {"ok": False, "reason": "qty must be > 0"}
    if not symbol:
        return {"ok": False, "reason": "symbol is empty"}

    row = ledger.positions.get(symbol, {"qty": 0.0, "avg_price": 0.0, "peak_price": 0.0})
    cur_qty = int(row.get("qty", 0))
    avg = float(row.get("avg_price", 0.0))
    peak = float(row.get("peak_price", 0.0))

    if side == "BUY":
        cost = qty * price
        if ledger.cash < cost:
            return {"ok": False, "reason": "insufficient cash"}
        new_qty = cur_qty + qty
        new_avg = ((avg * cur_qty) + cost) / new_qty if new_qty > 0 else 0.0
        ledger.cash -= cost
        risk_profile = _regime_risk_profile(regime)
        ledger.positions[symbol] = {
            "qty": float(new_qty),
            "avg_price": new_avg,
            "peak_price": max(peak, price),
            "entry_regime": str(regime or ""),
            "entry_ts": str(row.get("entry_ts") or ts),
            "entry_stop_atr": float(risk_profile["stop_atr"]),
            "entry_take_atr": float(risk_profile["take_atr"]),
            "entry_trailing_atr": float(risk_profile["trailing_atr"]),
            "entry_stop_floor_pct": float(risk_profile["stop_floor_pct"]),
            "entry_take_floor_pct": float(risk_profile["take_floor_pct"]),
        }
        trade = {
            "ts": ts,
            "side": "BUY",
            "symbol": symbol,
            "qty": qty,
            "price": price,
            "regime": regime,
            "entry_mode": entry_mode,
            "setup_state": setup_state,
            "strategy_profile": entry_mode,
            "sentiment_class": setup_state,
        }
        if isinstance(tags, dict):
            trade.update(tags)
        ledger.trades.append(trade)
        return {"ok": True, "trade": trade}

    if side == "SELL":
        if cur_qty < qty:
            return {"ok": False, "reason": "insufficient position"}
        proceeds = qty * price
        realized = (price - avg) * qty
        left = cur_qty - qty
        ledger.cash += proceeds
        ledger.realized_pnl += realized
        if left > 0:
            ledger.positions[symbol] = {
                "qty": float(left),
                "avg_price": avg,
                "peak_price": max(peak, price),
                "entry_regime": row.get("entry_regime", ""),
                "entry_ts": row.get("entry_ts", ""),
                "entry_stop_atr": row.get("entry_stop_atr"),
                "entry_take_atr": row.get("entry_take_atr"),
                "entry_trailing_atr": row.get("entry_trailing_atr"),
                "entry_stop_floor_pct": row.get("entry_stop_floor_pct"),
                "entry_take_floor_pct": row.get("entry_take_floor_pct"),
            }
        else:
            ledger.positions.pop(symbol, None)
        trade = {
            "ts": ts,
            "side": "SELL",
            "symbol": symbol,
            "qty": qty,
            "price": price,
            "realized_pnl": realized,
            "regime": regime,
            "entry_mode": entry_mode,
            "setup_state": setup_state,
            "strategy_profile": entry_mode,
            "sentiment_class": setup_state,
        }
        if isinstance(tags, dict):
            trade.update(tags)
        ledger.trades.append(trade)
        return {"ok": True, "trade": trade}

    return {"ok": False, "reason": f"unsupported side={side}"}


def _infer_ai_sleeve(entry_mode: str, setup_state: str, strategy_mode: str = "") -> str:
    mode = str(strategy_mode or "").strip().upper()
    if mode == "SCALPING":
        return "scalping"

    merged = " ".join(
        [
            str(entry_mode or ""),
            str(setup_state or ""),
            str(strategy_mode or ""),
        ]
    ).upper()

    if "SCALP" in merged:
        return "scalping"
    if any(key in merged for key in ["DEFENSIVE", "RISK_OFF", "BEARISH", "CAPITAL_PRESERVATION"]):
        return "defensive"
    return "trend"


def _infer_ai_sleeve_reason(entry_mode: str, setup_state: str, strategy_mode: str = "") -> str:
    mode = str(strategy_mode or "").strip().upper()
    if mode == "SCALPING":
        return "RSN_STRATEGY_MODE_SCALPING"

    merged = " ".join(
        [
            str(entry_mode or ""),
            str(setup_state or ""),
            str(strategy_mode or ""),
        ]
    ).upper()

    if "SCALP" in merged:
        return "RSN_KEYWORD_SCALP"
    if any(key in merged for key in ["DEFENSIVE", "RISK_OFF", "BEARISH", "CAPITAL_PRESERVATION"]):
        return "RSN_KEYWORD_DEFENSIVE_RISK_OFF"
    return "RSN_DEFAULT_TREND"


def _period_return(equity_history: list[dict], current_equity: float, days: int) -> float:
    if not equity_history:
        return 0.0
    target = datetime.now() - timedelta(days=days)
    base = None
    for row in reversed(equity_history):
        ts = datetime.fromisoformat(row["ts"])
        if ts <= target:
            base = float(row["equity"])
            break
    if base is None:
        base = float(equity_history[0]["equity"])
    if base == 0:
        return 0.0
    return ((current_equity - base) / base) * 100.0


def _trade_count_on_day(ledger: Ledger, day_key: str) -> int:
    count = 0
    for row in list(ledger.trades or []):
        ts = str((row or {}).get("ts") or "")
        if ts.startswith(day_key):
            count += 1
    return count


def _held_weekdays_since(entry_ts: str, now_dt: datetime) -> int:
    raw = str(entry_ts or "").strip()
    if not raw:
        return 0
    try:
        entry_dt = datetime.fromisoformat(raw)
    except Exception:
        try:
            entry_dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return 0
    if entry_dt.tzinfo is not None and now_dt.tzinfo is None:
        entry_dt = entry_dt.replace(tzinfo=None)
    start_date = entry_dt.date()
    end_date = now_dt.date()
    if end_date <= start_date:
        return 0
    days = 0
    cur = start_date
    while cur < end_date:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            days += 1
    return days


def _parse_symbols(raw: str, fallback: str) -> list[str]:
    items = [x.strip() for x in raw.split(",") if x.strip()]
    return items if items else [fallback]


def _fetch_kind_all_symbols(source_url: str, fallback: list[str]) -> list[str]:
    """
    Download listed company table from KIND and extract 6-digit stock codes.
    Returns fallback list on any failure.
    """
    url = (source_url or "").strip()
    if not url:
        return list(fallback)
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        html = resp.text
        codes = sorted(set(re.findall(r"\b\d{6}\b", html)))
        return codes if codes else list(fallback)
    except Exception:
        return list(fallback)


def _parse_symbol_float_map(raw: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for tok in str(raw or "").split(","):
        item = tok.strip()
        if not item or ":" not in item:
            continue
        code, val = item.split(":", 1)
        sym = code.strip()
        if not sym:
            continue
        try:
            out[sym] = float(str(val).strip())
        except Exception:
            continue
    return out


def _parse_symbol_text_map(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for tok in str(raw or "").split(","):
        item = tok.strip()
        if not item or ":" not in item:
            continue
        code, val = item.split(":", 1)
        sym = code.strip()
        text = val.strip()
        if sym and text:
            out[sym] = text
    return out


def _load_sector_cache(path: Path) -> dict[str, str]:
    try:
        if not path.exists():
            return {}
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            return {}
        out: dict[str, str] = {}
        for k, v in raw.items():
            key = str(k).strip()
            val = str(v).strip()
            if key and val:
                out[key] = val
        return out
    except Exception:
        return {}


def _save_sector_cache(path: Path, data: dict[str, str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {str(k): str(v) for k, v in sorted(data.items()) if str(k).strip() and str(v).strip()}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    except Exception:
        pass


def _fetch_sector_from_naver(symbol: str) -> str:
    code = str(symbol).strip()
    if not (code.isdigit() and len(code) == 6):
        return ""
    url = f"https://finance.naver.com/item/main.naver?code={code}"
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        resp.raise_for_status()
        text = resp.text
    except Exception:
        return ""
    patterns = [
        r"업종명\s*:\s*<a [^>]*>([^<]+)</a>",
        r"업종명\s*:\s*([^<|\n\r]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if not m:
            continue
        sector = re.sub(r"\s+", " ", str(m.group(1)).strip())
        if sector:
            return sector
    return ""


def _resolve_sector_map(
    *,
    symbols: list[str],
    manual_map: dict[str, str],
    cache_map: dict[str, str],
    auto_enabled: bool,
    cache_path: Path,
    fetch_limit: int = 12,
) -> dict[str, str]:
    merged = dict(cache_map)
    merged.update(manual_map)
    if not auto_enabled:
        return merged
    unresolved = [
        sym
        for sym in symbols
        if sym
        and sym not in merged
        and str(sym).isdigit()
        and len(str(sym)) == 6
    ][: max(1, int(fetch_limit))]
    changed = False
    for sym in unresolved:
        sector = _fetch_sector_from_naver(sym)
        if not sector:
            continue
        merged[sym] = sector
        changed = True
    if changed:
        _save_sector_cache(cache_path, merged)
    return merged


def _auto_tuned_entry_params(
    settings,
    *,
    regime: str,
    regime_confidence: float,
    regime_index_pct: float,
    market_volatility_pct: float,
) -> dict[str, float]:
    """
    Derive entry/exit quality knobs from current market regime and volatility.
    Positive mood (bullish/low vol) loosens entry and keeps runners longer.
    Risk-off/high-vol tightens entry and takes profit faster.
    """
    strength = max(0.0, min(2.0, float(settings.auto_param_tuning_strength)))
    base_score = float(settings.min_entry_score)
    base_mom = float(settings.min_entry_momentum_pct)
    base_partial = float(settings.take_profit_partial_ratio)
    if not settings.auto_param_tuning_enabled:
        return {
            "min_entry_score": base_score,
            "min_entry_momentum_pct": base_mom,
            "take_profit_partial_ratio": max(0.1, min(1.0, base_partial)),
            "risk_bias": 0.0,
            "market_volatility_pct": market_volatility_pct,
        }

    conf = max(0.0, min(1.0, float(regime_confidence)))
    idx = float(regime_index_pct)
    vol = max(0.0, float(market_volatility_pct))
    vol_unit = max(0.0, min(2.0, vol / 2.0))

    if regime == "BULLISH":
        regime_bias = -0.75 * conf
    elif regime == "BEARISH":
        regime_bias = +0.65 * conf
    else:
        regime_bias = +0.15 * (0.5 - conf)
    idx_bias = max(-1.5, min(1.5, -idx / 2.0))
    vol_bias = max(-1.0, min(2.0, vol_unit - 0.8))
    risk_bias = (regime_bias + idx_bias + vol_bias) * strength

    score = base_score + (0.55 * risk_bias)
    mom = base_mom + (0.35 * risk_bias)
    partial = base_partial + (0.22 * risk_bias)
    return {
        "min_entry_score": max(-999.0, min(999.0, score)),
        "min_entry_momentum_pct": max(-50.0, min(50.0, mom)),
        "take_profit_partial_ratio": max(0.1, min(1.0, partial)),
        "risk_bias": risk_bias,
        "market_volatility_pct": vol,
    }


def _market_bias_mode(
    *,
    regime: str,
    regime_confidence: float,
    regime_index_pct: float,
    market_volatility_pct: float,
) -> tuple[str, str]:
    conf = max(0.0, min(1.0, float(regime_confidence)))
    idx = float(regime_index_pct)
    vol = max(0.0, float(market_volatility_pct))
    if regime == "BEARISH" or idx <= -0.60 or vol >= 3.20:
        return "DEFENSIVE", f"regime={regime}, idx={idx:+.2f}%, vol={vol:.2f}%"
    if regime == "BULLISH" and conf >= 0.60 and idx >= 0.40 and vol <= 2.60:
        return "AGGRESSIVE", f"regime={regime}, conf={conf:.2f}, idx={idx:+.2f}%, vol={vol:.2f}%"
    return "BALANCED", f"regime={regime}, conf={conf:.2f}, idx={idx:+.2f}%, vol={vol:.2f}%"


def _parse_hhmm(value: str, default: str) -> dt_time:
    raw = str(value or "").strip() or default
    try:
        h, m = raw.split(":", 1)
        hh = max(0, min(23, int(h)))
        mm = max(0, min(59, int(m)))
        return dt_time(hour=hh, minute=mm)
    except Exception:
        dh, dm = default.split(":", 1)
        return dt_time(hour=int(dh), minute=int(dm))


def _time_between(cur: dt_time, start: dt_time, end: dt_time) -> bool:
    if start <= end:
        return start <= cur <= end
    # Overnight window fallback.
    return cur >= start or cur <= end


def _market_now(settings) -> datetime:
    tz_name = str(getattr(settings, "market_timezone", "Asia/Seoul") or "Asia/Seoul")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Asia/Seoul")
    return datetime.now(tz)


def _manual_event_profile(manual_alert: str) -> str:
    text = str(manual_alert or "").strip().lower()
    if not text:
        return "NONE"
    if any(tag in text for tag in ["credit", "liquidity", "funding", "repo", "cp"]):
        return "LIQUIDITY_STRESS"
    if any(tag in text for tag in ["energy", "oil", "lng", "hormuz", "원유", "유가"]):
        return "ENERGY_SHOCK"
    if any(tag in text for tag in ["tariff", "sanction", "policy", "관세", "제재", "정책"]):
        return "POLICY_SHOCK"
    return "GENERAL_ALERT"


def _market_risk_score(
    *,
    regime_index_pct: float,
    market_volatility_pct: float,
    shock_active: bool,
    event_profile: str,
    settings,
) -> float:
    idx_drop_limit = max(0.2, abs(float(getattr(settings, "market_shock_drop_pct", -2.0))))
    vol_spike_limit = max(0.5, float(getattr(settings, "vkospi_spike_proxy_pct", 3.8)))
    idx_stress = max(0.0, min(1.0, (-float(regime_index_pct)) / idx_drop_limit))
    vol_stress = max(0.0, min(1.0, float(market_volatility_pct) / vol_spike_limit))
    profile_boost_map = {
        "NONE": 0.0,
        "GENERAL_ALERT": 6.0,
        "POLICY_SHOCK": 10.0,
        "ENERGY_SHOCK": 12.0,
        "LIQUIDITY_STRESS": 16.0,
    }
    shock_boost = 18.0 if shock_active else 0.0
    score = (idx_stress * 40.0) + (vol_stress * 36.0) + shock_boost + profile_boost_map.get(str(event_profile), 0.0)
    return max(0.0, min(100.0, score))


def _policy_from_risk(risk_score: float, settings) -> str:
    caution = float(getattr(settings, "market_policy_caution_risk_score", 45.0))
    halt = float(getattr(settings, "market_policy_halt_risk_score", 72.0))
    if risk_score >= halt:
        return "HALT"
    if risk_score >= caution:
        return "CAUTION"
    return "NORMAL"


def _krx_session_context(
    settings,
    *,
    now_kst: datetime,
    regime_index_pct: float,
    market_volatility_pct: float,
) -> dict[str, object]:
    cur_t = now_kst.time()
    pre_s = _parse_hhmm(settings.premarket_brief_start, "07:00")
    pre_e = _parse_hhmm(settings.premarket_brief_end, "08:30")
    open_s = _parse_hhmm(settings.opening_focus_start, "08:50")
    open_e = _parse_hhmm(settings.opening_focus_end, "09:15")
    reg_s = _parse_hhmm(settings.regular_session_start, "09:00")
    reg_e = _parse_hhmm(settings.regular_session_end, "15:30")
    aft_s = _parse_hhmm(settings.after_market_start, "16:00")
    aft_e = _parse_hhmm(settings.after_market_end, "20:00")

    phase = "OFF_HOURS"
    profile = "CAPITAL_PRESERVATION"
    allow_buy = False
    allow_sell = False
    buy_mult = 1.20
    sell_mult = 0.90

    if _time_between(cur_t, pre_s, pre_e):
        phase = "PREMARKET_BRIEF"
        profile = "WATCHLIST_PREP"
    elif _time_between(cur_t, open_s, open_e):
        phase = "OPENING_FOCUS"
        profile = "OPENING_MOMENTUM"
        allow_buy = True
        allow_sell = True
        buy_mult = 0.88
        sell_mult = 1.08
    elif _time_between(cur_t, reg_s, reg_e):
        # Last 10 minutes: avoid aggressive new entries.
        close_guard_start = dt_time(hour=max(0, reg_e.hour), minute=max(0, reg_e.minute - 10))
        if _time_between(cur_t, close_guard_start, reg_e):
            phase = "CLOSE_GUARD"
            profile = "CAPITAL_PRESERVATION"
            allow_buy = False
            allow_sell = True
            buy_mult = 1.25
            sell_mult = 0.85
        else:
            phase = "REGULAR_SESSION"
            profile = "INTRADAY_BALANCED"
            allow_buy = True
            allow_sell = True
            buy_mult = 1.00
            sell_mult = 1.00
    elif _time_between(cur_t, aft_s, aft_e):
        phase = "AFTER_MARKET"
        profile = "POST_MARKET_DEFENSIVE"
        allow_buy = False
        allow_sell = bool(settings.allow_after_market_sell)
        buy_mult = 1.30
        sell_mult = 0.85

    if not bool(settings.enable_krx_session_gates):
        phase = "ALWAYS_ON"
        profile = "SESSION_GATES_DISABLED"
        allow_buy = True
        allow_sell = True
        buy_mult = 1.00
        sell_mult = 1.00

    manual_alert = str(getattr(settings, "manual_market_alert", "") or "").strip().lower()
    event_profile = _manual_event_profile(manual_alert)
    alert_tags = ["sidecar", "circuit", "vi", "서킷", "변동성 완화장치"]
    manual_halt = any(tag in manual_alert for tag in alert_tags)
    shock_by_index = float(regime_index_pct) <= float(settings.market_shock_drop_pct)
    shock_by_vol = float(market_volatility_pct) >= float(settings.vkospi_spike_proxy_pct)
    shock_active = manual_halt or shock_by_index or shock_by_vol
    risk_score = _market_risk_score(
        regime_index_pct=regime_index_pct,
        market_volatility_pct=market_volatility_pct,
        shock_active=shock_active,
        event_profile=event_profile,
        settings=settings,
    )
    trade_policy = _policy_from_risk(risk_score, settings)

    shock_reasons: list[str] = []
    if manual_halt:
        shock_reasons.append(f"manual_alert={manual_alert}")
    if shock_by_index:
        shock_reasons.append(
            f"market_drop={float(regime_index_pct):+.2f}%<={float(settings.market_shock_drop_pct):+.2f}%"
        )
    if shock_by_vol:
        shock_reasons.append(
            f"vol_proxy={float(market_volatility_pct):.2f}%>={float(settings.vkospi_spike_proxy_pct):.2f}%"
        )
    if shock_active:
        allow_buy = False
        profile = "CAPITAL_PRESERVATION"
        buy_mult = max(1.35, float(buy_mult))
        sell_mult = min(0.85, float(sell_mult))
    if trade_policy == "CAUTION":
        profile = "CAUTION_RISK_CONTROL"
        buy_mult = max(1.20, float(buy_mult))
        sell_mult = min(0.95, float(sell_mult))
    elif trade_policy == "HALT":
        allow_buy = False
        profile = "HALT_NEW_BUYS"
        buy_mult = max(1.45, float(buy_mult))
        sell_mult = min(0.85, float(sell_mult))

    diag = (
        f"phase={phase} profile={profile} allow={int(allow_buy)}/{int(allow_sell)} "
        f"policy={trade_policy} risk={risk_score:.1f} shock={int(shock_active)} "
        f"idx={float(regime_index_pct):+.2f}% mvol={float(market_volatility_pct):.2f}%"
    )
    return {
        "phase": phase,
        "profile": profile,
        "allow_buy": allow_buy,
        "allow_sell": allow_sell,
        "buy_mult": float(buy_mult),
        "sell_mult": float(sell_mult),
        "trade_policy": trade_policy,
        "risk_score": float(risk_score),
        "event_profile": event_profile,
        "shock_active": shock_active,
        "shock_reason": ", ".join(shock_reasons),
        "diag": diag,
    }


def _to_num(value: object) -> float:
    text = str(value or "0").replace(",", "").replace("+", "").strip()
    while "--" in text:
        text = text.replace("--", "-")
    try:
        return float(text)
    except ValueError:
        return 0.0


def _infer_market_regime(api: KiwoomAPI, settings) -> tuple[str, float, float]:
    snapshot = api.get_market_regime_snapshot(inds_cd="001")
    rows = snapshot.get("all_inds_idex") or []
    if not rows:
        return "UNKNOWN", 0.0, 0.0
    base = next((r for r in rows if str(r.get("stk_cd")) == "001"), rows[0])
    idx_pct = _to_num(base.get("flu_rt", "0"))
    rising = _to_num(base.get("rising", "0"))
    falling = _to_num(base.get("fall", "0"))
    flat = _to_num(base.get("stdns", "0"))
    total = max(1.0, rising + falling + flat)
    breadth = rising / total
    bull_score = (max(0.0, idx_pct) / 1.5) * 0.6 + (max(0.0, breadth - 0.5) / 0.5) * 0.4
    bear_score = (max(0.0, -idx_pct) / 1.5) * 0.6 + (max(0.0, 0.5 - breadth) / 0.5) * 0.4
    bull_score = max(0.0, min(1.0, bull_score))
    bear_score = max(0.0, min(1.0, bear_score))
    if idx_pct >= 0.7 and breadth >= 0.55:
        return "BULLISH", idx_pct, bull_score
    if idx_pct <= -0.7 and breadth <= 0.45:
        return "BEARISH", idx_pct, bear_score
    neutral_conf = max(0.0, min(1.0, 1.0 - max(bull_score, bear_score)))
    return "NEUTRAL", idx_pct, neutral_conf


def _humanize_market_type(value: object) -> str:
    key = str(value or "").strip().upper()
    mapping = {
        "OVEREXTENDED_MOMENTUM": "과열 추세장",
        "SURGE_CHASE": "급등 추격 구간",
        "HEALTHY_TREND": "건강한 추세장",
        "MIXED_TREND": "혼합 추세장",
        "WEAK_TAPE": "약한 테이프",
        "NEUTRAL": "중립",
    }
    return mapping.get(key, key or "-")


def _humanize_bias_mode(value: object) -> str:
    key = str(value or "").strip().upper()
    mapping = {
        "AGGRESSIVE": "공격 운용",
        "BALANCED": "균형 운용",
        "DEFENSIVE": "방어 운용",
    }
    return mapping.get(key, key or "-")


def _humanize_session_phase(value: object) -> str:
    key = str(value or "").strip().upper()
    mapping = {
        "PREMARKET_BRIEF": "프리마켓 브리프",
        "OPENING_FOCUS": "시초 집중",
        "REGULAR_SESSION": "정규장",
        "CLOSE_GUARD": "마감 경계",
        "AFTER_MARKET": "시간외",
        "OFF_HOURS": "장외",
        "ALWAYS_ON": "상시 운용",
    }
    return mapping.get(key, key or "-")


def _humanize_watch_reason(value: object) -> str:
    key = str(value or "").strip().lower()
    mapping = {
        "high_rsi_upper_band": "상단 과열 감시",
        "strong_overextension": "강한 과열 연장",
        "overextended_continuation": "연장 추세 과열",
        "mid_band_late_chase": "중단 추격 주의",
        "mid_band_continuation": "중단 연속형 감시",
        "weak_breakout": "약한 돌파 감시",
        "weak_torque": "약한 토크 추격",
        "low_attention_continuation": "관심도 약한 연속형",
        "market_surge_chase": "시장 급등 추격",
        "extended_but_strong": "강하지만 과열",
        "candidate": "후보 감시",
    }
    return mapping.get(key, key or "-")


def _humanize_blocker(value: object) -> str:
    text = str(value or "").strip()
    key = text.split(":", 1)[-1].split("(", 1)[0].strip().lower()
    mapping = {
        "trend": "추세 미충족",
        "structure_or_breakout": "구조/돌파 미충족",
        "overheat": "과열",
        "daily_rsi_low": "RSI 낮음",
        "daily_rsi_high": "RSI 과열",
        "attention_low": "관심도 부족",
        "value_spike_low": "거래대금 스파이크 부족",
        "gap_down_skip": "갭하락 제외",
        "gap_up_skip": "갭상승 제외",
        "chase_from_open": "시가 추격 제한",
        "trend_pct_low": "추세 강도 부족",
        "weak_high_band": "상단 약수급",
        "late_chase": "늦은 추격",
        "overextended_continuation": "연장 추세 과열",
        "strong_overextension": "강한 과열 연장",
        "mid_band_late_chase": "중단 추격",
        "mid_band_continuation": "중단 연속형",
        "weak_breakout": "약한 돌파",
        "weak_torque": "약한 토크",
        "residual_mid_band_continuation": "잔여 중단 연속형",
        "residual_weak_torque": "잔여 약토크",
        "high_rsi_upper_band": "고RSI 상단권",
        "low_attention_continuation": "관심도 약한 연속형",
        "market_surge_chase": "시장 급등 추격",
        "noisy_momentum": "노이즈형 모멘텀",
        "inefficient_trend": "비효율 추세",
        "shock_reversal_risk": "충격일 반락 위험",
        "event_spike_exhaustion": "이벤트 스파이크 소진",
        "trend_entry_filter": "추세 진입 필터",
        "pullback_entry_wait": "눌림 대기",
        "quality_gate": "품질 게이트",
        "bearish_regime": "약세장 제한",
        "risk_halt": "리스크 홀트",
        "stale": "시세 지연",
        "portfolio_heat": "포트 과열",
        "entry_score": "점수 부족",
        "entry_mom": "모멘텀 부족",
        "max_active_positions": "포지션 수 제한",
        "cooldown": "재진입 쿨다운",
        "loss_cooldown": "손실 쿨다운",
        "confirm": "확인 신호 대기",
        "live_not_armed": "실주문 비활성",
        "max_daily_orders": "일일 주문 한도",
        "session": "세션 제한",
        "session_sell": "세션 매도 제한",
    }
    return mapping.get(key, text or "-")


def _format_flow_qty(value: float) -> str:
    qty = float(value or 0.0)
    if abs(qty) >= 1000000:
        return f"{qty/1000000.0:+.2f}M주"
    if abs(qty) >= 1000:
        return f"{qty/1000.0:+.1f}K주"
    return f"{qty:+.0f}주"


def _fetch_market_microstructure(
    api: KiwoomAPI,
    symbols: list[str],
) -> tuple[dict[str, dict[str, float]], str, set[str]]:
    flow_map: dict[str, dict[str, float]] = {}
    flow_rows: list[str] = []
    vi_symbols: set[str] = set()
    try:
        unique_symbols = list(dict.fromkeys(str(x).strip() for x in symbols if str(x).strip()))
        for sym in unique_symbols[:3]:
            flow = api.get_symbol_investor_flow(sym, after_close=False)
            foreign_net = float(flow.get("foreign_net_qty", 0.0) or 0.0)
            institution_net = float(flow.get("institution_net_qty", 0.0) or 0.0)
            flow_map[sym] = {
                "foreign_net_qty": foreign_net,
                "institution_net_qty": institution_net,
            }
            flow_rows.append(
                f"{sym} 외인 {_format_flow_qty(foreign_net)} / 기관 {_format_flow_qty(institution_net)}"
            )
    except Exception:
        if not flow_rows:
            flow_rows = ["조회 실패"]
    try:
        vi_snapshot = api.get_vi_trigger_snapshot()
        vi_rows = list(vi_snapshot.get("rows") or [])
        vi_symbols = {
            str((row or {}).get("symbol") or "").strip()
            for row in vi_rows
            if str((row or {}).get("symbol") or "").strip()
        }
    except Exception:
        vi_symbols = set()
    flow_summary = " | ".join(flow_rows) if flow_rows else "-"
    return flow_map, flow_summary, vi_symbols


def _symbol_intraday_brief(
    *,
    row: dict[str, object],
    symbol: str,
    prev_close: float,
    current_price: float,
) -> str:
    gap_pct = _pct_change(current_price, prev_close) if prev_close > 0 else 0.0
    opening_strength = "강" if gap_pct >= 2.0 else "중" if gap_pct >= 0.5 else "약"
    return (
        f"{symbol} "
        f"갭{gap_pct:+.2f}% "
        f"시초강도={opening_strength} "
        f"RSI={float(row.get('factor_daily_rsi', 50.0)):.1f} "
        f"ATTN={float(row.get('factor_attention_ratio', 0.0)):.2f} "
        f"SPIKE={float(row.get('factor_value_spike_ratio', 0.0)):.2f}"
    )


def _symbol_momentum_score(api: KiwoomAPI, symbol: str, lookback_days: int) -> float:
    try:
        closes = api.get_daily_closes(symbol, limit=max(lookback_days + 5, 30))
    except Exception:
        return -999.0
    if len(closes) < lookback_days + 1:
        return -999.0
    last = closes[-1]
    base = closes[-(lookback_days + 1)]
    if base <= 0:
        return -999.0
    return ((last - base) / base) * 100.0


def _symbol_volatility_pct(api: KiwoomAPI, symbol: str, lookback_days: int) -> float:
    try:
        closes = api.get_daily_closes(symbol, limit=max(lookback_days + 5, 30))
    except Exception:
        return 0.0
    if len(closes) < lookback_days + 1:
        return 0.0
    window = closes[-(lookback_days + 1) :]
    returns: list[float] = []
    for i in range(1, len(window)):
        prev = float(window[i - 1])
        cur = float(window[i])
        if prev <= 0:
            continue
        returns.append((cur - prev) / prev)
    if len(returns) < 3:
        return 0.0
    return max(0.0, float(statistics.pstdev(returns)))


def _atr_proxy_pct_from_closes(closes: list[float], lookback_days: int) -> float:
    if len(closes) < lookback_days + 1:
        return 0.0
    window = closes[-(lookback_days + 1) :]
    moves: list[float] = []
    for i in range(1, len(window)):
        prev = float(window[i - 1])
        cur = float(window[i])
        if prev <= 0:
            continue
        moves.append(abs((cur - prev) / prev))
    if not moves:
        return 0.0
    return max(0.0, float(statistics.mean(moves)))


def _volatility_pct_from_closes(closes: list[float], lookback_days: int) -> float:
    if len(closes) < lookback_days + 1:
        return 0.0
    window = closes[-(lookback_days + 1) :]
    returns: list[float] = []
    for i in range(1, len(window)):
        prev = float(window[i - 1])
        cur = float(window[i])
        if prev <= 0:
            continue
        returns.append((cur - prev) / prev)
    if len(returns) < 3:
        return 0.0
    return max(0.0, float(statistics.pstdev(returns)))


def _sma(vals: list[float], n: int) -> float:
    if len(vals) < n or n <= 0:
        return 0.0
    return sum(float(x) for x in vals[-n:]) / float(n)


def _rsi(vals: list[float], period: int = 14) -> float:
    if len(vals) < period + 1:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(len(vals) - period, len(vals)):
        delta = float(vals[i]) - float(vals[i - 1])
        if delta > 0:
            gains += delta
        elif delta < 0:
            losses += abs(delta)
    if losses <= 0:
        return 100.0 if gains > 0 else 50.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))


def _pct_change(newer: float, older: float) -> float:
    if older <= 0:
        return 0.0
    return ((float(newer) / float(older)) - 1.0) * 100.0


def _avg(seq: list[float]) -> float:
    if not seq:
        return 0.0
    return sum(float(x) for x in seq) / float(len(seq))


def _trend_structure_higher_lows_highs(bars: list[dict[str, float]]) -> bool:
    if len(bars) < 16:
        return False
    lows = [float(row.get("low", 0.0)) for row in bars[-15:] if float(row.get("low", 0.0)) > 0]
    highs = [float(row.get("high", 0.0)) for row in bars[-15:] if float(row.get("high", 0.0)) > 0]
    if len(lows) < 15 or len(highs) < 15:
        return False
    low_a = min(lows[0:5])
    low_b = min(lows[5:10])
    low_c = min(lows[10:15])
    high_a = max(highs[0:5])
    high_b = max(highs[5:10])
    high_c = max(highs[10:15])
    return (
        (low_b >= (low_a * 0.995))
        and (low_c > low_b)
        and (high_b >= (high_a * 0.995))
        and (high_c > high_b)
    )


def _trend_strategy_metrics(
    bars: list[dict[str, float]],
    *,
    market_index_pct: float,
    settings=None,
) -> dict[str, float]:
    closes = [float(row.get("close", 0.0)) for row in bars if float(row.get("close", 0.0)) > 0]
    highs = [float(row.get("high", 0.0) or row.get("close", 0.0)) for row in bars if float(row.get("close", 0.0)) > 0]
    values = [float(row.get("value", 0.0)) for row in bars if float(row.get("close", 0.0)) > 0]
    volumes = [float(row.get("volume", 0.0)) for row in bars if float(row.get("close", 0.0)) > 0]
    history_len = len(closes)
    if history_len < 30:
        return {}
    short_window = min(5, history_len)
    mid_window = min(20, history_len)
    long_window = min(60, history_len)
    trend_long_window = max(30, min(60, history_len))
    last = closes[-1]
    ma5 = _sma(closes, short_window)
    ma20 = _sma(closes, mid_window)
    ma60 = _sma(closes, trend_long_window)
    # Compare against the real prior moving averages so trend slope matches
    # the selection intent used in the strategy description and backtests.
    ma20_prev = _avg(closes[-(mid_window + 1):-1]) if history_len >= (mid_window + 1) else ma20
    ma60_prev = _avg(closes[-(trend_long_window + 1):-1]) if history_len >= (trend_long_window + 1) else ma60
    ret5 = _pct_change(last, closes[-(short_window + 1)]) if history_len >= (short_window + 1) else 0.0
    ret20 = _pct_change(last, closes[-(mid_window + 1)]) if history_len >= (mid_window + 1) else ret5
    ret60 = _pct_change(last, closes[-(long_window + 1)]) if history_len >= (long_window + 1) else ret20
    relative_pct = ret20 - market_index_pct
    atr14_pct = _atr_proxy_pct_from_closes(closes, 14) * 100.0
    volatility_pct = _volatility_pct_from_closes(closes, 20) * 100.0
    turnover5 = _avg(values[-short_window:])
    turnover20 = _avg(values[-mid_window:])
    volume5 = _avg(volumes[-short_window:])
    volume20 = _avg(volumes[-mid_window:])
    attention_ratio = (turnover5 / turnover20) if turnover20 > 0 else 0.0
    volume_ratio = (volume5 / volume20) if volume20 > 0 else 0.0
    value_spike_ratio = (values[-1] / turnover20) if turnover20 > 0 and values else 0.0
    near_high_window = min(20, len(highs))
    near_high_base = max(highs[-near_high_window:]) if near_high_window > 0 else 0.0
    near_high_pct = (last / near_high_base * 100.0) if near_high_base > 0 else 0.0
    daily_rsi = _rsi(closes, 14)
    ret1 = _pct_change(last, closes[-2]) if len(closes) >= 2 else 0.0
    ret2 = _pct_change(last, closes[-3]) if len(closes) >= 3 else ret1
    trend_ok = 1.0 if (ma5 > ma20 > ma60 and ma20 > ma20_prev and ma60 >= ma60_prev) else 0.0
    structure_ok = 1.0 if _trend_structure_higher_lows_highs(bars) else 0.0
    breakout_near_high_pct = 97.0
    if settings is not None:
        breakout_near_high_pct = float(getattr(settings, "trend_breakout_near_high_pct", 97.0))
    breakout_ok = 1.0 if near_high_pct >= breakout_near_high_pct else 0.0
    overheat = 1.0 if (ret1 >= 18.0 or ret2 >= 25.0) else 0.0
    trend_pct = _pct_change(ma20, ma20_prev) if ma20_prev > 0 else 0.0
    overextension_penalty = (
        max(0.0, ret20 - 12.0) * 0.35
        + max(0.0, trend_pct - 6.0) * 1.20
        + max(0.0, daily_rsi - 72.0) * 0.15
    )
    risk_unit_pct = max(1.0, atr14_pct, volatility_pct)
    risk_adjusted_momentum = ret20 / risk_unit_pct
    risk_adjusted_relative = relative_pct / risk_unit_pct
    trend_efficiency = max(0.0, trend_pct) / risk_unit_pct
    participation_quality = (
        (max(0.0, attention_ratio - 1.0) * 0.55)
        + (max(0.0, value_spike_ratio - 1.0) * 0.90)
        + (max(0.0, volume_ratio - 1.0) * 0.35)
    )
    speculative_participation_penalty = (
        max(0.0, attention_ratio - 1.45) * 10.0
        + max(0.0, value_spike_ratio - 1.65) * 8.0
    ) * max(0.0, 0.22 - trend_efficiency)
    noisy_participation_penalty = (
        max(0.0, attention_ratio - 1.30) * 4.5
        + max(0.0, value_spike_ratio - 1.45) * 3.5
    ) * max(0.0, 0.35 - risk_adjusted_momentum)
    crowded_low_efficiency_penalty = (
        max(0.0, attention_ratio - 1.35) * 4.0
        + max(0.0, value_spike_ratio - 1.55) * 3.0
    ) * max(0.0, 0.12 - trend_efficiency) * max(0.0, 2.60 - risk_adjusted_momentum)
    top_rank_quality_penalty = (
        max(0.0, 3.00 - risk_adjusted_momentum) * 2.8
        + max(0.0, 0.18 - trend_efficiency) * 42.0
    )
    score = (
        0.12 * ret20
        + 0.10 * ret5
        + 0.26 * relative_pct
        + 0.12 * ret60
        + 2.20 * risk_adjusted_momentum
        + 1.80 * risk_adjusted_relative
        + 2.90 * trend_efficiency
        + 3.0 * (attention_ratio - 1.0)
        + 2.5 * (volume_ratio - 1.0)
        + 1.8 * max(0.0, value_spike_ratio - 1.0)
        + 2.0 * participation_quality
        + 0.10 * (near_high_pct - 95.0)
        - 0.30 * volatility_pct
        + 8.0 * trend_ok
        + 6.0 * structure_ok
        + 4.0 * breakout_ok
        - 12.0 * overheat
        - overextension_penalty
        - speculative_participation_penalty
        - noisy_participation_penalty
        - crowded_low_efficiency_penalty
        - top_rank_quality_penalty
    )
    return {
        "score": score,
        "momentum_pct": ret20,
        "relative_pct": relative_pct,
        "trend_pct": trend_pct,
        "volatility_pct": volatility_pct,
        "ret5_pct": ret5,
        "ret20_pct": ret20,
        "ret60_pct": ret60,
        "history_len": float(history_len),
        "long_window_days": float(long_window),
        "ma5": ma5,
        "ma20": ma20,
        "ma60": ma60,
        "atr14_pct": atr14_pct,
        "turnover5": turnover5,
        "turnover20": turnover20,
        "attention_ratio": attention_ratio,
        "volume_ratio": volume_ratio,
        "value_spike_ratio": value_spike_ratio,
        "risk_adjusted_momentum": risk_adjusted_momentum,
        "risk_adjusted_relative": risk_adjusted_relative,
        "trend_efficiency": trend_efficiency,
        "participation_quality": participation_quality,
        "speculative_participation_penalty": speculative_participation_penalty,
        "noisy_participation_penalty": noisy_participation_penalty,
        "crowded_low_efficiency_penalty": crowded_low_efficiency_penalty,
        "top_rank_quality_penalty": top_rank_quality_penalty,
        "near_high_pct": near_high_pct,
        "daily_rsi": daily_rsi,
        "ret1_pct": ret1,
        "ret2_pct": ret2,
        "trend_ok": trend_ok,
        "structure_ok": structure_ok,
        "breakout_ok": breakout_ok,
        "overheat": overheat,
        "overextended": 1.0 if overextension_penalty > 0.0 else 0.0,
        "overextension_penalty": round(float(overextension_penalty), 3),
    }


def _multi_factor_rank_score(
    bars: list[dict[str, float]],
    *,
    market_index_pct: float,
    settings,
) -> tuple[float, dict[str, float]]:
    metrics = _trend_strategy_metrics(bars, market_index_pct=market_index_pct, settings=settings)
    if not metrics:
        return -999.0, {}
    history_len = int(float(metrics.get("history_len", 0.0)))
    min_long_ret = 1.0 if history_len < 60 else 2.0
    min_ram = 0.70 if history_len < 60 else 0.90
    min_rar = 0.20 if history_len < 60 else 0.35
    relaxed_attention = max(0.95, float(settings.trend_min_turnover_ratio_5_to_20) - (0.10 if history_len < 60 else 0.0))
    relaxed_spike = max(0.95, float(settings.trend_min_value_spike_ratio) - (0.10 if history_len < 60 else 0.0))
    reject_reasons: list[str] = []
    if float(metrics.get("turnover20", 0.0)) < float(settings.trend_min_avg_turnover20_krw):
        reject_reasons.append("avg_turnover20_low")
    if float(metrics.get("attention_ratio", 0.0)) < relaxed_attention:
        reject_reasons.append("attention_low")
    if float(metrics.get("value_spike_ratio", 0.0)) < relaxed_spike:
        reject_reasons.append("value_spike_low")
    atr14_pct = float(metrics.get("atr14_pct", 0.0))
    if atr14_pct < float(settings.trend_min_atr14_pct) or atr14_pct > float(settings.trend_max_atr14_pct):
        reject_reasons.append("atr14_out_of_range")
    if not bool(metrics.get("trend_ok", 0.0)):
        reject_reasons.append("trend_missing")
    if not (
        bool(metrics.get("structure_ok", 0.0))
        or bool(metrics.get("breakout_ok", 0.0))
        or (
            float(metrics.get("near_high_pct", 0.0)) >= 94.0
            and float(metrics.get("attention_ratio", 0.0)) >= 1.00
        )
    ):
        reject_reasons.append("structure_or_breakout_missing")
    if float(metrics.get("near_high_pct", 0.0)) < (100.0 - float(settings.trend_breakout_buffer_pct)):
        reject_reasons.append("far_from_high")
    if float(metrics.get("ret1_pct", 0.0)) >= float(settings.trend_overheat_day_pct):
        reject_reasons.append("overheat_1d")
    if float(metrics.get("ret2_pct", 0.0)) >= float(settings.trend_overheat_2day_pct):
        reject_reasons.append("overheat_2d")
    if float(metrics.get("relative_pct", 0.0)) <= 1.0:
        reject_reasons.append("relative_strength_low")
    if float(metrics.get("ret60_pct", 0.0)) <= min_long_ret:
        reject_reasons.append("long_return_low")
    if float(metrics.get("risk_adjusted_momentum", 0.0)) <= min_ram:
        reject_reasons.append("risk_adjusted_momentum_low")
    if float(metrics.get("risk_adjusted_relative", 0.0)) <= min_rar:
        reject_reasons.append("risk_adjusted_relative_low")
    if (
        float(metrics.get("risk_adjusted_momentum", 0.0)) >= 3.0
        and float(metrics.get("trend_efficiency", 0.0)) < 0.36
        and float(metrics.get("value_spike_ratio", 0.0)) >= 1.10
    ):
        reject_reasons.append("shock_reversal_risk")
    if (
        float(metrics.get("risk_adjusted_momentum", 0.0)) >= 4.5
        and float(metrics.get("trend_efficiency", 0.0)) < 0.28
        and (
            float(metrics.get("attention_ratio", 0.0)) >= 1.50
            or float(metrics.get("value_spike_ratio", 0.0)) >= 2.20
        )
    ):
        reject_reasons.append("event_spike_exhaustion")
    if (
        float(metrics.get("trend_efficiency", 0.0)) < 0.15
        and (
            float(metrics.get("attention_ratio", 0.0)) >= 1.50
            or float(metrics.get("value_spike_ratio", 0.0)) >= 1.70
        )
    ):
        reject_reasons.append("inefficient_trend")
    if float(metrics.get("daily_rsi", 50.0)) >= min(float(settings.trend_daily_rsi_max) + 4.0, 84.0):
        reject_reasons.append("daily_rsi_high")
    if float(metrics.get("overextension_penalty", 0.0)) >= 6.0:
        reject_reasons.append("overextension_penalty_high")
    if float(metrics.get("attention_ratio", 0.0)) < max(0.95, float(settings.trend_min_turnover_ratio_5_to_20) - 0.10):
        reject_reasons.append("attention_floor_low")
    if float(metrics.get("value_spike_ratio", 0.0)) < max(0.90, float(settings.trend_min_value_spike_ratio) - 0.15):
        reject_reasons.append("spike_floor_low")
    if float(metrics.get("ret5_pct", 0.0)) <= 0.0:
        reject_reasons.append("ret5_non_positive")
    metrics = dict(metrics)
    metrics["selection_eligible"] = 0.0 if reject_reasons else 1.0
    metrics["reject_reason"] = ",".join(reject_reasons[:6])
    if reject_reasons:
        return -999.0, metrics
    return float(metrics["score"]), metrics


def _daily_analysis_snapshot(
    *,
    api: KiwoomAPI,
    symbol: str,
    limit: int,
    market_index_pct: float,
    settings,
    cache: dict[tuple[str, int, float], dict[str, object]],
) -> dict[str, object]:
    normalized_symbol = str(symbol or "").strip()
    normalized_limit = max(1, int(limit))
    cache_key = (normalized_symbol, normalized_limit, round(float(market_index_pct), 4))
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        return cached

    bars = api.get_daily_bars(normalized_symbol, limit=normalized_limit)
    closes = [float(row.get("close", 0.0)) for row in bars if float(row.get("close", 0.0)) > 0]
    score, factors = _multi_factor_rank_score(
        bars,
        market_index_pct=market_index_pct,
        settings=settings,
    )
    required_daily_bars = 60
    eligible = bool(factors) and bool(float(factors.get("selection_eligible", 1.0)))
    reject_reason = str((factors or {}).get("reject_reason") or "").strip()
    if not closes:
        factor_reason = "daily_bars_empty"
    elif len(closes) < required_daily_bars and factors:
        factor_reason = f"proxy_history_mode({len(closes)}/{required_daily_bars})"
    elif len(closes) < required_daily_bars:
        factor_reason = f"insufficient_daily_bars({len(closes)}/{required_daily_bars})"
    elif factors and not eligible:
        factor_reason = f"trend_filter_rejected({reject_reason or 'rule'})"
    elif factors:
        factor_reason = "ready"
    else:
        factor_reason = "trend_filter_rejected"
    volatility_pct_decimal = (
        max(0.0, float(factors.get("volatility_pct", 0.0)) / 100.0)
        if factors else _volatility_pct_from_closes(closes, settings.sizing_vol_lookback_days)
    )
    atr_proxy_pct_decimal = _atr_proxy_pct_from_closes(closes, settings.atr_exit_lookback_days)
    snapshot = {
        "bars": bars,
        "closes": closes,
        "score": float(score),
        "factors": dict(factors or {}),
        "volatility_pct_decimal": float(volatility_pct_decimal),
        "atr_proxy_pct_decimal": float(atr_proxy_pct_decimal),
        "daily_bar_count": int(len(closes)),
        "required_daily_bars": int(required_daily_bars),
        "factor_data_ready": bool(factors),
        "factor_data_reason": str(factor_reason),
    }
    cache[cache_key] = snapshot
    return snapshot


def _risk_based_order_qty(
    *,
    equity: float,
    cash: float,
    price: float,
    volatility_pct: float,
    fallback_qty: int,
    target_risk_per_trade_pct: float,
    max_capital_per_name_pct: float = 100.0,
) -> int:
    if price <= 0:
        return max(1, fallback_qty)
    if equity <= 0:
        equity = max(cash, price)
    vol = volatility_pct if volatility_pct > 0 else 0.02
    risk_budget = equity * max(0.05, target_risk_per_trade_pct) / 100.0
    per_share_risk = max(price * vol, price * 0.005)
    qty = int(risk_budget / per_share_risk) if per_share_risk > 0 else fallback_qty
    qty = max(1, qty)
    affordable = int(cash / price) if price > 0 else 0
    if affordable <= 0:
        return 0
    capital_cap = int((equity * max(0.1, float(max_capital_per_name_pct)) / 100.0) / price) if price > 0 else affordable
    qty = min(qty, affordable, max(1, capital_cap))
    return max(1, qty)


def _portfolio_heat_pct(
    ledger: Ledger,
    price_map: dict[str, float],
    vol_cache: dict[str, float],
    equity: float,
) -> float:
    if equity <= 0:
        return 0.0
    risk_notional = 0.0
    for sym, row in ledger.positions.items():
        qty = int(row.get("qty", 0))
        if qty <= 0:
            continue
        px = float(price_map.get(sym, row.get("avg_price", 0.0)))
        if px <= 0:
            continue
        vol = float(vol_cache.get(sym, 0.0))
        per_share_risk = max(px * vol, px * 0.005)
        risk_notional += per_share_risk * qty
    return max(0.0, (risk_notional / equity) * 100.0)


def _mk_order_id() -> str:
    return f"ord-{int(time.time() * 1000)}"


def _build_selection_reason(
    *,
    primary_symbol: str,
    regime: str,
    strategy_reference: str,
    score: float,
    factors: dict[str, float],
    rank: int,
    total: int,
) -> str:
    mom = float(factors.get("momentum_pct", 0.0))
    ret5 = float(factors.get("ret5_pct", 0.0))
    rel = float(factors.get("relative_pct", 0.0))
    trd = float(factors.get("trend_pct", 0.0))
    vol = float(factors.get("volatility_pct", 0.0))
    atr14 = float(factors.get("atr14_pct", 0.0))
    rsi = float(factors.get("daily_rsi", 50.0))
    attn = float(factors.get("attention_ratio", 0.0))
    spike = float(factors.get("value_spike_ratio", 0.0))
    ram = float(factors.get("risk_adjusted_momentum", 0.0))
    tef = float(factors.get("trend_efficiency", 0.0))
    tqp = float(factors.get("top_rank_quality_penalty", 0.0))
    near_high = float(factors.get("near_high_pct", 0.0))
    trend_ok = int(bool(factors.get("trend_ok", 0.0)))
    structure_ok = int(bool(factors.get("structure_ok", 0.0)))
    breakout_ok = int(bool(factors.get("breakout_ok", 0.0)))
    overheat = int(bool(factors.get("overheat", 0.0)))
    return (
        f"{primary_symbol} selected: rank {rank}/{max(1, total)}, "
        f"score={score:+.2f}, regime={regime}, basis={strategy_reference}, "
        f"factors(m20={mom:+.2f}%, m5={ret5:+.2f}%, rel={rel:+.2f}%, trend={trd:+.2f}%, "
        f"vol={vol:.2f}%, atr14={atr14:.2f}%, ram={ram:.2f}, tef={tef:.2f}, tqp={tqp:.2f}, "
        f"rsi={rsi:.1f}, attn={attn:.2f}, spike={spike:.2f}, "
        f"near_high={near_high:.1f}%, trend_ok={trend_ok}, structure_ok={structure_ok}, "
        f"breakout_ok={breakout_ok}, overheat={overheat})"
    )


def _strategy_edge_from_recent_trades(
    ledger: Ledger,
    *,
    days: int = 30,
    strength: float = 1.0,
) -> dict[str, float]:
    cutoff = datetime.now() - timedelta(days=max(1, int(days)))
    grouped: dict[str, list[float]] = {}
    for row in ledger.trades[-2000:]:
        if str(row.get("side", "")).upper() != "SELL":
            continue
        entry_mode = str(row.get("entry_mode", row.get("strategy_profile", ""))).strip()
        setup_state = str(row.get("setup_state", row.get("sentiment_class", ""))).strip()
        if not entry_mode or not setup_state:
            continue
        ts_raw = str(row.get("ts", "")).strip()
        try:
            ts = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if ts < cutoff:
            continue
        pnl = float(row.get("realized_pnl", 0.0))
        key = f"{entry_mode}|{setup_state}"
        grouped.setdefault(key, []).append(pnl)

    out: dict[str, float] = {}
    for key, pnls in grouped.items():
        n = len(pnls)
        if n < 3:
            continue
        wins = sum(1 for p in pnls if p > 0)
        win_rate = wins / float(max(1, n))
        confidence = min(1.0, n / 20.0)
        edge = (win_rate - 0.5) * 0.8 * confidence * max(0.0, strength)
        out[key] = max(-0.2, min(0.2, edge))
    return out


def _classify_setup_state(factors: dict[str, float]) -> str:
    mom = float(factors.get("momentum_pct", 0.0))
    rel = float(factors.get("relative_pct", 0.0))
    trd = float(factors.get("trend_pct", 0.0))
    vol = float(factors.get("volatility_pct", 0.0))
    if vol >= 4.5 and (abs(mom) >= 1.2 or abs(trd) >= 0.8):
        return "VOLATILE"
    if mom >= 2.0 and rel >= 1.0 and trd >= 0.4:
        return "RISK_ON"
    if mom <= -1.0 or rel <= -1.0 or trd <= -0.5:
        return "RISK_OFF"
    return "NEUTRAL"


def _classify_setup_state_live(
    *,
    base_factors: dict[str, float],
    price_change_pct: float,
    volatility_pct: float,
) -> tuple[str, dict[str, float]]:
    live_mom = float(base_factors.get("momentum_pct", 0.0)) + (0.35 * float(price_change_pct))
    live_rel = float(base_factors.get("relative_pct", 0.0)) + (0.20 * float(price_change_pct))
    live_trd = float(base_factors.get("trend_pct", 0.0)) + (0.25 * float(price_change_pct))
    base_vol = float(base_factors.get("volatility_pct", 0.0))
    live_vol = max(base_vol, float(volatility_pct))
    live_factors = {
        "momentum_pct": live_mom,
        "relative_pct": live_rel,
        "trend_pct": live_trd,
        "volatility_pct": live_vol,
    }
    return _classify_setup_state(live_factors), live_factors


def _technical_signal_flags(
    *,
    closes: list[float],
    current_price: float,
    current_volume: int,
    recent_volumes: list[int],
) -> dict[str, float | bool]:
    series = [float(x) for x in closes[-120:] if float(x) > 0]
    if current_price > 0:
        series.append(float(current_price))
    if len(series) < 25:
        return {
            "sma5": 0.0,
            "sma20": 0.0,
            "sma60": 0.0,
            "bb_upper": 0.0,
            "bb_lower": 0.0,
            "bb_pos": 0.0,
            "golden_cross": False,
            "death_cross": False,
            "near_lower": False,
            "near_upper": False,
            "volume_spike": False,
            "short_bottom": False,
            "short_top": False,
            "trend_up": False,
            "trend_down": False,
        }

    def _sma(vals: list[float], n: int) -> float:
        if len(vals) < n:
            return 0.0
        return sum(vals[-n:]) / float(n)

    sma5 = _sma(series, 5)
    sma20 = _sma(series, 20)
    sma60 = _sma(series, 60) if len(series) >= 60 else _sma(series, min(60, len(series)))
    prev = series[:-1] if len(series) > 1 else series
    prev_sma5 = _sma(prev, 5)
    prev_sma20 = _sma(prev, 20)
    golden_cross = (prev_sma5 <= prev_sma20) and (sma5 > sma20) if prev_sma20 > 0 else False
    death_cross = (prev_sma5 >= prev_sma20) and (sma5 < sma20) if prev_sma20 > 0 else False

    bb_window = series[-20:]
    bb_mid = sum(bb_window) / float(len(bb_window))
    bb_std = float(statistics.pstdev(bb_window)) if len(bb_window) >= 3 else 0.0
    bb_upper = bb_mid + (2.0 * bb_std)
    bb_lower = bb_mid - (2.0 * bb_std)
    bb_span = max(1e-9, bb_upper - bb_lower)
    bb_pos = ((current_price - bb_lower) / bb_span) if current_price > 0 else 0.5
    near_lower = current_price <= (bb_lower * 1.01) if bb_lower > 0 else False
    near_upper = current_price >= (bb_upper * 0.99) if bb_upper > 0 else False

    vols = [int(v) for v in recent_volumes if int(v) > 0][-20:]
    avg_vol = (sum(vols) / float(len(vols))) if vols else 0.0
    volume_spike = (
        current_volume > (avg_vol * TECH_VOLUME_SPIKE_MULT)
    ) if avg_vol > 0 else (current_volume > 0)

    short_bottom = near_lower and (sma5 >= sma20 * 0.99) and (bb_pos <= TECH_SHORT_BOTTOM_BB_MAX)
    short_top = near_upper and ((death_cross or sma5 <= sma20 * 1.01) and (bb_pos >= TECH_SHORT_TOP_BB_MIN)
)
    trend_up = (sma20 > 0.0 and sma60 > 0.0 and (sma20 >= (sma60 * TECH_TREND_MIN_SMA_RATIO)))
    trend_down = (sma20 > 0.0 and sma60 > 0.0 and (sma20 <= (sma60 * (2.0 - TECH_TREND_MIN_SMA_RATIO))))

    return {
        "sma5": sma5,
        "sma20": sma20,
        "sma60": sma60,
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "bb_pos": bb_pos,
        "golden_cross": golden_cross,
        "death_cross": death_cross,
        "near_lower": near_lower,
        "near_upper": near_upper,
        "volume_spike": volume_spike,
        "short_bottom": short_bottom,
        "short_top": short_top,
        "trend_up": trend_up,
        "trend_down": trend_down,
    }


def _entry_plan_for_symbol(
    *,
    regime: str,
    factors: dict[str, float],
    base_confirm_cycles: int,
    strategy_edge_map: dict[str, float] | None = None,
    market_bias_mode: str = "BALANCED",
    trade_policy: str = "NORMAL",
    risk_score: float = 0.0,
    settings: Any = None,
) -> dict[str, object]:
    setup_state = _classify_setup_state(factors)
    confirm_extra = 0
    profile = "균형(Balanced)"
    enable_trend_entry = False

    # Check for scalping mode first
    if _is_scalping_mode(settings):
        profile = "스캘핑(Scalping)"
        enable_trend_entry = False  # Scalping uses its own logic
        confirm_extra = 0  # Scalping can be fast
    elif regime == "BULLISH" and setup_state == "RISK_ON":
        # Trend-friendly: allow faster participation in strong setups.
        enable_trend_entry = True
        profile = "추세추종(Trend)"
    elif regime == "BEARISH" or setup_state == "RISK_OFF":
        # Defensive mode: require more confirmation.
        confirm_extra = 1
        profile = "방어형(Defensive)"
    elif setup_state == "VOLATILE":
        # High-noise phase: require more confirmation.
        confirm_extra = 1
        profile = "고변동(Volatility-Control)"
    else:
        profile = "균형(Balanced)"

    edge_key = f"{profile}|{setup_state}"
    edge = float((strategy_edge_map or {}).get(edge_key, 0.0))
    if edge <= -0.08:
        confirm_extra += 1

    bias_mode = str(market_bias_mode or "BALANCED").strip().upper()
    policy = str(trade_policy or "NORMAL").strip().upper()
    policy_confirm_extra = 0
    if policy == "CAUTION":
        policy_confirm_extra = int(getattr(settings, "market_policy_caution_confirm_extra", 1))
        enable_trend_entry = False
    elif policy == "HALT":
        policy_confirm_extra = int(getattr(settings, "market_policy_halt_confirm_extra", 2))
        enable_trend_entry = False
    if float(risk_score) >= 60.0:
        policy_confirm_extra += 1
    if bias_mode == "AGGRESSIVE":
        if regime != "BEARISH" and setup_state in {"RISK_ON", "NEUTRAL"}:
            enable_trend_entry = True
    elif bias_mode == "DEFENSIVE":
        confirm_extra += 1

    if policy == "HALT":
        confirm_extra += 2

    confirm_needed = max(1, int(base_confirm_cycles) + confirm_extra + policy_confirm_extra)
    return {
        "entry_mode": profile,
        "setup_state": setup_state,
        "confirm_needed": confirm_needed,
        "enable_trend_entry": enable_trend_entry,
        "edge": edge,
        "edge_key": edge_key,
        "market_bias_mode": bias_mode,
        "trade_policy": policy,
    }


def run_bot(stop_event: threading.Event, state: BotState) -> None:
    settings = load_settings()
    lock_path = Path("data/bot_runtime.lock")
    runtime_lock = _acquire_runtime_lock(lock_path)
    if runtime_lock is None:
        holder_hint = _runtime_lock_holder_hint(lock_path)
        msg = "Another bot runtime is already active. Stop the existing bot before starting a new one."
        if holder_hint:
            msg += f" lock={holder_hint}"
        state.last_error = msg
        logging.warning(msg)
        return
    global _SLACK_NOTIFIER
    can_slack = bool(
        settings.slack_webhook_url
        or (settings.slack_bot_token and settings.slack_channel_id)
    )
    if settings.slack_enabled and can_slack:
        kws = [x.strip() for x in settings.slack_event_keywords.split(",") if x.strip()]
        _SLACK_NOTIFIER = SlackNotifier(
            settings.slack_webhook_url,
            kws,
            bot_token=settings.slack_bot_token,
            channel_id=settings.slack_channel_id,
            attach_web_capture=settings.slack_attach_web_capture,
            capture_url=settings.slack_capture_url,
            capture_width=settings.slack_capture_width,
            capture_height=settings.slack_capture_height,
        )
    else:
        _SLACK_NOTIFIER = None

    if not settings.app_key or not settings.secret_key:
        raise RuntimeError("KIWOOM_APP_KEY / KIWOOM_SECRET_KEY are required.")
    if not settings.price_path:
        raise RuntimeError("PRICE_PATH is required. Fill it from Kiwoom REST guide.")

    api = KiwoomAPI(settings)
    token = api.login()
    ledger_path = Path(settings.ledger_path)
    ledger = _load_ledger(ledger_path, settings.initial_cash)
    selection_history_path = Path(settings.selection_history_path)
    selection_history = _load_selection_history(selection_history_path)
    market_brief_history_path = Path(getattr(settings, "market_brief_history_path", "data/market_brief_history.json"))
    market_brief_history = _load_market_brief_history(market_brief_history_path)
    opening_review_history_path = Path("data/opening_review_history.json")
    opening_review_history = _load_opening_review_history(opening_review_history_path)
    selected_intraday_prices_path = Path("data/selected_intraday_prices.json")
    selected_intraday_prices = _load_selected_intraday_prices(selected_intraday_prices_path)
    last_selected_intraday_bar_ts = ""

    state.running = True
    state.started_at = time.time()
    state.token_expires = token.expires_dt
    state.last_error = None
    state.trade_mode = settings.trade_mode
    state.live_armed = bool(settings.live_armed)
    state.max_portfolio_heat_pct = float(settings.max_portfolio_heat_pct)
    state.max_symbol_loss_pct = float(settings.max_symbol_loss_pct)
    state.auto_params = {
        "enabled": bool(settings.auto_param_tuning_enabled),
        "strength": round(float(settings.auto_param_tuning_strength), 2),
        "min_entry_score": round(float(settings.min_entry_score), 3),
        "min_entry_momentum_pct": round(float(settings.min_entry_momentum_pct), 3),
        "take_profit_partial_ratio": round(float(settings.take_profit_partial_ratio), 3),
        "market_bias_mode": "BALANCED",
        "market_bias_reason": "",
        "market_volatility_pct": 0.0,
        "risk_bias": 0.0,
        "session_phase": "OFF_HOURS",
        "session_profile": "CAPITAL_PRESERVATION",
        "session_diag": "",
    }
    _event(state, f"Logged in. token expires: {token.expires_dt}")

    prev_prices: dict[str, float] = {}
    selected_symbols: list[str] = []
    score_map: dict[str, float] = {}
    base_selection_universe = selection_universe_symbols(settings)
    dynamic_candidate_pool: list[str] = list(base_selection_universe)
    static_universe_pool: list[str] = list(base_selection_universe)
    auto_universe_pool: list[str] = []
    last_auto_universe_refresh_ts = 0.0
    universe_batch_cursor = 0
    last_candidate_refresh_ts = 0.0
    candidate_refresh_block_until = 0.0
    last_selection_ts = 0.0
    last_selection_day = ""
    last_regime_check_ts = 0.0
    regime = "UNKNOWN"
    regime_confidence = 0.0
    regime_index_pct = 0.0
    prev_regime = "UNKNOWN"
    pending_regime = ""
    pending_regime_count = 0
    strategy_reference = ""
    vol_cache: dict[str, float] = {}
    atr_cache: dict[str, float] = {}
    last_vol_refresh_ts = 0.0
    daily_halt_day = ""
    daily_halt_notified = False
    last_heat_alert_bucket = -1
    last_trade_ts: dict[str, float] = {}
    loss_reentry_block_until: dict[str, float] = {}
    signal_streak: dict[str, tuple[str, int]] = {}
    quote_ts_map: dict[str, float] = {}
    factor_map_state: dict[str, dict[str, float]] = {}
    factor_meta_state: dict[str, dict[str, object]] = {}
    order_journal: deque[dict[str, object]] = deque(maxlen=300)
    pending_reconcile: dict[str, dict[str, object]] = {}
    selection_detail_state: dict[str, object] = {}
    symbol_strategy_map: dict[str, dict[str, object]] = {}
    quote_volume_map: dict[str, int] = {}
    quote_volume_hist: dict[str, deque[int]] = {}
    tech_close_cache: dict[str, list[float]] = {}
    analysis_focus_symbols: list[str] = []
    last_strategy_signature = ""
    strategy_edge_map: dict[str, float] = {}
    last_hourly_slack_key = ""
    last_decision_bar_ts = ""
    min_entry_score_map = _parse_symbol_float_map(settings.min_entry_score_map)
    min_entry_momentum_map = _parse_symbol_float_map(settings.min_entry_momentum_map)
    take_profit_partial_ratio_map = _parse_symbol_float_map(settings.take_profit_partial_ratio_map)
    sector_cache_path = Path(getattr(settings, "sector_cache_path", "data/sector_map_cache.json"))
    manual_sector_map = _parse_symbol_text_map(getattr(settings, "symbol_sector_map", ""))
    sector_cache_map = _load_sector_cache(sector_cache_path)
    symbol_sector_map = _resolve_sector_map(
        symbols=list(dict.fromkeys(selected_symbols + dynamic_candidate_pool + static_universe_pool)),
        manual_map=manual_sector_map,
        cache_map=sector_cache_map,
        auto_enabled=bool(getattr(settings, "sector_auto_map_enabled", True)),
        cache_path=sector_cache_path,
        fetch_limit=20,
    )
    sector_cache_map = dict(symbol_sector_map)
    last_auto_param_signature = ""
    last_market_bias_signature = ""
    last_us_mock_report_day = ""
    last_market_brief_slot_key = str(market_brief_history.get("last_slot_key") or "")
    last_no_trade_summary_day = ""
    last_opening_brief_day = ""
    last_session_signature = ""
    last_post_market_review_day = ""
    last_candidate_refresh_day = ""
    broker_account_snapshot: dict[str, object] = {}
    last_broker_refresh_ts = 0.0
    perf_log_every_loops = max(1, int(os.getenv("BOT_PERF_LOG_EVERY_LOOPS", "20")))
    perf_slow_loop_sec = max(0.25, float(os.getenv("BOT_PERF_SLOW_LOOP_SEC", "1.5")))
    perf_alert_p95_ms = max(100.0, float(os.getenv("BOT_PERF_ALERT_P95_MS", "1800")))
    perf_alert_window = max(8, int(os.getenv("BOT_PERF_ALERT_WINDOW", "30")))
    perf_alert_consecutive = max(1, int(os.getenv("BOT_PERF_ALERT_CONSECUTIVE", "3")))
    perf_alert_cooldown_sec = max(60, int(os.getenv("BOT_PERF_ALERT_COOLDOWN_SEC", "900")))
    loop_latency_samples_ms: deque[float] = deque(maxlen=perf_alert_window)
    perf_alert_hits = 0
    last_perf_alert_ts = 0.0

    while not stop_event.is_set():
        try:
            loop_started_at = time.perf_counter()
            perf_profile: dict[str, float] = {}
            daily_analysis_cache: dict[tuple[str, int, float], dict[str, object]] = {}
            now = time.time()
            now_kst = _market_now(settings)
            current_day = now_kst.strftime("%Y-%m-%d")
            should_refresh_broker_account = (
                (now - last_broker_refresh_ts) >= max(30, int(getattr(settings, "account_refresh_sec", 300)))
            )
            if should_refresh_broker_account:
                try:
                    snapshot = api.get_account_snapshot()
                    if isinstance(snapshot, dict):
                        broker_account_snapshot = dict(snapshot)
                        state.broker_account_snapshot = dict(snapshot)
                        last_broker_refresh_ts = now
                except Exception as exc:
                    broker_account_snapshot = {
                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                        "source": "mock" if "mockapi" in str(settings.base_url).lower() else "live",
                        "account_no": str(settings.account_no or ""),
                        "last_error": str(exc),
                    }
                    state.broker_account_snapshot = dict(broker_account_snapshot)
                    last_broker_refresh_ts = now
            should_rebalance = False
            should_check_regime = (now - last_regime_check_ts) >= 60
            seed_vol_values_pct = [
                max(0.0, float(v) * 100.0)
                for v in vol_cache.values()
                if float(v) > 0.0
            ]
            seed_market_volatility_pct = (
                float(statistics.mean(seed_vol_values_pct)) if seed_vol_values_pct else 2.0
            )
            auto_params = _auto_tuned_entry_params(
                settings,
                regime=regime,
                regime_confidence=regime_confidence,
                regime_index_pct=regime_index_pct,
                market_volatility_pct=seed_market_volatility_pct,
            )
            market_bias_mode, market_bias_reason = _market_bias_mode(
                regime=regime,
                regime_confidence=regime_confidence,
                regime_index_pct=regime_index_pct,
                market_volatility_pct=seed_market_volatility_pct,
            )
            session_seed = _krx_session_context(
                settings,
                now_kst=now_kst,
                regime_index_pct=regime_index_pct,
                market_volatility_pct=seed_market_volatility_pct,
            )
            session_phase_seed = str(session_seed.get("phase", "OFF_HOURS"))
            daily_selection_done = last_selection_day == current_day
            intraday_reselect_due = (
                bool(getattr(settings, "intraday_reselect_enabled", False))
                and last_selection_day == current_day
                and last_selection_ts > 0
                and (now - last_selection_ts)
                >= (
                    max(
                        1,
                        int(getattr(settings, "intraday_reselect_minutes", settings.bar_interval_minutes)),
                    )
                    * 60
                )
            )
            selection_window_open = session_phase_seed in {
                "PREMARKET_BRIEF",
                "OPENING_FOCUS",
                "REGULAR_SESSION",
                "ALWAYS_ON",
            }
            prepare_candidates_window = session_phase_seed in {
                "OFF_HOURS",
                "AFTER_MARKET",
                "PREMARKET_BRIEF",
                "OPENING_FOCUS",
                "ALWAYS_ON",
            }
            if settings.auto_select_enabled and selection_window_open and ((not daily_selection_done) or intraday_reselect_due):
                should_rebalance = True
            if bool(session_seed.get("shock_active", False)):
                market_bias_mode = "DEFENSIVE"
                shock_reason = str(session_seed.get("shock_reason") or "market_shock")
                market_bias_reason = f"shock_guard:{shock_reason}"
            should_refresh_candidates = (
                settings.auto_select_enabled
                and settings.candidate_refresh_enabled
                and prepare_candidates_window
                and now >= candidate_refresh_block_until
                and (
                    (bool(getattr(settings, "candidate_refresh_once_daily", True)) and last_candidate_refresh_day != current_day)
                    or (
                        not bool(getattr(settings, "candidate_refresh_once_daily", True))
                        and (now - last_candidate_refresh_ts) >= (max(10, settings.candidate_refresh_minutes) * 60)
                    )
                )
            )

            if should_refresh_candidates:
                phase_started_at = time.perf_counter()
                try:
                    if settings.auto_universe_enabled and (
                        (not auto_universe_pool)
                        or (now - last_auto_universe_refresh_ts)
                        >= (max(30, settings.auto_universe_refresh_minutes) * 60)
                    ):
                        prev_auto_pool = list(auto_universe_pool)
                        fallback_pool = prev_auto_pool if prev_auto_pool else list(static_universe_pool)
                        fetched = _fetch_kind_all_symbols(
                            settings.auto_universe_source_url,
                            fallback_pool,
                        )
                        fetched_unique = list(dict.fromkeys(fetched))
                        # Keep the last known-good auto universe if a refresh
                        # suddenly returns an implausibly small list.
                        if prev_auto_pool and len(fetched_unique) < max(20, len(prev_auto_pool) // 10):
                            _event(
                                state,
                                "AUTO_UNIVERSE_KEEP_PREV "
                                + f"prev={len(prev_auto_pool)} fetched={len(fetched_unique)}",
                            )
                        else:
                            auto_universe_pool = fetched_unique
                        last_auto_universe_refresh_ts = now
                        _event(
                            state,
                            "AUTO_UNIVERSE_REFRESH "
                            + f"size={len(auto_universe_pool)} source=KIND",
                        )

                    universe_base = (
                        list(auto_universe_pool)
                        if (settings.auto_universe_enabled and auto_universe_pool)
                        else list(static_universe_pool)
                    )
                    batch_size = max(0, int(settings.universe_batch_size))
                    if batch_size > 0 and len(universe_base) > batch_size:
                        start = universe_batch_cursor % len(universe_base)
                        end = start + batch_size
                        if end <= len(universe_base):
                            universe = universe_base[start:end]
                        else:
                            universe = universe_base[start:] + universe_base[: end - len(universe_base)]
                        universe_batch_cursor = (start + batch_size) % len(universe_base)
                    else:
                        universe = universe_base

                    ranked_universe: list[tuple[float, str]] = []
                    refresh_limit = max(
                        settings.momentum_lookback_days + 25,
                        settings.sizing_vol_lookback_days + 25,
                        settings.atr_exit_lookback_days + 25,
                        60,
                    )
                    for sym in universe:
                        if stop_event.is_set():
                            break
                        snap = _daily_analysis_snapshot(
                            api=api,
                            symbol=sym,
                            limit=refresh_limit,
                            market_index_pct=regime_index_pct,
                            settings=settings,
                            cache=daily_analysis_cache,
                        )
                        ranked_universe.append((float(snap.get("score", -999.0)), sym))
                    ranked_universe.sort(reverse=True)
                    top_n = max(3, int(settings.candidate_refresh_top_n))
                    min_score = float(settings.candidate_refresh_min_score)
                    refreshed = [sym for sc, sym in ranked_universe if sc >= min_score][:top_n]
                    if not refreshed:
                        refreshed = [sym for _, sym in ranked_universe[:top_n]]
                    if refreshed:
                        old_pool = list(dynamic_candidate_pool)
                        dynamic_candidate_pool = list(dict.fromkeys(refreshed))
                        if old_pool != dynamic_candidate_pool:
                            _event(
                                state,
                                "CANDIDATE_REFRESH "
                                + f"scan={len(universe)} size={len(dynamic_candidate_pool)} "
                                + f"top={','.join(dynamic_candidate_pool[:6])}",
                            )
                    last_candidate_refresh_ts = now
                    last_candidate_refresh_day = current_day
                    if selection_window_open and ((not daily_selection_done) or intraday_reselect_due):
                        should_rebalance = True
                except Exception as cand_exc:
                    cand_text = str(cand_exc)
                    if ("429" in cand_text) or ("허용된 요청 개수" in cand_text):
                        cooldown_sec = max(300, max(10, settings.candidate_refresh_minutes) * 60)
                        candidate_refresh_block_until = now + cooldown_sec
                        next_retry_kst = datetime.fromtimestamp(candidate_refresh_block_until, tz=now_kst.tzinfo)
                        _event(
                            state,
                            "CANDIDATE_REFRESH_COOLDOWN "
                            + f"until={next_retry_kst.strftime('%Y-%m-%d %H:%M:%S %Z')} "
                            + f"reason={cand_text}",
                        )
                    _event(state, f"CANDIDATE_REFRESH_FALLBACK: {cand_exc}")
                _record_perf_phase(perf_profile, "candidate_refresh_sec", phase_started_at)

            if settings.auto_select_enabled and should_check_regime:
                try:
                    new_regime, new_idx_pct, new_confidence = _infer_market_regime(api, settings)
                    regime_index_pct = float(new_idx_pct)
                    regime_confidence = new_confidence
                    confirm_cycles = max(1, settings.regime_switch_confirm_cycles)
                    min_conf = max(0.0, min(1.0, settings.regime_switch_min_confidence))

                    if regime == "UNKNOWN":
                        regime = new_regime
                        regime_confidence = new_confidence
                        _event(state, f"REGIME_SHIFT UNKNOWN -> {regime} (conf={new_confidence:.2f})")
                        if selection_window_open and ((not daily_selection_done) or intraday_reselect_due):
                            should_rebalance = True
                    elif new_regime != regime and new_confidence >= min_conf:
                        if pending_regime == new_regime:
                            pending_regime_count += 1
                        else:
                            pending_regime = new_regime
                            pending_regime_count = 1
                        _event(
                            state,
                            f"REGIME_CANDIDATE {regime} -> {new_regime} "
                            f"(conf={new_confidence:.2f}, confirm={pending_regime_count}/{confirm_cycles})",
                        )
                        if pending_regime_count >= confirm_cycles:
                            prev_regime = regime
                            regime = new_regime
                            pending_regime = ""
                            pending_regime_count = 0
                            _event(state, f"REGIME_SHIFT {prev_regime} -> {regime} (conf={new_confidence:.2f})")
                            if selection_window_open and ((not daily_selection_done) or intraday_reselect_due):
                                should_rebalance = True
                    else:
                        pending_regime = ""
                        pending_regime_count = 0
                    last_regime_check_ts = now
                except Exception as regime_exc:
                    _event(state, f"REGIME_CHECK_FALLBACK: {regime_exc}")

            if settings.auto_select_enabled and should_rebalance:
                phase_started_at = time.perf_counter()
                try:
                    if settings.adaptive_edge_enabled:
                        strategy_edge_map = _strategy_edge_from_recent_trades(
                            ledger,
                            days=max(7, int(settings.adaptive_edge_lookback_days)),
                            strength=max(0.0, float(settings.adaptive_edge_strength)),
                        )
                    else:
                        strategy_edge_map = {}
                    if regime == "BEARISH":
                        pool = list(dynamic_candidate_pool)
                        strategy_reference = "약세장 추세 후보"
                    elif regime == "BULLISH":
                        pool = list(dynamic_candidate_pool)
                        strategy_reference = "상승 추세 후보"
                    else:
                        pool = list(dynamic_candidate_pool)
                        strategy_reference = "추세/균형 후보"
                    symbol_sector_map = _resolve_sector_map(
                        symbols=list(dict.fromkeys(pool + selected_symbols + list(ledger.positions.keys()))),
                        manual_map=manual_sector_map,
                        cache_map=sector_cache_map,
                        auto_enabled=bool(getattr(settings, "sector_auto_map_enabled", True)),
                        cache_path=sector_cache_path,
                        fetch_limit=12,
                    )
                    sector_cache_map = dict(symbol_sector_map)

                    ranked: list[tuple[float, str]] = []
                    factor_map: dict[str, dict[str, float]] = {}
                    factor_meta_map: dict[str, dict[str, object]] = {}
                    selection_limit = max(
                        settings.momentum_lookback_days + 25,
                        settings.sizing_vol_lookback_days + 25,
                        settings.atr_exit_lookback_days + 25,
                        60,
                    )
                    for sym in pool:
                        if stop_event.is_set():
                            break
                        snap = _daily_analysis_snapshot(
                            api=api,
                            symbol=sym,
                            limit=selection_limit,
                            market_index_pct=regime_index_pct,
                            settings=settings,
                            cache=daily_analysis_cache,
                        )
                        closes = list(snap.get("closes", []))
                        score = float(snap.get("score", -999.0))
                        factors = dict(snap.get("factors", {}))
                        factor_meta_map[sym] = {
                            "daily_bar_count": int(snap.get("daily_bar_count", len(closes))),
                            "required_daily_bars": int(snap.get("required_daily_bars", 60)),
                            "factor_data_ready": bool(snap.get("factor_data_ready", bool(factors))),
                            "factor_data_reason": str(snap.get("factor_data_reason", "")),
                        }
                        tech_close_cache[sym] = closes
                        if factors:
                            factor_map[sym] = factors
                        vol_cache[sym] = float(snap.get("volatility_pct_decimal", 0.0))
                        atr_cache[sym] = float(snap.get("atr_proxy_pct_decimal", 0.0))
                        ranked.append((score, sym))
                    ranked.sort(reverse=True)

                    old = list(selected_symbols)
                    held_symbols = [
                        sym
                        for sym, row in sorted(
                            ledger.positions.items(),
                            key=lambda item: int((item[1] or {}).get("qty", 0)),
                            reverse=True,
                        )
                        if int((row or {}).get("qty", 0)) > 0
                    ]
                    if ranked:
                        max_slots = max(1, int(settings.trend_select_count))
                        filtered = [(sc, sym) for sc, sym in ranked if sc > -900.0]
                        if filtered:
                            picks = []
                            sector_counts: dict[str, int] = {}
                            for sc, sym in filtered:
                                sector = str(symbol_sector_map.get(sym, sym)).strip() or sym
                                used = int(sector_counts.get(sector, 0))
                                if used >= int(settings.trend_max_sector_names):
                                    continue
                                picks.append((sc, sym))
                                sector_counts[sector] = used + 1
                                if len(picks) >= max_slots:
                                    break
                            if not picks:
                                picks = filtered[:max_slots]
                        else:
                            picks = ranked[:max_slots]
                        if not picks:
                            fallback_seed = list(dict.fromkeys(
                                held_symbols
                                + dynamic_candidate_pool[:max_slots]
                                + static_universe_pool[:max_slots]
                            ))
                            picks = [(0.0, sym) for sym in fallback_seed[:max_slots] if sym]
                        top_sym = picks[0][1] if picks else ""
                        top_mom = float(factor_map.get(top_sym, {}).get("momentum_pct", -999.0)) if top_sym else -999.0
                        fallback_used = False
                        fallback_reason = ""
                        market_status_enabled = bool(getattr(settings, "market_status_filter_enabled", True))
                        if market_status_enabled and regime == "BEARISH" and not filtered and not held_symbols:
                            selected_symbols = []
                            score_map = {}
                            fallback_used = True
                            fallback_reason = (
                                "BEARISH with no eligible long candidates -> capital preservation watchlist only"
                            )
                        elif market_status_enabled and regime == "BEARISH" and top_mom < settings.min_momentum_pct:
                            defensive_ranked = [sym for _, sym in picks if sym]
                            if not defensive_ranked:
                                defensive_ranked = list(dict.fromkeys(
                                    held_symbols
                                    + dynamic_candidate_pool[:max_slots]
                                    + static_universe_pool[:max_slots]
                                ))
                            selected_symbols = list(dict.fromkeys(held_symbols + defensive_ranked[:max_slots]))
                            score_map = {
                                sym: float(factor_map.get(sym, {}).get("score", 0.0))
                                for sym in selected_symbols
                            }
                            for sc, sym in picks:
                                if sym:
                                    score_map[sym] = float(sc)
                            fallback_used = True
                            fallback_reason = (
                                f"BEARISH and top momentum {top_mom:+.2f}% < "
                                f"min_momentum_pct {settings.min_momentum_pct:+.2f}% "
                                f"-> keep ranked defensive basket"
                            )
                        else:
                            ranked_selected = [sym for _, sym in picks]
                            selected_symbols = list(dict.fromkeys(held_symbols + ranked_selected))
                            score_map = {sym: sc for sc, sym in picks}
                            for held_sym in held_symbols:
                                score_map.setdefault(held_sym, float(factor_map.get(held_sym, {}).get("score", 0.0)))
                        ranked_top = filtered[:5] if filtered else picks[:5]
                        analysis_focus_symbols = [
                            str(sym).strip()
                            for _, sym in ranked_top
                            if str(sym).strip()
                        ]
                        analysis_state_symbols = list(dict.fromkeys(selected_symbols + analysis_focus_symbols))
                        factor_map_state = {sym: factor_map.get(sym, {}) for sym in analysis_state_symbols}
                        factor_meta_state = {sym: factor_meta_map.get(sym, {}) for sym in analysis_state_symbols}
                        for sym in analysis_focus_symbols:
                            score_map.setdefault(sym, float(factor_map.get(sym, {}).get("score", -999.0)))
                        symbol_strategy_map = {
                            sym: _entry_plan_for_symbol(
                                regime=regime,
                                factors=factor_map_state.get(sym, {}),
                                base_confirm_cycles=settings.signal_confirm_cycles,
                                strategy_edge_map=strategy_edge_map,
                                market_bias_mode=market_bias_mode,
                                trade_policy="NORMAL",
                                risk_score=0.0,
                                settings=settings,
                            )
                            for sym in analysis_state_symbols
                        }
                        rank_rows: list[dict[str, object]] = []
                        for idx, (sc, sym) in enumerate(ranked_top, start=1):
                            ff = factor_map.get(sym, {})
                            rank_rows.append(
                                {
                                    "rank": idx,
                                    "symbol": sym,
                                    "score": round(float(sc), 3),
                                    "momentum_pct": round(float(ff.get("momentum_pct", 0.0)), 2),
                                    "relative_pct": round(float(ff.get("relative_pct", 0.0)), 2),
                                    "trend_pct": round(float(ff.get("trend_pct", 0.0)), 2),
                                    "volatility_pct": round(float(ff.get("volatility_pct", 0.0)), 2),
                                    "ret5_pct": round(float(ff.get("ret5_pct", 0.0)), 2),
                                    "atr14_pct": round(float(ff.get("atr14_pct", 0.0)), 2),
                                    "daily_rsi": round(float(ff.get("daily_rsi", 50.0)), 1),
                                    "attention_ratio": round(float(ff.get("attention_ratio", 0.0)), 2),
                                    "value_spike_ratio": round(float(ff.get("value_spike_ratio", 0.0)), 2),
                                    "risk_adjusted_momentum": round(float(ff.get("risk_adjusted_momentum", 0.0)), 2),
                                    "risk_adjusted_relative": round(float(ff.get("risk_adjusted_relative", 0.0)), 2),
                                    "trend_efficiency": round(float(ff.get("trend_efficiency", 0.0)), 2),
                                    "participation_quality": round(float(ff.get("participation_quality", 0.0)), 2),
                                    "top_rank_quality_penalty": round(float(ff.get("top_rank_quality_penalty", 0.0)), 2),
                                    "near_high_pct": round(float(ff.get("near_high_pct", 0.0)), 2),
                                    "trend_ok": bool(ff.get("trend_ok", 0.0)),
                                    "structure_ok": bool(ff.get("structure_ok", 0.0)),
                                    "breakout_ok": bool(ff.get("breakout_ok", 0.0)),
                                    "overheat": bool(ff.get("overheat", 0.0)),
                                    "overextended": bool(ff.get("overextended", 0.0)),
                                    "overextension_penalty": round(float(ff.get("overextension_penalty", 0.0)), 3),
                                }
                            )
                        selected_primary = (
                            selected_symbols[0]
                            if selected_symbols
                            else (analysis_focus_symbols[0] if analysis_focus_symbols else settings.symbol)
                        )
                        selected_rank = next(
                            (
                                int(r.get("rank", 0))
                                for r in rank_rows
                                if str(r.get("symbol")) == selected_primary
                            ),
                            1,
                        )
                        selected_factors = factor_map.get(selected_primary, {})
                        selected_score = float(score_map.get(selected_primary, 0.0))
                        selection_reason_text = _build_selection_reason(
                            primary_symbol=selected_primary,
                            regime=regime,
                            strategy_reference=strategy_reference,
                            score=selected_score,
                            factors=selected_factors,
                            rank=selected_rank,
                            total=max(1, len(filtered) if filtered else len(ranked)),
                        )
                        if not selected_symbols and analysis_focus_symbols:
                            selection_reason_text = (
                                "No fresh long selection. "
                                + fallback_reason
                                + f" Watchlist lead={selected_primary}."
                            )
                        selection_detail_state = {
                            "reason": selection_reason_text,
                            "regime": regime,
                            "regime_confidence": round(regime_confidence, 3),
                            "regime_index_pct": round(regime_index_pct, 2),
                            "strategy_reference": strategy_reference,
                            "selected_symbols": list(selected_symbols),
                            "analysis_watch_symbols": list(analysis_focus_symbols),
                            "selected_primary": selected_primary,
                            "selected_rank": selected_rank,
                            "selected_score": round(selected_score, 3),
                            "selected_factor": {
                                "momentum_pct": round(float(selected_factors.get("momentum_pct", 0.0)), 2),
                                "relative_pct": round(float(selected_factors.get("relative_pct", 0.0)), 2),
                                "trend_pct": round(float(selected_factors.get("trend_pct", 0.0)), 2),
                                "volatility_pct": round(float(selected_factors.get("volatility_pct", 0.0)), 2),
                                "ret5_pct": round(float(selected_factors.get("ret5_pct", 0.0)), 2),
                                "atr14_pct": round(float(selected_factors.get("atr14_pct", 0.0)), 2),
                                "daily_rsi": round(float(selected_factors.get("daily_rsi", 50.0)), 1),
                                "attention_ratio": round(float(selected_factors.get("attention_ratio", 0.0)), 2),
                                "value_spike_ratio": round(float(selected_factors.get("value_spike_ratio", 0.0)), 2),
                                "near_high_pct": round(float(selected_factors.get("near_high_pct", 0.0)), 2),
                                "trend_ok": bool(selected_factors.get("trend_ok", 0.0)),
                                "structure_ok": bool(selected_factors.get("structure_ok", 0.0)),
                                "breakout_ok": bool(selected_factors.get("breakout_ok", 0.0)),
                                "overheat": bool(selected_factors.get("overheat", 0.0)),
                                "overextended": bool(selected_factors.get("overextended", 0.0)),
                                "overextension_penalty": round(float(selected_factors.get("overextension_penalty", 0.0)), 3),
                            },
                            "pool_size": len(pool),
                            "ranked_size": len(filtered) if filtered else len(ranked),
                            "top_ranked": rank_rows,
                            "selected_sectors": [
                                {
                                    "symbol": sym,
                                    "sector": str(symbol_sector_map.get(sym, "UNMAPPED")),
                                }
                                for sym in selected_symbols
                            ],
                            "fallback_used": fallback_used,
                            "fallback_reason": fallback_reason,
                            "auto_params": {
                                "enabled": bool(settings.auto_param_tuning_enabled),
                                "strength": round(float(settings.auto_param_tuning_strength), 2),
                                "min_entry_score": round(float(auto_params.get("min_entry_score", settings.min_entry_score)), 3),
                                "min_entry_momentum_pct": round(float(auto_params.get("min_entry_momentum_pct", settings.min_entry_momentum_pct)), 3),
                                "take_profit_partial_ratio": round(float(auto_params.get("take_profit_partial_ratio", settings.take_profit_partial_ratio)), 3),
                                "market_bias_mode": market_bias_mode,
                                "market_bias_reason": market_bias_reason,
                                "market_volatility_pct": round(float(auto_params.get("market_volatility_pct", 0.0)), 3),
                                "risk_bias": round(float(auto_params.get("risk_bias", 0.0)), 3),
                            },
                        }

                        selection_history = _record_selection_history(
                            selection_history,
                            day=current_day,
                            symbols=list(selected_symbols),
                            primary=selected_primary,
                            regime=regime,
                            selection_basis=strategy_reference,
                        )
                        _save_selection_history(selection_history_path, selection_history)
                        history_stats, turnover_pct, turnover_note = _selection_history_stats(
                            selection_history,
                            list(selected_symbols),
                        )
                        selection_detail_state["history_stats"] = history_stats
                        selection_detail_state["turnover_pct"] = round(turnover_pct, 1)
                        selection_detail_state["turnover_note"] = turnover_note

                        last_selection_ts = now
                        last_selection_day = current_day
                        _event(
                            state,
                            "SELECT symbols="
                            + ",".join(selected_symbols)
                            + f" regime={regime} conf={regime_confidence:.2f} idx={regime_index_pct:+.2f}% ref={strategy_reference}",
                        )
                        _event(
                            state,
                            f"DAILY_SELECTION_LOCK day={current_day} phase={session_phase_seed} symbols={','.join(selected_symbols)}",
                        )
                        if selected_symbols:
                            top = selected_symbols[0]
                            f = factor_map.get(top, {})
                            if f:
                                _event(
                                    state,
                                    "SELECT_FACTORS "
                                    + f"{top} mom={f.get('momentum_pct', 0.0):+.2f}% "
                                    + f"rel={f.get('relative_pct', 0.0):+.2f}% "
                                    + f"trend={f.get('trend_pct', 0.0):+.2f}% "
                                    + f"vol={f.get('volatility_pct', 0.0):.2f}% "
                                    + f"score={f.get('score', 0.0):+.2f}",
                                )
                        if old != selected_symbols:
                            _event(state, f"ROTATE_TARGET {','.join(old)} -> {','.join(selected_symbols)}")
                            if _SLACK_NOTIFIER and settings.slack_enabled and settings.selection_change_slack_enabled:
                                added = [sym for sym in selected_symbols if sym not in old]
                                removed = [sym for sym in old if sym not in selected_symbols]
                                ranked_bits = []
                                for item in list(selection_detail_state.get("top_ranked", []))[:5]:
                                    if not isinstance(item, dict):
                                        continue
                                    sym = str(item.get("symbol") or "").strip()
                                    if not sym:
                                        continue
                                    ranked_bits.append(
                                        f"{sym}({float(item.get('score', 0.0)):+.2f})"
                                    )
                                msg = (
                                    "SELECTION_CHANGE "
                                    f"day={current_day} "
                                    f"phase={session_phase_seed} "
                                    f"regime={regime} conf={regime_confidence:.2f} idx={regime_index_pct:+.2f}% "
                                    f"basis={strategy_reference or '-'} "
                                    f"primary={selected_primary} "
                                    f"selected={','.join(selected_symbols) if selected_symbols else '-'} "
                                    f"added={','.join(added) if added else '-'} "
                                    f"removed={','.join(removed) if removed else '-'} "
                                    f"reason={selection_reason_text} "
                                    f"top_ranked={','.join(ranked_bits) if ranked_bits else '-'}"
                                )
                                _SLACK_NOTIFIER.send(msg, force=True)
                                _event(state, f"SELECTION_CHANGE_SENT {current_day} added={','.join(added) if added else '-'} removed={','.join(removed) if removed else '-'}")
                        if held_symbols:
                            _event(state, f"HOLDING_PIN {','.join(held_symbols)}")
                        strategy_sig = "|".join(
                            f"{sym}:{symbol_strategy_map.get(sym, {}).get('profile','')}/{symbol_strategy_map.get(sym, {}).get('sentiment','')}"
                            for sym in selected_symbols
                        )
                        if strategy_sig != last_strategy_signature:
                            last_strategy_signature = strategy_sig
                            _event(state, f"STRATEGY_MAP {strategy_sig}")
                except Exception as select_exc:
                    _event(state, f"SELECT_FALLBACK: {select_exc}")
                _record_perf_phase(perf_profile, "selection_sec", phase_started_at)

            should_refresh_vol = (now - last_vol_refresh_ts) >= max(300, settings.rebalance_minutes * 30)
            if should_refresh_vol or should_rebalance:
                phase_started_at = time.perf_counter()
                vol_targets = list(dict.fromkeys(selected_symbols + list(ledger.positions.keys())))
                if not vol_targets:
                    vol_targets = list(dict.fromkeys(analysis_focus_symbols))
                vol_limit = max(
                    settings.sizing_vol_lookback_days + 25,
                    settings.atr_exit_lookback_days + 25,
                    60,
                )
                for sym in vol_targets:
                    if stop_event.is_set():
                        break
                    snap = _daily_analysis_snapshot(
                        api=api,
                        symbol=sym,
                        limit=vol_limit,
                        market_index_pct=regime_index_pct,
                        settings=settings,
                        cache=daily_analysis_cache,
                    )
                    closes = list(snap.get("closes", []))
                    tech_close_cache[sym] = closes
                    vol_cache[sym] = float(snap.get("volatility_pct_decimal", 0.0))
                    atr_cache[sym] = float(snap.get("atr_proxy_pct_decimal", 0.0))
                    if sym not in factor_meta_state:
                        factor_meta_state[sym] = {
                            "daily_bar_count": int(snap.get("daily_bar_count", len(closes))),
                            "required_daily_bars": int(snap.get("required_daily_bars", 60)),
                            "factor_data_ready": bool(snap.get("factor_data_ready", False)),
                            "factor_data_reason": str(snap.get("factor_data_reason", "")),
                        }
                last_vol_refresh_ts = now
                _record_perf_phase(perf_profile, "vol_refresh_sec", phase_started_at)

            # Keep holdings in the selected basket until fully sold.
            # Exit decisions for held names should come from signal/risk rules only.

            watch_symbols = list(dict.fromkeys(selected_symbols + analysis_focus_symbols + list(ledger.positions.keys())))
            price_map: dict[str, float] = {}
            phase_started_at = time.perf_counter()
            for sym in watch_symbols:
                if stop_event.is_set():
                    break
                try:
                    quote = api.get_quote(sym)
                    px = float(quote.get("price", 0.0))
                    vol = int(quote.get("volume", 0))
                    price_map[sym] = px
                    quote_volume_map[sym] = max(0, vol)
                    if sym not in quote_volume_hist:
                        quote_volume_hist[sym] = deque(maxlen=60)
                    if vol > 0:
                        quote_volume_hist[sym].append(vol)
                    quote_ts_map[sym] = time.time()
                except Exception as px_exc:
                    _event(state, f"PRICE_FALLBACK {sym}: {px_exc}")
            _record_perf_phase(perf_profile, "quote_fetch_sec", phase_started_at)

            if not price_map:
                state.stale_data_active = True
                state.stale_data_reason = "price_map empty"
                state.data_freshness_sec = float(settings.stale_data_max_age_sec)
                stop_event.wait(settings.poll_seconds)
                continue

            age_map: dict[str, float] = {}
            stale_symbols: set[str] = set()
            for sym in watch_symbols:
                ts = quote_ts_map.get(sym, 0.0)
                age = (time.time() - ts) if ts > 0 else float(settings.stale_data_max_age_sec + 999)
                age_map[sym] = age
                if age > settings.stale_data_max_age_sec:
                    stale_symbols.add(sym)
            data_freshness_sec = max(age_map.values()) if age_map else 0.0
            stale_data_active = len(stale_symbols) > 0
            stale_reason = (
                "stale: " + ",".join(sorted(stale_symbols))
                if stale_symbols
                else ""
            )

            equity_pre, _, _, _ = _mark_to_market(ledger, price_map)
            heat_pct_pre = _portfolio_heat_pct(ledger, price_map, vol_cache, equity_pre)
            daily_return_pre = _period_return(ledger.equity_history, equity_pre, 1)
            today = datetime.now().strftime("%Y-%m-%d")
            if daily_halt_day != today:
                daily_halt_day = ""
                daily_halt_notified = False
            if daily_return_pre <= settings.daily_loss_limit_pct:
                if daily_halt_day != today:
                    daily_halt_day = today
                if not daily_halt_notified:
                    _event(
                        state,
                        "RISK_HALT daily_loss_limit "
                        f"{daily_return_pre:.2f}% <= {settings.daily_loss_limit_pct:.2f}%",
                    )
                    daily_halt_notified = True
            risk_halt_active = (daily_halt_day == today)
            heat_bucket = int(heat_pct_pre // 5)
            if heat_pct_pre > settings.max_portfolio_heat_pct and heat_bucket != last_heat_alert_bucket:
                _event(
                    state,
                    f"RISK_HEAT {heat_pct_pre:.2f}% > {settings.max_portfolio_heat_pct:.2f}%",
                )
                last_heat_alert_bucket = heat_bucket

            state.loop_count += 1
            primary_symbol = (
                selected_symbols[0]
                if selected_symbols
                else (analysis_focus_symbols[0] if analysis_focus_symbols else settings.symbol)
            )
            stock_statuses: list[dict[str, object]] = []
            reason_counter: dict[str, int] = {}
            top_ranked_focus_symbols = [
                str(item.get("symbol") or "").strip()
                for item in list(selection_detail_state.get("top_ranked", []))[:5]
                if isinstance(item, dict) and str(item.get("symbol") or "").strip()
            ]
            focus_symbols_seed = list(
                dict.fromkeys(selected_symbols + analysis_focus_symbols + top_ranked_focus_symbols + list(ledger.positions.keys()))
            )
            vol_values_pct = [
                max(0.0, float(vol_cache.get(sym, 0.0)) * 100.0)
                for sym in watch_symbols
                if float(vol_cache.get(sym, 0.0)) > 0.0
            ]
            market_volatility_pct = float(statistics.mean(vol_values_pct)) if vol_values_pct else 2.0
            market_status_enabled = bool(getattr(settings, "market_status_filter_enabled", True))
            trade_regime = regime if market_status_enabled else "NEUTRAL"
            trade_regime_confidence = regime_confidence if market_status_enabled else 0.0
            trade_regime_index_pct = regime_index_pct if market_status_enabled else 0.0
            auto_params = _auto_tuned_entry_params(
                settings,
                regime=trade_regime,
                regime_confidence=trade_regime_confidence,
                regime_index_pct=trade_regime_index_pct,
                market_volatility_pct=market_volatility_pct,
            )
            auto_sig = (
                f"{trade_regime}|{trade_regime_confidence:.2f}|{trade_regime_index_pct:+.2f}|"
                f"{auto_params['min_entry_score']:.2f}|{auto_params['min_entry_momentum_pct']:.2f}|"
                f"{auto_params['take_profit_partial_ratio']:.2f}|{auto_params['market_volatility_pct']:.2f}"
            )
            market_bias_mode, market_bias_reason = _market_bias_mode(
                regime=trade_regime,
                regime_confidence=trade_regime_confidence,
                regime_index_pct=trade_regime_index_pct,
                market_volatility_pct=market_volatility_pct,
            )
            session_ctx = _krx_session_context(
                settings,
                now_kst=now_kst,
                regime_index_pct=trade_regime_index_pct,
                market_volatility_pct=market_volatility_pct,
            )
            if not market_status_enabled:
                session_ctx["shock_active"] = False
                session_ctx["shock_reason"] = ""
                session_ctx["trade_policy"] = "NORMAL"
                session_ctx["risk_score"] = 0.0
                session_ctx["event_profile"] = "NONE"
                session_ctx["diag"] = str(session_ctx.get("diag", "")).replace(" shock=1", " shock=0")
            if bool(session_ctx.get("shock_active", False)):
                market_bias_mode = "DEFENSIVE"
                market_bias_reason = "shock_guard:" + str(session_ctx.get("shock_reason") or "active")
            flow_snapshot_map: dict[str, dict[str, float]] = {}
            flow_summary_text = "-"
            vi_active_symbols: set[str] = set()
            if str(session_ctx.get("phase", "OFF_HOURS")) in {"OPENING_FOCUS", "REGULAR_SESSION", "CLOSE_GUARD"}:
                flow_snapshot_map, flow_summary_text, vi_active_symbols = _fetch_market_microstructure(
                    api,
                    focus_symbols_seed[:5],
                )
            if auto_sig != last_auto_param_signature:
                last_auto_param_signature = auto_sig
                _event(
                    state,
                    "AUTO_PARAMS "
                    + f"regime={trade_regime} conf={trade_regime_confidence:.2f} idx={trade_regime_index_pct:+.2f}% "
                    + f"mvol={auto_params['market_volatility_pct']:.2f}% "
                    + f"min_score={auto_params['min_entry_score']:.2f} "
                    + f"min_mom={auto_params['min_entry_momentum_pct']:.2f}% "
                    + f"tp_partial={auto_params['take_profit_partial_ratio']:.2f} "
                    + f"risk_bias={auto_params['risk_bias']:+.2f}",
                )
            bias_sig = f"{market_bias_mode}|{market_bias_reason}"
            if bias_sig != last_market_bias_signature:
                last_market_bias_signature = bias_sig
                _event(state, f"MARKET_BIAS mode={market_bias_mode} reason={market_bias_reason}")
            session_sig = (
                f"{session_ctx.get('phase')}|{session_ctx.get('profile')}|"
                f"{session_ctx.get('trade_policy')}|{float(session_ctx.get('risk_score', 0.0)):.1f}|"
                f"{int(bool(session_ctx.get('allow_buy')))}|{int(bool(session_ctx.get('allow_sell')))}|"
                f"{int(bool(session_ctx.get('shock_active')))}"
            )
            if session_sig != last_session_signature:
                last_session_signature = session_sig
                _event(state, f"SESSION_MODE {session_ctx.get('diag')} shock_reason={session_ctx.get('shock_reason') or '-'}")

            phase_started_at = time.perf_counter()
            bar_dt = now_kst.replace(second=0, microsecond=0)
            bar_minutes = max(1, int(settings.bar_interval_minutes))
            bar_dt = bar_dt.replace(minute=(bar_dt.minute // bar_minutes) * bar_minutes)
            decision_bar_ts = bar_dt.strftime("%Y-%m-%d %H:%M:%S")
            bar_decision_due = (decision_bar_ts != last_decision_bar_ts)
            for sym in watch_symbols:
                current_price = price_map.get(sym)
                if current_price is None:
                    continue
                prev_price = prev_prices.get(sym, 0.0)
                price_change_pct = (
                    ((current_price - prev_price) / prev_price) * 100.0 if prev_price > 0 else 0.0
                )
                factor_row = factor_map_state.get(sym, {})
                symbol_vol = float(vol_cache.get(sym, 0.0))
                live_setup_state, live_factor_row = _classify_setup_state_live(
                    base_factors=factor_row,
                    price_change_pct=price_change_pct,
                    volatility_pct=(symbol_vol * 100.0),
                )
                strategy_plan = _entry_plan_for_symbol(
                    regime=trade_regime,
                    factors=live_factor_row,
                    base_confirm_cycles=settings.signal_confirm_cycles,
                    strategy_edge_map=strategy_edge_map,
                    market_bias_mode=market_bias_mode,
                    trade_policy=str(session_ctx.get("trade_policy", "NORMAL")),
                    risk_score=float(session_ctx.get("risk_score", 0.0)),
                    settings=settings,
                )
                symbol_strategy_map[sym] = strategy_plan
                symbol_enable_trend_entry = bool(strategy_plan.get("enable_trend_entry", False))
                symbol_entry_mode = str(strategy_plan.get("entry_mode", "균형(Balanced)"))
                symbol_setup_state = str(strategy_plan.get("setup_state", live_setup_state or "NEUTRAL"))
                symbol_edge = float(strategy_plan.get("edge", 0.0))
                effective_min_entry_score = float(
                    min_entry_score_map.get(sym, auto_params["min_entry_score"])
                )
                effective_min_entry_momentum = float(
                    min_entry_momentum_map.get(sym, auto_params["min_entry_momentum_pct"])
                )
                policy = str(session_ctx.get("trade_policy", "NORMAL") or "NORMAL").strip().upper()
                if policy == "CAUTION":
                    effective_min_entry_score += float(getattr(settings, "market_policy_caution_entry_score_boost", 0.08))
                    effective_min_entry_momentum += float(
                        getattr(settings, "market_policy_caution_entry_momentum_boost_pct", 0.20)
                    )
                elif policy == "HALT":
                    effective_min_entry_score += float(getattr(settings, "market_policy_halt_entry_score_boost", 0.20))
                    effective_min_entry_momentum += float(
                        getattr(settings, "market_policy_halt_entry_momentum_boost_pct", 0.60)
                    )
                effective_tp_partial_ratio = float(
                    take_profit_partial_ratio_map.get(sym, auto_params["take_profit_partial_ratio"])
                )
                row = ledger.positions.get(sym, {"qty": 0.0, "avg_price": 0.0, "peak_price": 0.0})
                qty = int(row.get("qty", 0))
                avg = float(row.get("avg_price", 0.0))
                peak = float(row.get("peak_price", 0.0))
                prev_daily_close = float(tech_close_cache.get(sym, [0.0])[-1]) if tech_close_cache.get(sym) else 0.0
                gap_from_prev_close_pct = _pct_change(current_price, prev_daily_close) if prev_daily_close > 0 else 0.0
                daily_rsi = float(factor_row.get("daily_rsi", 50.0))
                trend_ok = bool(factor_row.get("trend_ok", 0.0))
                structure_ok = bool(factor_row.get("structure_ok", 0.0))
                breakout_ok = bool(factor_row.get("breakout_ok", 0.0))
                overheat_flag = bool(factor_row.get("overheat", 0.0))
                attention_ratio = float(factor_row.get("attention_ratio", 0.0))
                value_spike_ratio = float(factor_row.get("value_spike_ratio", 0.0))

                tech_flags = _technical_signal_flags(
                    closes=tech_close_cache.get(sym, []),
                    current_price=current_price,
                    current_volume=int(quote_volume_map.get(sym, 0)),
                    recent_volumes=list(quote_volume_hist.get(sym, deque())),
                )
                trend_up = bool(tech_flags.get("trend_up", False))
                trend_down = bool(tech_flags.get("trend_down", False))
                trend_diag = trend_runtime_diagnostics(
                    qty=qty,
                    trend_ok=trend_ok,
                    structure_ok=structure_ok,
                    breakout_ok=breakout_ok,
                    overheat_flag=overheat_flag,
                    daily_rsi=daily_rsi,
                    attention_ratio=attention_ratio,
                    value_spike_ratio=value_spike_ratio,
                    gap_from_prev_close_pct=gap_from_prev_close_pct,
                    trend_daily_rsi_min=settings.trend_daily_rsi_min,
                    trend_daily_rsi_max=settings.trend_daily_rsi_max,
                    trend_min_turnover_ratio_5_to_20=settings.trend_min_turnover_ratio_5_to_20,
                    trend_min_value_spike_ratio=settings.trend_min_value_spike_ratio,
                    trend_gap_skip_up_pct=settings.trend_gap_skip_up_pct,
                    trend_gap_skip_down_pct=settings.trend_gap_skip_down_pct,
                    trend_max_chase_from_open_pct=settings.trend_max_chase_from_open_pct,
                    market_chg_pct=float(regime_index_pct),
                    momentum_pct=float(live_factor_row.get("momentum_pct", 0.0)),
                    trend_pct=float(live_factor_row.get("trend_pct", 0.0)),
                    tech_flags=tech_flags,
                    prev_price=float(prev_prices.get(sym, prev_daily_close or current_price)),
                    current_price=current_price,
                    atr_pct=float(factor_row.get("atr14_pct", 0.0)),
                    volume_ratio=float(factor_row.get("value_spike_ratio", 0.0)),
                )
                
                # Check scalping signals first if in scalping mode
                live_scalping_phase = str(session_ctx.get("phase", "OFF_HOURS")) in {
                    "OPENING_FOCUS",
                    "REGULAR_SESSION",
                    "CLOSE_GUARD",
                }
                scalping_action, scalping_priority, scalping_data_source = _scalping_decision(
                    symbol=sym,
                    current_price=current_price,
                    qty=qty,
                    entry_price=float(row.get("avg_price", 0.0)),
                    hold_bars=int(row.get("hold_bars", 0)),
                    settings=settings,
                    api=api,
                    prefer_live=live_scalping_phase,
                    trade_policy=str(session_ctx.get("trade_policy", "NORMAL")),
                    risk_score=float(session_ctx.get("risk_score", 0.0)),
                )
                if _is_scalping_mode(settings):
                    _event(
                        state,
                        f"DATA_SOURCE {sym} source={scalping_data_source} phase={session_ctx.get('phase', 'OFF_HOURS')} prefer_live={int(live_scalping_phase)}",
                    )
                
                if scalping_action != "HOLD":
                    action = scalping_action
                    tech_priority_signal = scalping_priority
                    trend_entry_ready = True  # Scalping overrides trend filters
                else:
                    action, tech_priority_signal, trend_entry_ready = trend_runtime_signal(
                        qty=qty,
                        trend_ok=trend_ok,
                        structure_ok=structure_ok,
                    breakout_ok=breakout_ok,
                    overheat_flag=overheat_flag,
                    daily_rsi=daily_rsi,
                    attention_ratio=attention_ratio,
                    value_spike_ratio=value_spike_ratio,
                    gap_from_prev_close_pct=gap_from_prev_close_pct,
                    trend_daily_rsi_min=settings.trend_daily_rsi_min,
                    trend_daily_rsi_max=settings.trend_daily_rsi_max,
                    trend_min_turnover_ratio_5_to_20=settings.trend_min_turnover_ratio_5_to_20,
                    trend_min_value_spike_ratio=settings.trend_min_value_spike_ratio,
                    trend_gap_skip_up_pct=settings.trend_gap_skip_up_pct,
                    trend_gap_skip_down_pct=settings.trend_gap_skip_down_pct,
                    trend_max_chase_from_open_pct=settings.trend_max_chase_from_open_pct,
                    market_chg_pct=float(regime_index_pct),
                    momentum_pct=float(live_factor_row.get("momentum_pct", 0.0)),
                    trend_pct=float(live_factor_row.get("trend_pct", 0.0)),
                    tech_flags=tech_flags,
                    golden_cross_entry_bb_max=TECH_GC_ENTRY_BB_MAX,
                    prev_price=float(prev_prices.get(sym, prev_daily_close or current_price)),
                    current_price=current_price,
                    atr_pct=float(factor_row.get("atr14_pct", 0.0)),
                    volume_ratio=float(factor_row.get("value_spike_ratio", 0.0)),
                )
                bearish_long_ok = False
                if bool(getattr(settings, "enable_bearish_exception", False)):
                    bearish_long_ok = bearish_long_exception_ready(
                        trend_ok=trend_ok,
                        structure_ok=structure_ok,
                        breakout_ok=breakout_ok,
                        daily_rsi=daily_rsi,
                        attention_ratio=attention_ratio,
                        value_spike_ratio=value_spike_ratio,
                        momentum_pct=float(live_factor_row.get("momentum_pct", 0.0)),
                        trend_pct=float(live_factor_row.get("trend_pct", 0.0)),
                        tech_flags=tech_flags,
                    )
                bearish_exception_market = market_status_enabled and (
                    float(regime_index_pct) <= float(settings.bearish_exception_trigger_pct)
                )
                if qty <= 0 and bearish_exception_market and action == "HOLD" and bearish_long_ok:
                    action = "BUY"
                    tech_priority_signal = True
                raw_signal = action
                strategy_type = "scalping" if _is_scalping_mode(settings) else ("trend" if symbol_enable_trend_entry else "pullback")
                reason_parts: list[str] = [
                    f"chg={price_change_pct:+.2f}%",
                    f"gap={gap_from_prev_close_pct:+.2f}%",
                    f"sig={raw_signal}",
                    f"entry_mode={strategy_type}",
                    f"market_bias={market_bias_mode}",
                    f"edge={symbol_edge:+.2f}",
                    (
                        "filters="
                        + f"trend:{1 if trend_ok else 0}/"
                        + f"struct:{1 if structure_ok else 0}/"
                        + f"breakout:{1 if breakout_ok else 0}/"
                        + f"overheat:{1 if overheat_flag else 0}"
                    ),
                    (
                        "tech="
                        + f"gc={1 if bool(tech_flags.get('golden_cross')) else 0}/"
                        + f"dc={1 if bool(tech_flags.get('death_cross')) else 0}/"
                        + f"bot={1 if bool(tech_flags.get('short_bottom')) else 0}/"
                        + f"top={1 if bool(tech_flags.get('short_top')) else 0}/"
                        + f"vspk={1 if bool(tech_flags.get('volume_spike')) else 0}/"
                        + f"tup={1 if trend_up else 0}/"
                        + f"tdn={1 if trend_down else 0}"
                    ),
                    f"rsi={daily_rsi:.1f}",
                    f"attn={attention_ratio:.2f}",
                    f"spike={value_spike_ratio:.2f}",
                ]
                confirm_needed = max(1, int(strategy_plan.get("confirm_needed", settings.signal_confirm_cycles)))
                if tech_priority_signal and raw_signal in {"BUY", "SELL"}:
                    confirm_needed = 1
                    reason_parts.append("tech_fast_confirm")
                confirm_progress = 0
                prefer_partial_sell = False
                risk_exit_signal = False
                if qty <= 0 and raw_signal == "HOLD":
                    if not trend_entry_ready:
                        diag_blockers = list(trend_diag.get("blockers") or [])
                        if diag_blockers:
                            reason_parts.extend(f"blocked:{item}" for item in diag_blockers[:3])
                        else:
                            reason_parts.append("blocked:trend_entry_filter")
                        if bool(trend_diag.get("watchlist", False)):
                            reason_parts.append(f"watch:{str(trend_diag.get('watch_reason') or 'candidate')}")
                    else:
                        reason_parts.append("blocked:pullback_entry_wait")
                elif qty <= 0 and raw_signal != "BUY":
                    reason_parts.append("blocked:quality_gate")

                if qty > 0:
                    row["peak_price"] = max(peak, current_price)
                    ledger.positions[sym] = row

                force_sell_all = False
                if qty > 0 and avg > 0:
                    current_risk = _regime_risk_profile(trade_regime)
                    regime_stop_atr = max(
                        float(current_risk["stop_atr"]),
                        float(row.get("entry_stop_atr", current_risk["stop_atr"])),
                    )
                    regime_take_atr = max(
                        float(current_risk["take_atr"]),
                        float(row.get("entry_take_atr", current_risk["take_atr"])),
                    )
                    regime_trailing_atr = max(
                        float(current_risk["trailing_atr"]),
                        float(row.get("entry_trailing_atr", current_risk["trailing_atr"])),
                    )
                    regime_stop_floor_pct = max(
                        float(current_risk["stop_floor_pct"]),
                        float(row.get("entry_stop_floor_pct", current_risk["stop_floor_pct"])),
                    )
                    regime_take_floor_pct = max(
                        float(current_risk["take_floor_pct"]),
                        float(row.get("entry_take_floor_pct", current_risk["take_floor_pct"])),
                    )
                    position_return_pct = ((current_price - avg) / avg) * 100.0
                    peak_val = max(float(row.get("peak_price", 0.0)), current_price)
                    trailing_drawdown_pct = ((current_price - peak_val) / peak_val) * 100.0 if peak_val > 0 else 0.0
                    held_weekdays = _held_weekdays_since(str(row.get("entry_ts", "")), now_kst.replace(tzinfo=None))
                    atr_pct = float(atr_cache.get(sym, 0.0))
                    dyn_stop_loss_pct = -max(regime_stop_floor_pct, atr_pct * 100.0 * regime_stop_atr)
                    dyn_take_profit_pct = max(
                        regime_take_floor_pct,
                        atr_pct * 100.0 * regime_take_atr,
                    )
                    dyn_trailing_stop_pct = max(
                        abs(settings.trailing_stop_pct),
                        atr_pct * 100.0 * regime_trailing_atr,
                    )
                    quick_take_ready = (
                        position_return_pct >= 2.5
                        and (
                            daily_rsi >= 68.0
                            or float(tech_flags.get("bb_pos", 0.0)) >= 0.88
                            or bool(tech_flags.get("short_top", False))
                        )
                    )
                    fast_fail_exit_ready = (
                        held_weekdays <= 1
                        and position_return_pct <= -2.4
                        and (
                            trend_down
                            or bool(tech_flags.get("short_top", False))
                            or raw_signal == "SELL"
                            or float(tech_flags.get("bb_pos", 0.0)) <= 0.55
                        )
                    )
                    breakout_fail_fast_ready = (
                        held_weekdays <= 1
                        and position_return_pct <= -1.8
                        and (
                            raw_signal == "SELL"
                            or trend_down
                            or float(tech_flags.get("bb_pos", 0.0)) <= 0.62
                        )
                        and (
                            float(metrics.get("daily_rsi", 50.0)) < 60.0
                            or not bool(tech_flags.get("volume_spike", False))
                        )
                    )
                    upper_band_reversal_fail_ready = (
                        held_weekdays <= 1
                        and position_return_pct <= -1.2
                        and float(tech_flags.get("bb_pos", 0.0)) >= 0.95
                        and (
                            raw_signal == "SELL"
                            or trend_down
                            or bool(tech_flags.get("short_top", False))
                        )
                    )
                    hold_exit_ready = (
                        (held_weekdays >= 2 and position_return_pct >= 0.8 and float(tech_flags.get("bb_pos", 0.0)) >= 0.78)
                        or (held_weekdays >= 3 and position_return_pct >= 0.2)
                    )
                    if position_return_pct <= settings.max_symbol_loss_pct:
                        action = "SELL"
                        risk_exit_signal = True
                        force_sell_all = True
                        reason_parts.append(f"symbol_loss_cap({position_return_pct:.2f}%)")
                        _event(state, f"RISK_EXIT symbol_loss_cap {sym} ({position_return_pct:.2f}%)")
                    elif position_return_pct <= dyn_stop_loss_pct:
                        action = "SELL"
                        risk_exit_signal = True
                        force_sell_all = True
                        reason_parts.append(
                            f"stop_loss_dyn({position_return_pct:.2f}%<={dyn_stop_loss_pct:.2f}%)"
                        )
                        _event(
                            state,
                            f"RISK_EXIT stop_loss_dyn {sym} ({position_return_pct:.2f}% <= {dyn_stop_loss_pct:.2f}%)",
                        )
                    elif position_return_pct >= dyn_take_profit_pct:
                        action = "SELL"
                        risk_exit_signal = True
                        prefer_partial_sell = True
                        reason_parts.append(
                            f"take_profit_dyn({position_return_pct:.2f}%>={dyn_take_profit_pct:.2f}%)"
                        )
                        _event(
                            state,
                            f"RISK_EXIT take_profit_dyn {sym} ({position_return_pct:.2f}% >= {dyn_take_profit_pct:.2f}%)",
                        )
                    elif quick_take_ready:
                        action = "SELL"
                        risk_exit_signal = True
                        force_sell_all = True
                        reason_parts.append(f"quick_take({position_return_pct:.2f}%)")
                        _event(
                            state,
                            f"RISK_EXIT quick_take {sym} ({position_return_pct:.2f}%)",
                        )
                    elif fast_fail_exit_ready:
                        action = "SELL"
                        risk_exit_signal = True
                        force_sell_all = True
                        reason_parts.append(f"fast_fail({position_return_pct:.2f}%)")
                        _event(
                            state,
                            f"RISK_EXIT fast_fail {sym} ({position_return_pct:.2f}%)",
                        )
                    elif breakout_fail_fast_ready:
                        action = "SELL"
                        risk_exit_signal = True
                        force_sell_all = True
                        reason_parts.append(f"breakout_fail_fast({position_return_pct:.2f}%)")
                        _event(
                            state,
                            f"RISK_EXIT breakout_fail_fast {sym} ({position_return_pct:.2f}%)",
                        )
                    elif upper_band_reversal_fail_ready:
                        action = "SELL"
                        risk_exit_signal = True
                        force_sell_all = True
                        reason_parts.append(f"upper_band_reversal({position_return_pct:.2f}%)")
                        _event(
                            state,
                            f"RISK_EXIT upper_band_reversal {sym} ({position_return_pct:.2f}%)",
                        )
                    elif hold_exit_ready:
                        action = "SELL"
                        risk_exit_signal = True
                        force_sell_all = True
                        reason_parts.append(f"hold_exit({held_weekdays}d,{position_return_pct:.2f}%)")
                        _event(
                            state,
                            f"RISK_EXIT hold_exit {sym} ({held_weekdays}d, {position_return_pct:.2f}%)",
                        )
                    elif trailing_drawdown_pct <= -dyn_trailing_stop_pct:
                        action = "SELL"
                        risk_exit_signal = True
                        force_sell_all = True
                        reason_parts.append(
                            f"trailing_stop_dyn({trailing_drawdown_pct:.2f}%<=-{dyn_trailing_stop_pct:.2f}%)"
                        )
                        _event(
                            state,
                            f"RISK_EXIT trailing_stop_dyn {sym} ({trailing_drawdown_pct:.2f}% <= -{dyn_trailing_stop_pct:.2f}%)",
                        )

                shock_exception_ok = (
                    bearish_exception_market
                    and bearish_long_ok
                    and float(regime_index_pct) > float(settings.bearish_exception_max_market_drop_pct)
                    and float(market_volatility_pct) < float(settings.bearish_exception_max_vol_pct)
                )
                if bearish_exception_market and action == "BUY":
                    if shock_exception_ok:
                        confirm_needed = max(confirm_needed, 2)
                        reason_parts.append("bearish_exception_long")
                    else:
                        action = "HOLD"
                        reason_parts.append("blocked:bearish_regime")
                if action == "BUY" and not bool(session_ctx.get("allow_buy", False)) and not shock_exception_ok:
                    action = "HOLD"
                    reason_parts.append(f"blocked:session({session_ctx.get('phase')})")
                if action == "BUY" and str(session_ctx.get("trade_policy", "NORMAL")).upper() == "HALT":
                    action = "HOLD"
                    reason_parts.append("blocked:policy_halt")
                if action == "SELL" and not bool(session_ctx.get("allow_sell", False)):
                    action = "HOLD"
                    reason_parts.append(f"blocked:session_sell({session_ctx.get('phase')})")
                if action == "BUY":
                    loss_block_left = int(max(0.0, loss_reentry_block_until.get(sym, 0.0) - time.time()))
                    if loss_block_left > 0:
                        action = "HOLD"
                        reason_parts.append(f"blocked:loss_cooldown({loss_block_left}s)")
                    symbol_score = float(score_map.get(sym, 0.0))
                    symbol_momentum = float(live_factor_row.get("momentum_pct", 0.0))
                    if symbol_score < effective_min_entry_score:
                        action = "HOLD"
                        reason_parts.append(
                            f"blocked:entry_score({symbol_score:.2f}<{effective_min_entry_score:.2f})"
                        )
                    if symbol_momentum < effective_min_entry_momentum:
                        action = "HOLD"
                        reason_parts.append(
                            f"blocked:entry_mom({symbol_momentum:.2f}%<{effective_min_entry_momentum:.2f}%)"
                        )
                if risk_halt_active and action == "BUY":
                    action = "HOLD"
                    reason_parts.append("blocked:risk_halt")
                if stale_data_active and sym in stale_symbols and action == "BUY":
                    action = "HOLD"
                    reason_parts.append(f"blocked:stale({int(age_map.get(sym, 0.0))}s)")
                if heat_pct_pre > settings.max_portfolio_heat_pct and action == "BUY":
                    action = "HOLD"
                    reason_parts.append(f"blocked:portfolio_heat({heat_pct_pre:.1f}%)")

                if (
                    bool(getattr(settings, "decision_on_bar_close_only", True))
                    and (not bar_decision_due)
                    and action in {"BUY", "SELL"}
                    and (not risk_exit_signal)
                ):
                    action = "HOLD"
                    reason_parts.append(f"blocked:bar_wait({decision_bar_ts})")

                base_qty = max(1, settings.position_size)
                order_qty = base_qty
                is_a_grade_opening = False
                if action == "BUY":
                    is_a_grade_opening = (
                        float(flow_snapshot_map.get(sym, {}).get("foreign_net_qty", 0.0) or 0.0) > 0.0
                        and float(flow_snapshot_map.get(sym, {}).get("institution_net_qty", 0.0) or 0.0) > 0.0
                        and (sym not in vi_active_symbols)
                        and not (
                            {
                                str(x or "").split(":", 1)[-1].split("(", 1)[0].strip().lower()
                                for x in list(trend_diag.get("blockers") or [])
                                if str(x).strip()
                            }
                            & {
                                "late_chase",
                                "mid_band_late_chase",
                                "high_rsi_upper_band",
                                "market_surge_chase",
                                "strong_overextension",
                                "overextended_continuation",
                            }
                        )
                    )
                    if is_a_grade_opening:
                        reason_parts.append("tag:a_grade_opening")
                    order_qty = _risk_based_order_qty(
                        equity=equity_pre,
                        cash=ledger.cash,
                        price=current_price,
                        volatility_pct=symbol_vol,
                        fallback_qty=base_qty,
                        target_risk_per_trade_pct=settings.trend_risk_per_trade_pct,
                        max_capital_per_name_pct=settings.trend_capital_per_name_pct,
                    )
                    if market_status_enabled and regime == "BEARISH" and bearish_long_ok:
                        order_qty = max(1, int(order_qty * 0.5))
                        reason_parts.append("bearish_half_size")
                    policy = str(session_ctx.get("trade_policy", "NORMAL") or "NORMAL").strip().upper()
                    risk_score = max(0.0, min(100.0, float(session_ctx.get("risk_score", 0.0))))
                    if policy == "CAUTION":
                        order_qty = max(1, int(round(order_qty * 0.70)))
                        reason_parts.append("policy_size:0.70")
                    elif policy == "HALT":
                        order_qty = max(1, int(round(order_qty * 0.40)))
                        reason_parts.append("policy_size:0.40")
                    elif risk_score >= 60.0:
                        order_qty = max(1, int(round(order_qty * 0.85)))
                        reason_parts.append("risk_size:0.85")
                    reason_parts.append(f"vol={symbol_vol*100:.2f}%")
                if action == "SELL" and qty > 0:
                    if force_sell_all:
                        order_qty = qty
                    elif prefer_partial_sell:
                        partial_ratio = max(0.1, min(1.0, effective_tp_partial_ratio))
                        order_qty = max(1, min(qty, int(round(qty * partial_ratio))))
                        reason_parts.append(f"tp_partial({partial_ratio:.2f})")
                    else:
                        order_qty = min(base_qty, qty)
                if action == "SELL" and qty <= 0:
                    action = "HOLD"
                    reason_parts.append("blocked:no_position")
                if action == "BUY" and qty <= 0 and _position_count(ledger) >= settings.max_active_positions:
                    action = "HOLD"
                    reason_parts.append("blocked:max_active_positions")

                cooldown_left = int(
                    max(0.0, settings.trade_cooldown_sec - (time.time() - last_trade_ts.get(sym, 0.0)))
                )
                if action in {"BUY", "SELL"} and cooldown_left > 0:
                    action = "HOLD"
                    reason_parts.append(f"blocked:cooldown({cooldown_left}s)")
                if action == "BUY" and order_qty <= 0:
                    action = "HOLD"
                    reason_parts.append("blocked:insufficient_cash")

                # Only executable signals build confirmation streaks.
                if raw_signal in {"BUY", "SELL"} and action in {"BUY", "SELL"} and not force_sell_all:
                    prev_sig, prev_cnt = signal_streak.get(sym, ("", 0))
                    now_cnt = (prev_cnt + 1) if prev_sig == raw_signal else 1
                    signal_streak[sym] = (raw_signal, now_cnt)
                    confirm_progress = now_cnt
                    if now_cnt < confirm_needed:
                        action = "HOLD"
                        reason_parts.append(f"blocked:confirm({now_cnt}/{confirm_needed})")
                    else:
                        reason_parts.append(f"confirm:{now_cnt}/{confirm_needed}")
                else:
                    signal_streak[sym] = ("", 0)
                    confirm_progress = 0

                if action in {"BUY", "SELL"} and state.order_count < settings.max_daily_orders:
                    order_ok = False
                    intended_action = action
                    order_id = _mk_order_id()
                    qty_before = qty
                    expected_qty_after = qty_before + order_qty if action == "BUY" else max(0, qty_before - order_qty)
                    ai_sleeve = _infer_ai_sleeve(
                        entry_mode=symbol_entry_mode,
                        setup_state=symbol_setup_state,
                        strategy_mode=str(getattr(settings, "strategy_mode", "")),
                    )
                    ai_sleeve_reason = _infer_ai_sleeve_reason(
                        entry_mode=symbol_entry_mode,
                        setup_state=symbol_setup_state,
                        strategy_mode=str(getattr(settings, "strategy_mode", "")),
                    )
                    order_entry: dict[str, object] = {
                        "id": order_id,
                        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "symbol": sym,
                        "side": intended_action,
                        "qty": int(order_qty),
                        "price": round(float(current_price), 4),
                        "mode": settings.trade_mode,
                        "ai_sleeve": ai_sleeve,
                        "ai_sleeve_reason": ai_sleeve_reason,
                        "status": "REQUESTED",
                        "detail": "",
                    }
                    sell_realizing_loss = bool(
                        intended_action == "SELL" and qty_before > 0 and avg > 0 and current_price < avg
                    )
                    if settings.trade_mode == "LIVE" and not settings.live_armed:
                        action = "HOLD"
                        reason_parts.append("blocked:live_not_armed")
                        order_entry["status"] = "BLOCKED"
                        order_entry["detail"] = "live_not_armed"
                        order_journal.append(order_entry)
                        _event(
                            state,
                            f"LIVE_GUARD blocked order {intended_action} {sym} x{order_qty} (not armed)",
                        )
                    elif settings.dry_run and settings.simulate_on_dry_run:
                        sim = _apply_fill(
                            ledger,
                            side=action,
                            qty=order_qty,
                            price=current_price,
                            symbol=sym,
                            regime=regime,
                            entry_mode=symbol_entry_mode,
                            setup_state=symbol_setup_state,
                            tags={
                                "a_grade_opening": bool(is_a_grade_opening),
                                "ai_sleeve": ai_sleeve,
                                "ai_sleeve_reason": ai_sleeve_reason,
                            },
                        )
                        order_ok = bool(sim.get("ok"))
                        if order_ok:
                            order_entry["status"] = "FILLED_SIM"
                            order_entry["detail"] = "simulated fill"
                            if is_a_grade_opening:
                                _event(
                                    state,
                                    "A_GRADE_ENTRY "
                                    + _a_grade_entry_summary(
                                        symbol=sym,
                                        score=float(score_map.get(sym, 0.0)),
                                        gap_pct=float(gap_from_prev_close_pct),
                                        daily_rsi=float(daily_rsi),
                                        attention_ratio=float(attention_ratio),
                                        value_spike_ratio=float(value_spike_ratio),
                                        foreign_net_qty=float(flow_snapshot_map.get(sym, {}).get("foreign_net_qty", 0.0)),
                                        institution_net_qty=float(flow_snapshot_map.get(sym, {}).get("institution_net_qty", 0.0)),
                                    ),
                                )
                            _event(state, f"SIM_FILL: {action} {sym} x{order_qty} @{current_price:.2f}")
                            last_trade_ts[sym] = time.time()
                            if sell_realizing_loss:
                                loss_reentry_block_until[sym] = time.time() + max(
                                    float(settings.trade_cooldown_sec),
                                    float(settings.bar_interval_minutes * 60),
                                )
                            reason_parts.append(f"exec:{action}x{order_qty}")
                        else:
                            order_entry["status"] = "REJECTED"
                            order_entry["detail"] = str(sim.get("reason"))
                            _event(state, f"SIM_FILL_REJECT {sym}: {sim.get('reason')}")
                            reason_parts.append(f"reject:{sim.get('reason')}")
                        order_journal.append(order_entry)
                    elif settings.dry_run:
                        order_entry["status"] = "DRY_SKIPPED"
                        order_entry["detail"] = "dry_run skip"
                        order_journal.append(order_entry)
                        _event(state, f"DRY_RUN: skipped order {action} {sym} x{order_qty}")
                        last_trade_ts[sym] = time.time()
                        if sell_realizing_loss:
                            loss_reentry_block_until[sym] = time.time() + max(
                                float(settings.trade_cooldown_sec),
                                float(settings.bar_interval_minutes * 60),
                            )
                        reason_parts.append(f"dry:{action}x{order_qty}")
                    else:
                        result = api.place_order(symbol=sym, side=action, quantity=order_qty)
                        code = str(result.get("return_code", ""))
                        order_entry["broker_result"] = result
                        if code in {"0", "00"}:
                            order_entry["status"] = "ACK"
                            fill = _apply_fill(
                                ledger,
                                side=action,
                                qty=order_qty,
                                price=current_price,
                                symbol=sym,
                                regime=regime,
                                entry_mode=symbol_entry_mode,
                                setup_state=symbol_setup_state,
                                tags={
                                    "a_grade_opening": bool(is_a_grade_opening),
                                    "ai_sleeve": ai_sleeve,
                                    "ai_sleeve_reason": ai_sleeve_reason,
                                },
                            )
                            order_ok = bool(fill.get("ok"))
                            if order_ok:
                                order_entry["status"] = "FILLED_LOCAL"
                                order_entry["detail"] = "broker ack + local fill"
                                last_trade_ts[sym] = time.time()
                                if sell_realizing_loss:
                                    loss_reentry_block_until[sym] = time.time() + max(
                                        float(settings.trade_cooldown_sec),
                                        float(settings.bar_interval_minutes * 60),
                                    )
                                reason_parts.append(f"exec:{action}x{order_qty}")
                                if is_a_grade_opening:
                                    _event(
                                        state,
                                        "A_GRADE_ENTRY "
                                        + _a_grade_entry_summary(
                                            symbol=sym,
                                            score=float(score_map.get(sym, 0.0)),
                                            gap_pct=float(gap_from_prev_close_pct),
                                            daily_rsi=float(daily_rsi),
                                            attention_ratio=float(attention_ratio),
                                            value_spike_ratio=float(value_spike_ratio),
                                            foreign_net_qty=float(flow_snapshot_map.get(sym, {}).get("foreign_net_qty", 0.0)),
                                            institution_net_qty=float(flow_snapshot_map.get(sym, {}).get("institution_net_qty", 0.0)),
                                        ),
                                    )
                                pending_reconcile[order_id] = {
                                    "symbol": sym,
                                    "side": intended_action,
                                    "expected_qty_after": expected_qty_after,
                                    "created_at": time.time(),
                                    "status": "PENDING",
                                }
                            else:
                                order_entry["status"] = "ACK_FILL_REJECTED"
                                order_entry["detail"] = str(fill.get("reason", "local fill rejected"))
                        else:
                            order_entry["status"] = "REJECTED"
                            order_entry["detail"] = f"return_code={code}"
                        order_journal.append(order_entry)
                        _event(state, f"ORDER RESULT {sym}: {result}")
                    if order_ok or settings.dry_run:
                        state.order_count += 1
                elif action in {"BUY", "SELL"} and state.order_count >= settings.max_daily_orders:
                    reason_parts.append("blocked:max_daily_orders")
                    action = "HOLD"

                prev_prices[sym] = current_price
                if sym == primary_symbol:
                    state.last_price = current_price
                    state.last_action = action

                position_return_pct = (
                    ((current_price - avg) / avg) * 100.0 if qty > 0 and avg > 0 else 0.0
                )
                for rp in reason_parts:
                    if isinstance(rp, str) and rp.startswith("blocked:"):
                        key = rp.split("(", 1)[0]
                        reason_counter[key] = int(reason_counter.get(key, 0)) + 1
                stock_statuses.append(
                    {
                        "symbol": sym,
                        "price": round(current_price, 2),
                        "action": action,
                        "selected": sym in selected_symbols,
                        "score": round(float(score_map.get(sym, 0.0)), 2),
                        "qty": qty,
                        "avg_price": round(avg, 2),
                        "return_pct": round(position_return_pct, 2),
                        "volatility_pct": round(symbol_vol * 100.0, 2),
                        "atr_proxy_pct": round(float(atr_cache.get(sym, 0.0)) * 100.0, 2),
                        "factor_momentum_pct": round(float(factor_row.get("momentum_pct", 0.0)), 2),
                        "factor_relative_pct": round(float(factor_row.get("relative_pct", 0.0)), 2),
                        "factor_trend_pct": round(float(factor_row.get("trend_pct", 0.0)), 2),
                        "factor_vol_penalty_pct": round(float(factor_row.get("volatility_pct", 0.0)), 2),
                        "factor_ret5_pct": round(float(factor_row.get("ret5_pct", 0.0)), 2),
                        "factor_atr14_pct": round(float(factor_row.get("atr14_pct", 0.0)), 2),
                        "factor_daily_rsi": round(float(factor_row.get("daily_rsi", 50.0)), 1),
                        "factor_attention_ratio": round(float(factor_row.get("attention_ratio", 0.0)), 2),
                        "factor_value_spike_ratio": round(float(factor_row.get("value_spike_ratio", 0.0)), 2),
                        "factor_risk_adjusted_momentum": round(float(factor_row.get("risk_adjusted_momentum", 0.0)), 2),
                        "factor_risk_adjusted_relative": round(float(factor_row.get("risk_adjusted_relative", 0.0)), 2),
                        "factor_trend_efficiency": round(float(factor_row.get("trend_efficiency", 0.0)), 2),
                        "factor_participation_quality": round(float(factor_row.get("participation_quality", 0.0)), 2),
                        "factor_top_rank_quality_penalty": round(float(factor_row.get("top_rank_quality_penalty", 0.0)), 2),
                        "factor_near_high_pct": round(float(factor_row.get("near_high_pct", 0.0)), 2),
                        "factor_trend_ok": bool(factor_row.get("trend_ok", 0.0)),
                        "factor_structure_ok": bool(factor_row.get("structure_ok", 0.0)),
                        "factor_breakout_ok": bool(factor_row.get("breakout_ok", 0.0)),
                        "factor_overheat": bool(factor_row.get("overheat", 0.0)),
                        "factor_overextended": bool(factor_row.get("overextended", 0.0)),
                        "factor_overextension_penalty": round(float(factor_row.get("overextension_penalty", 0.0)), 3),
                        "factor_selection_eligible": bool(float(factor_row.get("selection_eligible", 1.0))),
                        "factor_reject_reason": str(factor_row.get("reject_reason", "")),
                        "factor_data_ready": bool(factor_meta_state.get(sym, {}).get("factor_data_ready", bool(factor_row))),
                        "factor_data_reason": str(factor_meta_state.get(sym, {}).get("factor_data_reason", "")),
                        "factor_daily_bar_count": int(factor_meta_state.get(sym, {}).get("daily_bar_count", len(tech_close_cache.get(sym, [])))),
                        "factor_required_daily_bars": int(factor_meta_state.get(sym, {}).get("required_daily_bars", 60)),
                        "factor_bearish_exception_ready": bool(bearish_long_ok),
                        "watchlist": bool(trend_diag.get("watchlist", False)),
                        "watch_reason": str(trend_diag.get("watch_reason") or ""),
                        "market_type": str(trend_diag.get("market_type") or ""),
                        "entry_blockers": list(trend_diag.get("blockers") or []),
                        "foreign_net_qty": round(float(flow_snapshot_map.get(sym, {}).get("foreign_net_qty", 0.0)), 2),
                        "institution_net_qty": round(float(flow_snapshot_map.get(sym, {}).get("institution_net_qty", 0.0)), 2),
                        "vi_active": bool(sym in vi_active_symbols),
                        "gap_from_prev_close_pct": round(float(gap_from_prev_close_pct), 2),
                        "opening_strength": (
                            "강"
                            if gap_from_prev_close_pct >= 2.0
                            else ("중" if gap_from_prev_close_pct >= 0.5 else ("약" if gap_from_prev_close_pct > -1.0 else "약세"))
                        ),
                        "sector": str(symbol_sector_map.get(sym, "UNMAPPED")),
                        "quote_volume": int(quote_volume_map.get(sym, 0)),
                        "signal_raw": raw_signal,
                        "confirm_needed": confirm_needed,
                        "confirm_progress": confirm_progress,
                        "data_age_sec": int(age_map.get(sym, 0.0)),
                        "cooldown_left_sec": cooldown_left,
                        "decision_reason": " | ".join(reason_parts[-6:]),
                        "session_phase": str(session_ctx.get("phase", "")),
                        "session_profile": str(session_ctx.get("profile", "")),
                        "session_shock_active": bool(session_ctx.get("shock_active", False)),
                        "regime": regime,
                        "strategy_edge": round(symbol_edge, 3),
                        "tech_golden_cross": bool(tech_flags.get("golden_cross", False)),
                        "tech_death_cross": bool(tech_flags.get("death_cross", False)),
                        "tech_bollinger_pos": round(float(tech_flags.get("bb_pos", 0.0)), 3),
                        "tech_volume_spike": bool(tech_flags.get("volume_spike", False)),
                        "tech_short_bottom": bool(tech_flags.get("short_bottom", False)),
                        "tech_short_top": bool(tech_flags.get("short_top", False)),
                        "tech_trend_up": bool(tech_flags.get("trend_up", False)),
                        "tech_trend_down": bool(tech_flags.get("trend_down", False)),
                    }
                )

                if bar_decision_due or risk_exit_signal:
                    _event(
                        state,
                        f"BAR_DECISION bar={decision_bar_ts} symbol={sym} prev={prev_price:.2f} current={current_price:.2f} "
                        f"raw={raw_signal} action={action} regime={trade_regime} bias={market_bias_mode} phase={session_ctx.get('phase')}",
                    )

            _record_perf_phase(perf_profile, "decision_eval_sec", phase_started_at)

            selected_bar_ts = bar_dt.strftime("%Y-%m-%d %H:%M:%S")
            if selected_symbols and selected_bar_ts != last_selected_intraday_bar_ts:
                selected_snapshot_rows: list[dict[str, object]] = []
                for row in stock_statuses:
                    if not bool((row or {}).get("selected")):
                        continue
                    selected_snapshot_rows.append(
                        {
                            "symbol": str((row or {}).get("symbol") or ""),
                            "price": round(float((row or {}).get("price", 0.0) or 0.0), 2),
                            "action": str((row or {}).get("action") or "HOLD"),
                            "signal_raw": str((row or {}).get("signal_raw") or (row or {}).get("action") or "HOLD"),
                            "score": round(float((row or {}).get("score", 0.0) or 0.0), 2),
                            "return_pct": round(float((row or {}).get("return_pct", 0.0) or 0.0), 2),
                            "factor_trend_pct": round(float((row or {}).get("factor_trend_pct", 0.0) or 0.0), 2),
                            "factor_attention_ratio": round(float((row or {}).get("factor_attention_ratio", 0.0) or 0.0), 2),
                            "factor_value_spike_ratio": round(float((row or {}).get("factor_value_spike_ratio", 0.0) or 0.0), 2),
                            "decision_reason": str((row or {}).get("decision_reason") or ""),
                        }
                    )
                if selected_snapshot_rows:
                    selected_intraday_prices = _append_selected_intraday_snapshot(
                        selected_intraday_prices,
                        bar_ts=selected_bar_ts,
                        rows=selected_snapshot_rows,
                        bar_interval_minutes=bar_minutes,
                    )
                    _save_selected_intraday_prices(selected_intraday_prices_path, selected_intraday_prices)
                    last_selected_intraday_bar_ts = selected_bar_ts
            if bar_decision_due:
                last_decision_bar_ts = decision_bar_ts

            # Reconcile provisional live fills with current local position snapshot.
            reconciled_now = 0
            timeout_now = 0
            pending_now = 0
            done_ids: list[str] = []
            for oid, rec in list(pending_reconcile.items()):
                psym = str(rec.get("symbol", ""))
                side = str(rec.get("side", ""))
                expected_qty_after = int(rec.get("expected_qty_after", 0))
                created_at = float(rec.get("created_at", time.time()))
                current_qty = int(ledger.positions.get(psym, {}).get("qty", 0))
                age_sec = time.time() - created_at
                ok_reconciled = (
                    (side == "BUY" and current_qty >= expected_qty_after)
                    or (side == "SELL" and current_qty <= expected_qty_after)
                )
                if ok_reconciled:
                    rec["status"] = "RECONCILED"
                    rec["reconciled_at"] = time.time()
                    for j in reversed(order_journal):
                        if str(j.get("id")) == oid:
                            j["status"] = "RECONCILED"
                            j["detail"] = f"local qty={current_qty}"
                            break
                    reconciled_now += 1
                    done_ids.append(oid)
                elif age_sec >= max(20.0, settings.poll_seconds * 3):
                    rec["status"] = "TIMEOUT"
                    rec["reconciled_at"] = time.time()
                    for j in reversed(order_journal):
                        if str(j.get("id")) == oid:
                            j["status"] = "RECONCILE_TIMEOUT"
                            j["detail"] = f"age={int(age_sec)}s qty={current_qty}"
                            break
                    timeout_now += 1
                    done_ids.append(oid)
                else:
                    pending_now += 1
            for oid in done_ids:
                pending_reconcile.pop(oid, None)

            equity, unrealized, total_pnl, _ = _mark_to_market(ledger, price_map)
            ledger.equity_history.append(
                {"ts": datetime.now().isoformat(timespec="seconds"), "equity": round(equity, 4)}
            )
            ledger.equity_history = ledger.equity_history[-5000:]

            pos_sym, pos_qty, pos_avg = _primary_position(ledger)
            positions_summary = ", ".join(
                f"{sym}:{int(row.get('qty', 0))}@{float(row.get('avg_price', 0.0)):.1f}"
                for sym, row in sorted(ledger.positions.items())
                if int(row.get("qty", 0)) > 0
            )
            history_stats, turnover_pct, turnover_note = _selection_history_stats(
                selection_history,
                list(selected_symbols),
            )
            today_key = datetime.now().strftime("%Y-%m-%d")
            today_trade_count = _trade_count_on_day(ledger, today_key)
            blockers = sorted(reason_counter.items(), key=lambda x: x[1], reverse=True)[:3]
            blocker_text = ", ".join(f"{k}:{v}" for k, v in blockers) if blockers else "집계 중"
            status_map = {
                str(row.get("symbol") or "").strip(): row
                for row in stock_statuses
                if isinstance(row, dict) and str(row.get("symbol") or "").strip()
            }
            opening_focus_parts = []
            for sym in focus_symbols_seed[:3]:
                row = status_map.get(sym, {})
                prev_close = float(tech_close_cache.get(sym, [0.0])[-1]) if tech_close_cache.get(sym) else 0.0
                current_price = float((row or {}).get("price", 0.0) or 0.0)
                if current_price > 0:
                    opening_focus_parts.append(
                        _symbol_intraday_brief(
                            row=row if isinstance(row, dict) else {},
                            symbol=sym,
                            prev_close=prev_close,
                            current_price=current_price,
                        )
                    )
            opening_focus_summary = " | ".join(opening_focus_parts) if opening_focus_parts else "-"
            vi_summary = ",".join(sorted(list(vi_active_symbols))[:5]) if vi_active_symbols else "발동 종목 없음"
            chase_risk_keys = {
                "late_chase",
                "mid_band_late_chase",
                "high_rsi_upper_band",
                "market_surge_chase",
                "strong_overextension",
                "overextended_continuation",
            }

            def _row_blocker_keys(row: dict[str, object]) -> set[str]:
                return {
                    str(x or "").split(":", 1)[-1].split("(", 1)[0].strip().lower()
                    for x in list(row.get("entry_blockers") or [])
                    if str(x).strip()
                }

            def _flow_sync_score(row: dict[str, object]) -> float:
                foreign_net = float(row.get("foreign_net_qty", 0.0) or 0.0)
                institution_net = float(row.get("institution_net_qty", 0.0) or 0.0)
                score = float(row.get("score", 0.0) or 0.0)
                attention = float(row.get("factor_attention_ratio", 0.0) or 0.0)
                return foreign_net + institution_net + max(0.0, score * 1000.0) + (attention * 100.0)

            flow_sync_rows = [
                row for row in stock_statuses
                if isinstance(row, dict)
                and float(row.get("foreign_net_qty", 0.0) or 0.0) > 0.0
                and float(row.get("institution_net_qty", 0.0) or 0.0) > 0.0
            ]
            flow_sync_safe_rows = [
                row for row in flow_sync_rows
                if not (_row_blocker_keys(row) & chase_risk_keys)
            ]
            a_grade_rows = [
                row for row in flow_sync_safe_rows
                if not bool(row.get("vi_active"))
            ]
            flow_sync_safe_rows.sort(key=_flow_sync_score, reverse=True)
            a_grade_rows.sort(key=_flow_sync_score, reverse=True)
            opening_priority_summary = " | ".join(
                f"{str(row.get('symbol') or '').strip()} "
                f"score={float(row.get('score', 0.0) or 0.0):+.2f} "
                f"외인 {_format_flow_qty(float(row.get('foreign_net_qty', 0.0) or 0.0))} "
                f"기관 {_format_flow_qty(float(row.get('institution_net_qty', 0.0) or 0.0))}"
                for row in flow_sync_safe_rows[:3]
                if str(row.get("symbol") or "").strip()
            ) or "-"
            opening_a_grade_summary = " | ".join(
                f"{str(row.get('symbol') or '').strip()} "
                f"score={float(row.get('score', 0.0) or 0.0):+.2f} "
                f"갭={float(row.get('gap_from_prev_close_pct', 0.0) or 0.0):+.2f}%"
                for row in a_grade_rows[:3]
                if str(row.get("symbol") or "").strip()
            ) or "-"
            no_trade_summary = (
                f"{today_key} 체결 {today_trade_count}건"
                if today_trade_count > 0
                else f"{today_key} 무체결 | 상위 차단 {blocker_text}"
            )

            state.position_qty = _position_qty_total(ledger)
            state.position_symbol = pos_sym
            state.avg_price = round(pos_avg, 4)
            state.active_positions = _position_count(ledger)
            state.monitored_symbols = ",".join(selected_symbols or analysis_focus_symbols)
            state.positions_summary = positions_summary
            state.stock_statuses = stock_statuses
            state.cash_balance = round(ledger.cash, 2)
            state.equity = round(equity, 2)
            state.unrealized_pnl = round(unrealized, 2)
            state.realized_pnl = round(ledger.realized_pnl, 2)
            state.total_pnl = round(total_pnl, 2)
            state.total_return_pct = round((total_pnl / ledger.initial_cash) * 100.0, 2)
            state.perf_daily_pct = round(_period_return(ledger.equity_history, equity, 1), 2)
            state.perf_weekly_pct = round(_period_return(ledger.equity_history, equity, 7), 2)
            state.perf_monthly_pct = round(_period_return(ledger.equity_history, equity, 30), 2)
            state.broker_account_snapshot = dict(broker_account_snapshot) if isinstance(broker_account_snapshot, dict) else {}
            if isinstance(broker_account_snapshot, dict):
                broker_cash = float(broker_account_snapshot.get("cash_balance", 0.0) or 0.0)
                broker_equity = float(broker_account_snapshot.get("equity", 0.0) or 0.0)
                broker_total_pnl = float(broker_account_snapshot.get("total_pnl", 0.0) or 0.0)
                broker_total_return_pct = float(broker_account_snapshot.get("total_return_pct", 0.0) or 0.0)
                broker_positions_summary = str(broker_account_snapshot.get("positions_summary") or "").strip()
                broker_active_positions = int(float(broker_account_snapshot.get("active_positions", 0) or 0))
                broker_position_qty = int(float(broker_account_snapshot.get("position_qty", 0) or 0))
                broker_position_symbol = str(broker_account_snapshot.get("position_symbol") or "").strip()
                if abs(broker_cash) > 1e-12 or abs(broker_equity) > 1e-12:
                    state.cash_balance = round(broker_cash, 2)
                    state.equity = round(broker_equity, 2)
                    state.unrealized_pnl = round(float(broker_account_snapshot.get("unrealized_pnl", 0.0) or 0.0), 2)
                    state.realized_pnl = round(float(broker_account_snapshot.get("realized_pnl", 0.0) or 0.0), 2)
                    state.total_pnl = round(broker_total_pnl, 2)
                    state.total_return_pct = round(broker_total_return_pct, 2)
                if broker_positions_summary or broker_active_positions > 0:
                    state.active_positions = broker_active_positions
                    state.position_qty = broker_position_qty
                    state.positions_summary = broker_positions_summary
                    state.position_symbol = broker_position_symbol
            state.selected_symbol = primary_symbol
            state.market_regime = regime
            state.strategy_reference = strategy_reference
            state.selection_score = round(float(score_map.get(primary_symbol, 0.0)), 2)
            state.selection_reason = str(selection_detail_state.get("reason") or "")
            state.selection_detail = dict(selection_detail_state)
            state.auto_params = {
                "enabled": bool(settings.auto_param_tuning_enabled),
                "strength": round(float(settings.auto_param_tuning_strength), 2),
                "min_entry_score": round(float(auto_params.get("min_entry_score", settings.min_entry_score)), 3),
                "min_entry_momentum_pct": round(float(auto_params.get("min_entry_momentum_pct", settings.min_entry_momentum_pct)), 3),
                "take_profit_partial_ratio": round(float(auto_params.get("take_profit_partial_ratio", settings.take_profit_partial_ratio)), 3),
                "market_bias_mode": market_bias_mode,
                "market_bias_reason": market_bias_reason,
                "market_volatility_pct": round(float(auto_params.get("market_volatility_pct", 0.0)), 3),
                "risk_bias": round(float(auto_params.get("risk_bias", 0.0)), 3),
                "session_phase": str(session_ctx.get("phase", "")),
                "session_profile": str(session_ctx.get("profile", "")),
                "session_diag": str(session_ctx.get("diag", "")),
                "session_shock_active": bool(session_ctx.get("shock_active", False)),
                "session_shock_reason": str(session_ctx.get("shock_reason", "")),
                "daily_selection_done": bool(last_selection_day == current_day),
                "daily_selection_day": str(last_selection_day),
                "daily_selection_status": (
                    f"{current_day} 선정 완료"
                    if last_selection_day == current_day
                    else (
                        "장전 선정 대기"
                        if str(session_ctx.get('phase', 'OFF_HOURS')) in {"OFF_HOURS", "AFTER_MARKET", "PREMARKET_BRIEF"}
                        else "당일 선정 대기"
                    )
                ),
            }
            state.regime_confidence = round(regime_confidence, 3)
            state.session_phase = str(session_ctx.get("phase", "OFF_HOURS"))
            state.session_profile = str(session_ctx.get("profile", "CAPITAL_PRESERVATION"))
            state.session_diag = str(session_ctx.get("diag", ""))
            state.daily_selection_done = bool(last_selection_day == current_day)
            state.daily_selection_day = str(last_selection_day)
            state.daily_selection_status = (
                f"{current_day} 선정 완료"
                if last_selection_day == current_day
                else (
                    "장전 선정 대기"
                    if str(session_ctx.get('phase', 'OFF_HOURS')) in {"OFF_HOURS", "AFTER_MARKET", "PREMARKET_BRIEF"}
                    else "당일 선정 대기"
                )
            )
            state.risk_halt_active = risk_halt_active
            state.risk_halt_reason = (
                f"daily_loss {daily_return_pre:.2f}% <= {settings.daily_loss_limit_pct:.2f}%"
                if risk_halt_active
                else ""
            )
            state.stale_data_active = stale_data_active
            state.stale_data_reason = stale_reason
            state.data_freshness_sec = round(data_freshness_sec, 1)
            state.portfolio_heat_pct = round(heat_pct_pre, 2)
            state.max_portfolio_heat_pct = float(settings.max_portfolio_heat_pct)
            state.max_symbol_loss_pct = float(settings.max_symbol_loss_pct)
            state.trade_mode = settings.trade_mode
            state.live_armed = bool(settings.live_armed)
            state.no_trade_summary = no_trade_summary
            state.market_flow_summary = flow_summary_text
            state.vi_summary = vi_summary
            state.opening_focus_summary = opening_focus_summary
            state.opening_priority_summary = opening_priority_summary
            state.opening_a_grade_summary = opening_a_grade_summary
            state.opening_review_summary = _post_market_candidate_review(stock_statuses, ledger, current_day)
            selected_count = sum(1 for row in stock_statuses if bool((row or {}).get("selected")))
            buy_signal_count = sum(1 for row in stock_statuses if str((row or {}).get("action", "")).upper() == "BUY")
            watch_count = sum(1 for row in stock_statuses if bool((row or {}).get("watchlist")))
            blockers = sorted(reason_counter.items(), key=lambda x: x[1], reverse=True)[:3]
            blocker_text = ", ".join(f"{k}:{v}" for k, v in blockers) if blockers else "-"
            state.decision_activity_summary = (
                f"후보 {len(dynamic_candidate_pool)} | 감시 {selected_count} | BUY신호 {buy_signal_count} | "
                f"WATCH {watch_count} | 포지션한도 {settings.max_active_positions} | 차단 {blocker_text}"
            )
            state.selection_history_stats = history_stats
            state.selection_turnover_pct = round(turnover_pct, 1)
            state.selection_turnover_note = turnover_note
            state.reason_histogram = dict(sorted(reason_counter.items(), key=lambda x: x[1], reverse=True))
            perf_profile["daily_analysis_cache_size"] = float(len(daily_analysis_cache))
            perf_profile["watch_symbol_count"] = float(len(watch_symbols))
            perf_profile["candidate_pool_count"] = float(len(dynamic_candidate_pool))
            perf_profile["selected_symbol_count"] = float(len(selected_symbols))
            perf_profile["loop_total_sec"] = round(max(0.0, time.perf_counter() - loop_started_at), 4)

            loop_ms = float(perf_profile.get("loop_total_sec", 0.0)) * 1000.0
            loop_latency_samples_ms.append(loop_ms)
            if loop_latency_samples_ms:
                samples_sorted = sorted(loop_latency_samples_ms)
                p50_idx = min(len(samples_sorted) - 1, len(samples_sorted) // 2)
                p95_idx = min(len(samples_sorted) - 1, int(len(samples_sorted) * 0.95))
                perf_profile["loop_p50_ms"] = round(float(samples_sorted[p50_idx]), 2)
                perf_profile["loop_p95_ms"] = round(float(samples_sorted[p95_idx]), 2)

            state.perf_profile = dict(perf_profile)
            state.factor_snapshot = [
                {
                    "symbol": sym,
                    "score": round(float(score_map.get(sym, 0.0)), 3),
                    "momentum_pct": round(float(factor_map_state.get(sym, {}).get("momentum_pct", 0.0)), 2),
                    "relative_pct": round(float(factor_map_state.get(sym, {}).get("relative_pct", 0.0)), 2),
                    "trend_pct": round(float(factor_map_state.get(sym, {}).get("trend_pct", 0.0)), 2),
                    "volatility_pct": round(float(factor_map_state.get(sym, {}).get("volatility_pct", 0.0)), 2),
                    "ret5_pct": round(float(factor_map_state.get(sym, {}).get("ret5_pct", 0.0)), 2),
                    "atr14_pct": round(float(factor_map_state.get(sym, {}).get("atr14_pct", 0.0)), 2),
                    "daily_rsi": round(float(factor_map_state.get(sym, {}).get("daily_rsi", 50.0)), 1),
                    "attention_ratio": round(float(factor_map_state.get(sym, {}).get("attention_ratio", 0.0)), 2),
                    "value_spike_ratio": round(float(factor_map_state.get(sym, {}).get("value_spike_ratio", 0.0)), 2),
                    "near_high_pct": round(float(factor_map_state.get(sym, {}).get("near_high_pct", 0.0)), 2),
                    "trend_ok": bool(factor_map_state.get(sym, {}).get("trend_ok", 0.0)),
                    "structure_ok": bool(factor_map_state.get(sym, {}).get("structure_ok", 0.0)),
                    "breakout_ok": bool(factor_map_state.get(sym, {}).get("breakout_ok", 0.0)),
                    "overheat": bool(factor_map_state.get(sym, {}).get("overheat", 0.0)),
                    "overextended": bool(factor_map_state.get(sym, {}).get("overextended", 0.0)),
                    "overextension_penalty": round(float(factor_map_state.get(sym, {}).get("overextension_penalty", 0.0)), 3),
                    "sector": str(symbol_sector_map.get(sym, "UNMAPPED")),
                }
                for sym in selected_symbols
            ]
            state.order_journal = list(order_journal)[-80:]
            state.reconcile_stats = {
                "pending": pending_now,
                "reconciled_this_loop": reconciled_now,
                "timeout_this_loop": timeout_now,
                "journal_size": len(order_journal),
            }
            if (
                float(perf_profile.get("loop_total_sec", 0.0)) >= perf_slow_loop_sec
                or (state.loop_count % perf_log_every_loops) == 0
            ):
                _event(
                    state,
                    "PERF "
                    + f"loop={float(perf_profile.get('loop_total_sec', 0.0)):.3f}s "
                    + f"candidate={float(perf_profile.get('candidate_refresh_sec', 0.0)):.3f}s "
                    + f"selection={float(perf_profile.get('selection_sec', 0.0)):.3f}s "
                    + f"vol={float(perf_profile.get('vol_refresh_sec', 0.0)):.3f}s "
                    + f"quotes={float(perf_profile.get('quote_fetch_sec', 0.0)):.3f}s "
                    + f"decision={float(perf_profile.get('decision_eval_sec', 0.0)):.3f}s "
                    + f"cache={int(float(perf_profile.get('daily_analysis_cache_size', 0.0)))} "
                    + f"watch={int(float(perf_profile.get('watch_symbol_count', 0.0)))} "
                    + f"selected={int(float(perf_profile.get('selected_symbol_count', 0.0)))}",
                )

            # Performance regression alert: raise if p95 loop latency stays high.
            loop_p95_ms = float(perf_profile.get("loop_p95_ms", 0.0) or 0.0)
            if len(loop_latency_samples_ms) >= min(perf_alert_window, 10):
                if loop_p95_ms >= perf_alert_p95_ms:
                    perf_alert_hits += 1
                else:
                    perf_alert_hits = 0

                if (
                    perf_alert_hits >= perf_alert_consecutive
                    and (now - last_perf_alert_ts) >= perf_alert_cooldown_sec
                ):
                    perf_msg = (
                        "PERF_ALERT "
                        + f"p95={loop_p95_ms:.1f}ms "
                        + f"threshold={perf_alert_p95_ms:.1f}ms "
                        + f"window={len(loop_latency_samples_ms)} "
                        + f"hits={perf_alert_hits} "
                        + f"loop={float(perf_profile.get('loop_total_sec', 0.0)):.3f}s "
                        + f"candidate={float(perf_profile.get('candidate_refresh_sec', 0.0)):.3f}s "
                        + f"selection={float(perf_profile.get('selection_sec', 0.0)):.3f}s "
                        + f"quotes={float(perf_profile.get('quote_fetch_sec', 0.0)):.3f}s "
                        + f"decision={float(perf_profile.get('decision_eval_sec', 0.0)):.3f}s"
                    )
                    _event(state, perf_msg)
                    if _SLACK_NOTIFIER and settings.slack_enabled:
                        _SLACK_NOTIFIER.send(perf_msg, force=True)
                    last_perf_alert_ts = now
                    perf_alert_hits = 0

            # Hourly Slack heartbeat: once per hour only while the KRX market session is open.
            if _SLACK_NOTIFIER and settings.slack_enabled and settings.morning_brief_enabled:
                now_dt = datetime.now()
                if now_dt.weekday() < 5:
                    schedule_times = _parse_market_brief_times(
                        getattr(
                            settings,
                            "market_brief_times",
                            f"{int(settings.morning_brief_hour):02d}:{int(settings.morning_brief_minute):02d}",
                        )
                    )
                    due_slots = [
                        (hh, mm)
                        for hh, mm in schedule_times
                        if (now_dt.hour, now_dt.minute) >= (hh, mm)
                    ]
                    if due_slots:
                        slot_hh, slot_mm = due_slots[-1]
                        slot_key = f"{now_dt.strftime('%Y-%m-%d')} {slot_hh:02d}:{slot_mm:02d}"
                        if slot_key != last_market_brief_slot_key:
                            today_key = now_dt.strftime("%Y-%m-%d")
                            sent_news_keys_by_day = market_brief_history.get("sent_news_keys_by_day")
                            if not isinstance(sent_news_keys_by_day, dict):
                                sent_news_keys_by_day = {}
                            seen_news_keys_today = {
                                str(x).strip()
                                for x in list(sent_news_keys_by_day.get(today_key) or [])
                                if str(x).strip()
                            }
                            selection_snapshot = _selection_report_snapshot(
                                selected_symbols,
                                current_day=today_key,
                                last_selection_day=last_selection_day,
                                fallback_symbol=settings.symbol,
                            )
                            primary = str(selection_snapshot.get("primary") or "SELECTION_PENDING")
                            selected_for_report = [
                                str(sym).strip()
                                for sym in list(selection_snapshot.get("selected") or [])
                                if str(sym).strip()
                            ]
                            top_ranked = [
                                str(item.get("symbol") or "").strip()
                                for item in list(selection_detail_state.get("top_ranked", []))[:5]
                                if isinstance(item, dict) and str(item.get("symbol") or "").strip()
                            ]
                            news_brief = _build_morning_news_brief(settings, exclude_keys=seen_news_keys_today)
                            msg = (
                                "MARKET_BRIEF "
                                f"time={now_dt.strftime('%Y-%m-%d %H:%M:%S')} "
                                f"slot={slot_hh:02d}:{slot_mm:02d} "
                                f"phase={session_ctx.get('phase', 'OFF_HOURS')} "
                                f"regime={regime} conf={regime_confidence:.2f} idx={regime_index_pct:+.2f}% "
                                f"selection_basis={strategy_reference or '-'} "
                                f"selection_status={selection_snapshot.get('status')} "
                                f"primary={primary} "
                                f"selected={','.join(selected_for_report) if selected_for_report else '-'} "
                                f"fallback_symbol={selection_snapshot.get('fallback_symbol')} "
                                f"top_ranked={','.join(top_ranked) if top_ranked else '-'} "
                                f"active_positions={_position_count(ledger)} "
                                f"equity={state.equity:.0f} pnl={state.total_pnl:+.0f} ({state.total_return_pct:+.2f}%) "
                                f"{str(news_brief.get('summary') or '').strip()}"
                            )
                            _SLACK_NOTIFIER.send(msg, force=True)
                            news_items = list(news_brief.get("items") or [])
                            if news_items:
                                detail_lines = [
                                    f"{idx + 1}. [{_format_news_published(str(item.get('published') or ''), tz_name=str(getattr(settings, 'market_timezone', 'Asia/Seoul') or 'Asia/Seoul'))}] "
                                    f"{_truncate_text(str(item.get('title') or ''), 220)}"
                                    for idx, item in enumerate(news_items[: max(1, int(settings.morning_news_limit))])
                                ]
                                detail_chunks = _chunk_lines("MARKET_NEWS", detail_lines, max_chars=1800)
                                for detail in detail_chunks:
                                    _SLACK_NOTIFIER.send(detail, force=True)
                                sent_news_keys_by_day[today_key] = list(dict.fromkeys(
                                    list(sent_news_keys_by_day.get(today_key) or [])
                                    + [
                                        _news_item_key(item)
                                        for item in news_items
                                    ]
                                ))[-100:]
                            old_days = sorted(sent_news_keys_by_day.keys())
                            for old_day in old_days[:-7]:
                                sent_news_keys_by_day.pop(old_day, None)
                            market_brief_history = {
                                "last_slot_key": slot_key,
                                "sent_news_keys_by_day": sent_news_keys_by_day,
                            }
                            _save_market_brief_history(market_brief_history_path, market_brief_history)
                            _event(state, f"MARKET_BRIEF_SENT {slot_key}")
                            last_market_brief_slot_key = slot_key

            if _SLACK_NOTIFIER and settings.slack_enabled:
                now_dt = datetime.now()
                today_key = now_dt.strftime("%Y-%m-%d")
                session_phase = str(session_ctx.get("phase", "OFF_HOURS"))
                if (
                    now_dt.weekday() < 5
                    and session_phase == "AFTER_MARKET"
                    and today_key != last_no_trade_summary_day
                    and _trade_count_on_day(ledger, today_key) == 0
                ):
                    blockers = sorted(reason_counter.items(), key=lambda x: x[1], reverse=True)[:3]
                    blocker_text = ",".join(f"{k}:{v}" for k, v in blockers) if blockers else "-"
                    selection_snapshot = _selection_report_snapshot(
                        selected_symbols,
                        current_day=today_key,
                        last_selection_day=last_selection_day,
                        fallback_symbol=settings.symbol,
                    )
                    primary = str(selection_snapshot.get("primary") or "SELECTION_PENDING")
                    selected_for_report = [
                        str(sym).strip()
                        for sym in list(selection_snapshot.get("selected") or [])
                        if str(sym).strip()
                    ]
                    msg = (
                        "NO_TRADE_SUMMARY "
                        f"day={today_key} "
                        f"phase={session_phase} "
                        f"regime={regime} conf={regime_confidence:.2f} idx={regime_index_pct:+.2f}% "
                        f"selection_basis={strategy_reference or '-'} "
                        f"selection_status={selection_snapshot.get('status')} "
                        f"primary={primary} "
                        f"selected={','.join(selected_for_report) if selected_for_report else '-'} "
                        f"fallback_symbol={selection_snapshot.get('fallback_symbol')} "
                        f"top_blockers={blocker_text} "
                        f"active_positions={_position_count(ledger)} "
                        f"equity={state.equity:.0f} pnl={state.total_pnl:+.0f} ({state.total_return_pct:+.2f}%)"
                    )
                    _SLACK_NOTIFIER.send(msg, force=True)
                    _event(state, f"NO_TRADE_SUMMARY_SENT {today_key} blockers={blocker_text}")
                    last_no_trade_summary_day = today_key

            if _SLACK_NOTIFIER and settings.slack_enabled:
                now_dt = datetime.now()
                today_key = now_dt.strftime("%Y-%m-%d")
                session_phase = str(session_ctx.get("phase", "OFF_HOURS"))
                if (
                    now_dt.weekday() < 5
                    and session_phase in {"OPENING_FOCUS", "REGULAR_SESSION"}
                    and (now_kst.hour > 9 or (now_kst.hour == 9 and now_kst.minute >= 10))
                    and today_key != last_opening_brief_day
                ):
                    selection_locked = bool(last_selection_day == current_day and selected_symbols)
                    report_symbols = list(selected_symbols) if selection_locked else focus_symbols_seed[:5]
                    watched_rows = [
                        row for row in stock_statuses
                        if isinstance(row, dict) and bool(row.get("watchlist", False))
                    ]
                    watchlist_text = " | ".join(
                        f"{str(row.get('symbol') or '').strip()}:{_humanize_watch_reason(row.get('watch_reason'))}"
                        for row in watched_rows[:3]
                        if str(row.get("symbol") or "").strip()
                    ) or "-"
                    gap_leaders = sorted(
                        [
                            row for row in stock_statuses
                            if isinstance(row, dict) and str(row.get("symbol") or "").strip()
                        ],
                        key=lambda row: float(row.get("gap_from_prev_close_pct", 0.0) or 0.0),
                        reverse=True,
                    )
                    def _gap_row_text(row: dict[str, object]) -> str:
                        symbol = str(row.get("symbol") or "").strip()
                        gap_pct = float(row.get("gap_from_prev_close_pct", 0.0) or 0.0)
                        chase_tag = " [추격금지]" if _row_blocker_keys(row) & chase_risk_keys else ""
                        return f"{symbol} {gap_pct:+.2f}%{chase_tag}"

                    gap_up_text = " | ".join(
                        _gap_row_text(row)
                        for row in gap_leaders[:3]
                    ) or "-"
                    gap_down_text = " | ".join(
                        _gap_row_text(row)
                        for row in list(reversed(gap_leaders[-3:]))
                    ) or "-"
                    opening_msg = (
                        "OPENING_MARKET_BRIEF\n"
                        f"[오프닝 브리프] {now_kst.strftime('%Y-%m-%d %H:%M:%S')} KST\n"
                        f"세션: {_humanize_session_phase(session_phase)} | 운용: {_humanize_bias_mode(market_bias_mode)} | 실주문: {'ON' if settings.live_armed else 'OFF'}\n"
                        f"시장: {regime} (conf {regime_confidence:.2f}, idx {regime_index_pct:+.2f}%) | 선정상태: {'LOCKED' if selection_locked else 'PENDING'}\n"
                        f"포커스 종목: {','.join(report_symbols[:5]) if report_symbols else '-'}\n"
                        f"A급 오프닝 후보: {opening_a_grade_summary}\n"
                        f"시초 우선 관찰: {opening_priority_summary}\n"
                        f"갭상 리더: {gap_up_text}\n"
                        f"갭하 리더: {gap_down_text}\n"
                        f"동시 순매수 상위: {opening_priority_summary}\n"
                        f"수급: {flow_summary_text}\n"
                        f"VI: {vi_summary}\n"
                        f"시초 포커스: {opening_focus_summary}\n"
                        f"상위 차단: {blocker_text}\n"
                        f"WATCHLIST: {watchlist_text}"
                    )
                    _SLACK_NOTIFIER.send(opening_msg, force=True)
                    _event(state, f"OPENING_MARKET_BRIEF_SENT {today_key}")
                    last_opening_brief_day = today_key

            # Hourly Slack market report: once per hour only while the KRX market session is open.
            if _SLACK_NOTIFIER and settings.slack_enabled and bool(getattr(settings, "hourly_market_report_enabled", True)):
                now_dt = datetime.now()
                session_phase = str(session_ctx.get("phase", "OFF_HOURS"))
                if session_phase in {"OPENING_FOCUS", "REGULAR_SESSION", "CLOSE_GUARD"}:
                    hour_key = now_dt.strftime("%Y-%m-%d %H")
                    if hour_key != last_hourly_slack_key:
                        selection_locked = bool(last_selection_day == current_day and selected_symbols)
                        ranked_candidates = [
                            str(item.get("symbol") or "").strip()
                            for item in list(selection_detail_state.get("top_ranked", []))[:5]
                            if isinstance(item, dict) and str(item.get("symbol") or "").strip()
                        ]
                        report_symbols = (
                            list(selected_symbols)
                            if selection_locked
                            else list(dict.fromkeys(ranked_candidates + list(ledger.positions.keys())))
                        )
                        primary = (
                            report_symbols[0]
                            if report_symbols
                            else ("SELECTION_PENDING" if not selection_locked else settings.symbol)
                        )
                        top_watch = []
                        for sym in list(report_symbols)[:3]:
                            ff = factor_map_state.get(sym, {}) if isinstance(factor_map_state.get(sym, {}), dict) else {}
                            if not ff and isinstance(selection_detail_state.get("top_ranked"), list):
                                ff = next(
                                    (
                                        item for item in list(selection_detail_state.get("top_ranked", []))
                                        if isinstance(item, dict) and str(item.get("symbol") or "").strip() == sym
                                    ),
                                    {},
                                )
                            top_watch.append(
                                f"{sym}(score={float(score_map.get(sym, 0.0)):+.2f},"
                                f"mom={float(ff.get('momentum_pct', 0.0)):+.1f}%,"
                                f"ram={float(ff.get('risk_adjusted_momentum', 0.0)):.2f},"
                                f"tef={float(ff.get('trend_efficiency', 0.0)):.2f},"
                                f"tqp={float(ff.get('top_rank_quality_penalty', 0.0)):.2f},"
                                f"trd={float(ff.get('trend_pct', 0.0)):+.1f}%,"
                                f"rsi={float(ff.get('daily_rsi', 50.0)):.1f})"
                            )
                        blockers = sorted(reason_counter.items(), key=lambda x: x[1], reverse=True)[:3]
                        blocker_text = ",".join(f"{_humanize_blocker(k)}:{v}" for k, v in blockers) if blockers else "-"
                        trades_today = _trade_count_on_day(ledger, now_dt.strftime("%Y-%m-%d"))
                        selection_status = "LOCKED" if selection_locked else "PENDING"
                        watched_rows = [
                            row for row in stock_statuses
                            if isinstance(row, dict) and bool(row.get("watchlist", False))
                        ]
                        watchlist_text = " | ".join(
                            f"{str(row.get('symbol') or '').strip()}:{_humanize_watch_reason(row.get('watch_reason'))}"
                            for row in watched_rows[:3]
                            if str(row.get("symbol") or "").strip()
                        ) or "-"
                        breadth_pool = [
                            row for row in stock_statuses
                            if isinstance(row, dict)
                        ]
                        internal_breadth = (
                            sum(1 for row in breadth_pool if bool(row.get("factor_trend_ok", False))) / float(len(breadth_pool)) * 100.0
                            if breadth_pool else 0.0
                        )
                        sector_counts: dict[str, int] = {}
                        for sym in report_symbols[:5]:
                            sector = str(symbol_sector_map.get(sym, "UNMAPPED")).strip() or "UNMAPPED"
                            sector_counts[sector] = int(sector_counts.get(sector, 0)) + 1
                        sector_heat = ",".join(
                            f"{sector}:{count}"
                            for sector, count in sorted(sector_counts.items(), key=lambda item: item[1], reverse=True)[:3]
                        ) or "-"
                        market_types = [
                            str(row.get("market_type") or "").strip()
                            for row in breadth_pool[:5]
                            if isinstance(row, dict) and str(row.get("market_type") or "").strip()
                        ]
                        market_type = _humanize_market_type(market_types[0] if market_types else "NEUTRAL")
                        session_profile_text = _humanize_bias_mode(market_bias_mode)
                        phase_text = _humanize_session_phase(session_phase)
                        shock_text = str(session_ctx.get("shock_reason") or "").strip() or "-"
                        flow_text = flow_summary_text
                        vi_text = vi_summary
                        intraday_focus_text = opening_focus_summary
                        focus_label = "selected" if selection_locked else "watch"
                        msg = (
                            "HOURLY_MARKET_REPORT\n"
                            f"[장중 브리프] {now_dt.strftime('%Y-%m-%d %H:%M:%S')} KST\n"
                            f"세션: {phase_text} | 운용: {session_profile_text} | 모드: {settings.trade_mode} | 실주문: {'ON' if settings.live_armed else 'OFF'}\n"
                            f"시장: {regime} (conf {regime_confidence:.2f}, idx {regime_index_pct:+.2f}%) | 타입: {market_type} | breadth {internal_breadth:.1f}%\n"
                            f"섹터 heat: {sector_heat}\n"
                            f"수급: {flow_text}\n"
                            f"VI: {vi_text}\n"
                            f"장중 포커스: {intraday_focus_text}\n"
                            f"충격장: {'YES' if bool(session_ctx.get('shock_active', False)) else 'NO'} | 사유: {shock_text}\n"
                            f"선정상태: {selection_status} | 기준: {strategy_reference or '-'}\n"
                            f"포커스: {primary} | {focus_label}: {','.join(report_symbols) if report_symbols else '-'}\n"
                            f"체결: {trades_today}건 | 보유: {_position_count(ledger)} | 자산: {state.equity:.0f} | P&L: {state.total_pnl:+.0f} ({state.total_return_pct:+.2f}%)\n"
                            f"상위 차단: {blocker_text}\n"
                            f"WATCHLIST: {watchlist_text}\n"
                            f"상위 후보: {' | '.join(top_watch) if top_watch else '-'}"
                        )
                        _SLACK_NOTIFIER.send(msg, force=True)
                        _event(state, f"HOURLY_MARKET_REPORT_SENT {hour_key}")
                        last_hourly_slack_key = hour_key

            # Daily US mock-trading report to Slack at configured time.
            if _SLACK_NOTIFIER and settings.slack_enabled and settings.us_mock_enabled:
                now_dt = datetime.now()
                today_key = now_dt.strftime("%Y-%m-%d")
                yesterday_key = (now_dt - timedelta(days=1)).strftime("%Y-%m-%d")
                if (
                    now_dt.hour == int(settings.us_mock_report_hour)
                    and now_dt.minute >= int(settings.us_mock_report_minute)
                    and today_key != last_us_mock_report_day
                ):
                    try:
                        rep = _run_us_mock_daily_report(settings)
                        if bool(rep.get("ok")):
                            kr = _ledger_day_summary(ledger, yesterday_key)
                            msg = (
                                "CROSS_MARKET_DAILY "
                                + f"time={now_dt.strftime('%Y-%m-%d %H:%M:%S')} "
                                + f"kr_day={yesterday_key} "
                                + f"kr_trades={int(kr.get('trades', 0))} "
                                + f"kr_buys={int(kr.get('buys', 0))} "
                                + f"kr_sells={int(kr.get('sells', 0))} "
                                + f"kr_realized={float(kr.get('realized_pnl', 0.0)):+.0f} "
                                + f"kr_sell_win={float(kr.get('sell_win_rate_pct', 0.0)):.1f}% "
                                + f"window={int(rep.get('lookback_days', 0))}d_backtest "
                                + f"cash=${float(rep.get('initial_cash', 0.0)):,.0f} "
                                + f"equity=${float(rep.get('final_equity', 0.0)):,.0f} "
                                + f"ret={float(rep.get('total_return_pct', 0.0)):+.2f}% "
                                + f"trades_window={int(rep.get('trade_count', 0))} "
                                + f"trades_yesterday={int(rep.get('trade_count_last_session', 0))} "
                                + f"sells_yesterday={int(rep.get('sell_count_last_session', 0))} "
                                + f"win={float(rep.get('avg_win_rate_pct', 0.0)):.1f}% "
                                + f"avg_mdd={float(rep.get('avg_mdd_pct', 0.0)):.2f}% "
                                + f"best={rep.get('best_symbol', '-')}"
                                + f"({float(rep.get('best_ret_pct', 0.0)):+.2f}%) "
                                + f"worst={rep.get('worst_symbol', '-')}"
                                + f"({float(rep.get('worst_ret_pct', 0.0)):+.2f}%) "
                                + f"symbols={','.join([str(x) for x in list(rep.get('symbols', []))])}"
                            )
                            _SLACK_NOTIFIER.send(msg, force=True)
                            _event(state, f"US_MOCK_REPORT_SENT {today_key}")
                        else:
                            _event(state, f"US_MOCK_REPORT_FAIL {rep.get('error', 'unknown')}")
                    except Exception as us_exc:
                        _event(state, f"US_MOCK_REPORT_FAIL {us_exc}")
                    last_us_mock_report_day = today_key

            # KRX post-market review snapshot once per day after 15:40 KST.
            today_kst = now_kst.strftime("%Y-%m-%d")
            if now_kst.hour == 15 and now_kst.minute >= 40 and last_post_market_review_day != today_kst:
                day_sum = _ledger_day_summary(ledger, today_kst)
                opening_review = _post_market_candidate_review(stock_statuses, ledger, today_kst)
                review_days = [row for row in list(opening_review_history.get("days") or []) if isinstance(row, dict)]
                review_entry = {
                    "day": today_kst,
                    "opening_review": opening_review,
                    "trades": int(day_sum.get("trades", 0)),
                    "realized_pnl": round(float(day_sum.get("realized_pnl", 0.0)), 2),
                    "sell_win_rate_pct": round(float(day_sum.get("sell_win_rate_pct", 0.0)), 2),
                    "equity": round(float(state.equity), 2),
                    "total_pnl": round(float(state.total_pnl), 2),
                }
                replaced = False
                for idx, row in enumerate(review_days):
                    if str(row.get("day") or "") == today_kst:
                        review_days[idx] = review_entry
                        replaced = True
                        break
                if not replaced:
                    review_days.append(review_entry)
                review_days.sort(key=lambda row: str(row.get("day") or ""))
                opening_review_history = {"days": review_days[-260:]}
                _save_opening_review_history(opening_review_history_path, opening_review_history)
                review_msg = (
                    "POST_MARKET_REVIEW "
                    + f"day={today_kst} "
                    + f"trades={int(day_sum.get('trades', 0))} "
                    + f"buys={int(day_sum.get('buys', 0))} "
                    + f"sells={int(day_sum.get('sells', 0))} "
                    + f"realized={float(day_sum.get('realized_pnl', 0.0)):+.0f} "
                    + f"sell_win={float(day_sum.get('sell_win_rate_pct', 0.0)):.1f}% "
                    + f"equity={state.equity:.0f} total_pnl={state.total_pnl:+.0f} ({state.total_return_pct:+.2f}%) "
                    + f"opening_review={opening_review}"
                )
                _event(state, review_msg)
                if _SLACK_NOTIFIER and settings.slack_enabled:
                    _SLACK_NOTIFIER.send(review_msg, force=True)
                last_post_market_review_day = today_kst

            _save_ledger(ledger_path, ledger)
            stop_event.wait(settings.poll_seconds)
        except Exception as exc:
            state.last_error = str(exc)
            _event(state, f"Loop error: {exc}")
            stop_event.wait(settings.poll_seconds)

    state.running = False
    _event(state, "Bot stopped.")
    _release_runtime_lock(runtime_lock)
