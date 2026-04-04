from __future__ import annotations

import csv
import html
import io
import json
import logging
import os
import re
import socket
import ssl
import requests
import statistics
import threading
import time
import secrets
from collections import deque
from dataclasses import asdict
from datetime import datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from json import dumps
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from backtest_compare import (
    _prepare_market_data,
    generate_daily_selection_portfolio_report,
    generate_intraday_selected_replay_report,
    generate_rank_weighted_portfolio_study,
    generate_rolling_rank_study,
    generate_short_horizon_rank_study,
    generate_short_term_trade_report,
)
from bot_runtime import BotState, _multi_factor_rank_score, _trend_strategy_metrics, run_bot
from config import load_settings, save_runtime_overrides, selection_universe_symbols
from kiwoom_api import KiwoomAPI


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _symbol_name_cache_path() -> Path:
    return Path(os.getenv("SYMBOL_NAME_CACHE_PATH", "data/symbol_name_cache.json"))


def _load_symbol_name_cache(path: Path) -> dict[str, str]:
    try:
        if not path.exists():
            return {}
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            return {}
        out: dict[str, str] = {}
        for k, v in raw.items():
            code = str(k).strip()
            name = str(v).strip()
            if code and name:
                out[code] = name
        return out
    except Exception:
        return {}


def _save_symbol_name_cache(path: Path, mapping: dict[str, str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2))
    except Exception:
        pass


def _fetch_kind_symbol_name_map() -> dict[str, str]:
    source_url = (
        os.getenv("AUTO_UNIVERSE_SOURCE_URL", "").strip()
        or "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13"
    )
    try:
        resp = requests.get(source_url, timeout=20)
        resp.raise_for_status()
        text = resp.content.decode("cp949", "ignore")
    except Exception:
        return {}
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.S | re.I)
    out: dict[str, str] = {}
    for row in rows[1:]:
        cells = [
            re.sub(r"<[^>]+>", "", html.unescape(x)).strip()
            for x in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)
        ]
        if len(cells) < 3:
            continue
        name = cells[0].strip()
        code = cells[2].strip()
        if re.fullmatch(r"\d{6}", code) and name:
            out[code] = name
    return out


def _runtime_overrides() -> dict[str, str]:
    path = Path(os.getenv("RUNTIME_CONFIG_PATH", "data/runtime_config.json"))
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in data.items():
        key = str(k).strip().upper()
        if not key:
            continue
        out[key] = str(v).strip()
    return out


def _trusted_web_devices_path() -> Path:
    return Path(os.getenv("WEB_TRUSTED_DEVICES_PATH", "data/web_trusted_devices.json"))


def _load_trusted_web_devices() -> dict[str, dict[str, str]]:
    path = _trusted_web_devices_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for token, meta in raw.items():
        tok = str(token).strip()
        if not tok or not isinstance(meta, dict):
            continue
        out[tok] = {str(k): str(v) for k, v in meta.items()}
    return out


def _save_trusted_web_devices(devices: dict[str, dict[str, str]]) -> None:
    path = _trusted_web_devices_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(devices, ensure_ascii=False, indent=2))


def _cleanup_trusted_web_devices(
    devices: dict[str, dict[str, str]],
    *,
    ttl_days: int,
    max_devices: int,
) -> dict[str, dict[str, str]]:
    now = time.time()
    ttl_seconds = max(1, ttl_days) * 86400
    cleaned: dict[str, dict[str, str]] = {}
    for token, meta in devices.items():
        try:
            last_seen = float(str(meta.get("last_seen_ts") or meta.get("created_ts") or "0"))
        except Exception:
            last_seen = 0.0
        if last_seen and now - last_seen <= ttl_seconds:
            cleaned[token] = dict(meta)
    if len(cleaned) <= max_devices:
        return cleaned
    ranked = sorted(
        cleaned.items(),
        key=lambda item: float(str(item[1].get("last_seen_ts") or item[1].get("created_ts") or "0") or 0.0),
        reverse=True,
    )
    return dict(ranked[: max(1, max_devices)])


def _trusted_device_rows_html(devices: dict[str, dict[str, str]]) -> str:
    if not devices:
        return "<tr><td colspan='5'>등록된 신뢰 기기가 없습니다.</td></tr>"
    ranked = sorted(
        devices.items(),
        key=lambda item: float(str(item[1].get("last_seen_ts") or item[1].get("created_ts") or "0") or 0.0),
        reverse=True,
    )
    rows = []
    for token, meta in ranked:
        label = html.escape(str(meta.get("label") or "모바일 기기").strip() or "모바일 기기")
        last_ip = html.escape(str(meta.get("last_ip") or "-").strip() or "-")
        created_at = html.escape(str(meta.get("created_at") or "-").strip() or "-")
        last_seen_at = html.escape(str(meta.get("last_seen_at") or "-").strip() or "-")
        token_html = html.escape(token)
        rows.append(
            "<tr>"
            "<td>"
            f"<form method='post' action='/trusted-device-rename' style='display:flex;gap:8px;align-items:center;margin:0'>"
            f"<input type='hidden' name='token' value='{token_html}' />"
            f"<input type='text' name='label' value='{label}' style='min-width:140px;padding:8px 10px;border-radius:10px;border:1px solid #33424e;background:#10171d;color:#eef3f7' />"
            "<button type='submit' class='secondary-btn' style='padding:8px 10px;font-size:12px'>이름 저장</button>"
            "</form>"
            "</td>"
            f"<td>{last_ip}</td>"
            f"<td>{created_at}</td>"
            f"<td>{last_seen_at}</td>"
            "<td>"
            f"<form method='post' action='/trusted-device-remove' style='margin:0'>"
            f"<input type='hidden' name='token' value='{token_html}' />"
            "<button type='submit' class='danger-btn' style='padding:8px 10px;font-size:12px'>접근 해제</button>"
            "</form>"
            "</td>"
            "</tr>"
        )
    return "".join(rows)


def _to_float(value: object) -> float:
    text = str(value or "0").replace(",", "").strip()
    if text in {"", "+", "-"}:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _to_int(value: object) -> int:
    return int(_to_float(value))


def _display_label(value: object, *, kind: str = "generic") -> str:
    text = str(value or "").strip().upper()
    if kind == "regime":
        return {
            "BULLISH": "강세",
            "BEARISH": "약세",
            "NEUTRAL": "중립",
            "UNKNOWN": "미판정",
        }.get(text, text or "-")
    if kind == "risk_on":
        return {
            "RISK-ON": "위험선호",
            "RISK-OFF": "위험회피",
            "NEUTRAL": "중립",
        }.get(text, text or "-")
    if kind == "mode":
        return {"LIVE": "실거래", "DRY": "모의투자"}.get(text, text or "-")
    if kind == "bool":
        return {"ON": "활성", "OFF": "비활성", "UP": "정상", "DOWN": "중지"}.get(text, text or "-")
    return text or "-"


def _display_text(value: object, fallback: str = "데이터 수집 중") -> str:
    text = str(value or "").strip()
    if text in {"", "-", "None", "NONE", "null", "NULL"}:
        return fallback
    return text


def _display_number(value: object, fallback: str = "집계 전", digits: int = 1) -> str:
    if value in {None, "", "-", "None", "NONE", "null", "NULL"}:
        return fallback
    num = _to_float(value)
    if abs(num) < 1e-12:
        return fallback
    return f"{num:.{digits}f}"


def _display_money_pair(left: object, right: object, fallback: str = "잔고 동기화 중") -> str:
    left_num = _to_float(left)
    right_num = _to_float(right)
    if abs(left_num) < 1e-12 and abs(right_num) < 1e-12:
        return fallback
    return f"{left_num:,.1f} / {right_num:,.1f}"


def _factor_metric_text(
    row: dict[str, object],
    key: str,
    *,
    fmt: str,
    fallback: str = "-",
) -> str:
    if not bool(row.get("factor_data_ready")):
        return fallback
    value = row.get(key)
    if value in {None, "", "-", "None", "NONE", "null", "NULL"}:
        return fallback
    try:
        return fmt.format(_to_float(value))
    except Exception:
        return fallback


def _is_blankish(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() in {"", "-", "None", "NONE", "null", "NULL"}
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) == 0
    return False


def _load_json_report(path: str) -> dict[str, object]:
    report_path = Path(path)
    if not report_path.exists():
        return {}
    try:
        data = json.loads(report_path.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _ts_text(value: object) -> str:
    text = str(value or "").strip()
    return text if text else ""


def _simulation_run_config_path() -> Path:
    return Path("data") / "simulation_run_config.json"


def _simulation_report_catalog() -> dict[str, dict[str, object]]:
    return {
        "short_term": {
            "label": "단기 종목 리포트",
            "description": "현재 선택 로직으로 상위 종목을 뽑아 종목별 백테스트를 실행합니다.",
            "report_path": "data/short_term_trade_report_top100.json",
            "strategy_label": "Top1 우선 선별 + 종목별 단기 매매",
        },
        "rolling_rank": {
            "label": "롤링 랭킹 스터디",
            "description": "최근 N거래일 동안 매일 다시 랭킹을 만들고 forward 성과를 검증합니다.",
            "report_path": "data/rolling_rank_study_last20.json",
            "strategy_label": "일별 재선정 + forward 1/3/5일 검증",
        },
        "short_horizon": {
            "label": "초단기 랭킹 스터디",
            "description": "매일 재선정된 후보의 1일/2일 보유 성과를 집중 검증합니다.",
            "report_path": "data/rolling_rank_short_horizon_last20.json",
            "strategy_label": "일별 재선정 + 1~2일 단기 보유",
        },
        "daily_selection": {
            "label": "일일 재선정 포트폴리오",
            "description": "매일 종목을 다시 고르고 포트폴리오 단위로 BUY/SELL/보유를 누적 관리합니다.",
            "report_path": "data/daily_selection_portfolio_last20.json",
            "strategy_label": "일일 재선정 + 포트폴리오 회전형",
        },
        "rank_weighted": {
            "label": "랭크 가중 포트폴리오",
            "description": "top1/top2/top3를 가중치로 묶어 포트폴리오 기대값을 검증합니다.",
            "report_path": "data/rank_weighted_portfolio_last20.json",
            "strategy_label": "top1/2/3 가중 포트폴리오",
        },
        "intraday_replay": {
            "label": "선정 종목 2분 리플레이",
            "description": "실전에서 저장한 선정 종목 2분 가격과 BUY 신호를 바탕으로 intraday replay 결과를 요약합니다.",
            "report_path": "data/intraday_selected_replay.json",
            "strategy_label": "선정 종목 2분 데이터 리플레이",
        },
        "intraday_scalping": {
            "label": "스캘핑 인트라데이 리플레이",
            "description": "선정 종목 2분 데이터에서 스캘핑 전략을 재생하여 매매 성과를 분석합니다.",
            "report_path": "data/intraday_scalping_report.json",
            "strategy_label": "스캘핑 단기 매매 재생",
        },
    }


def _load_simulation_run_config() -> dict[str, object]:
    path = _simulation_run_config_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _simulation_profile_prefs(sim_prefs: dict[str, object], profile: str) -> dict[str, object]:
    if not isinstance(sim_prefs, dict):
        return {}
    profiles = sim_prefs.get("profiles")
    if isinstance(profiles, dict):
        scoped = profiles.get(profile)
        if isinstance(scoped, dict):
            return scoped
        return {}
    return sim_prefs


def _simulation_strategy_human_label(value: object) -> str:
    text = str(value or "").strip()
    return {
        "top1_priority_with_watchlist": "Top1 우선 선별 + 보조 후보 감시",
        "short_horizon_trend_follow": "단기 추세 추종형 진입",
    }.get(text, text or "-")


def _simulation_strategy_human_detail(value: object) -> str:
    text = str(value or "").strip()
    return {
        "top1_priority_with_watchlist": "상위 1개 후보를 실전 우선으로 보고, 2~3위는 보조 후보로 감시합니다.",
        "short_horizon_trend_follow": "강한 종목만 짧게 따라가고, 추격·과열·충격 리스크는 빠르게 차단합니다.",
    }.get(text, text or "-")


def _simulation_guard_human_label(value: object) -> str:
    text = str(value or "").strip()
    return {
        "shock_reversal_risk": "충격 반전 리스크",
        "event_spike_exhaustion": "이벤트 과열 소진",
        "market_surge_chase": "시장 급등 추격 금지",
        "late_chase": "늦은 추격 금지",
        "mid_band_continuation": "중단 밴드 continuation 과열",
        "mid_band_late_chase": "중단 밴드 늦은 추격",
        "inefficient_trend": "비효율 추세",
        "high_rsi_upper_band": "고 RSI 상단 과열",
    }.get(text, text or "-")


def _simulation_profile_why_text(profile: str) -> str:
    return {
        "short_term": "현재 선별 로직이 개별 종목 단위 백테스트에서도 일관된 기대값을 만드는지 빠르게 확인합니다.",
        "rolling_rank": "매일 후보를 다시 고르는 선택 로직이 익일·수일 성과로 이어지는지 검증합니다.",
        "short_horizon": "지금 전략이 1~2일 보유형에 맞는지, 단기 추세 추종 품질을 집중 점검합니다.",
        "daily_selection": "실전과 가장 비슷한 일일 재선정·포트폴리오 회전 흐름을 재현해 병목을 찾습니다.",
        "rank_weighted": "top1 중심 전략이 top2~top3 분산보다 유리한지 포트폴리오 배분 관점에서 비교합니다.",
        "intraday_replay": "실전에서 저장한 2분 선택 종목과 BUY 신호가 intraday 기준으로 어떤 기대값을 만들었는지 확인합니다.",
    }.get(profile, "현재 프로필이 실전 판단과 얼마나 가까운지 확인합니다.")


def _simulation_profile_scope_text(profile: str) -> str:
    return {
        "short_term": "핵심 입력은 top, seed, 데이터 기간입니다. 아래 결과 카드는 실제 실행 후 갱신됩니다. window는 직접 쓰지 않고 종목별 120일 기준으로 평가되며, 데이터 기간은 최소 260일로 보정됩니다.",
        "rolling_rank": "핵심 입력은 기간, seed, 데이터 기간입니다. 아래 결과 카드는 실제 실행 후 갱신됩니다.",
        "short_horizon": "핵심 입력은 기간, seed, 데이터 기간, 최대보유일입니다. 아래 결과 카드는 실제 실행 후 갱신됩니다.",
        "daily_selection": "핵심 입력은 기간, seed, 데이터 기간, 최대보유일, 완화/probe 옵션입니다. 아래 결과 카드는 실제 실행 후 갱신됩니다.",
        "rank_weighted": "핵심 입력은 기간, seed, 데이터 기간, rank weights입니다. 아래 결과 카드는 실제 실행 후 갱신됩니다.",
        "intraday_replay": "핵심 입력은 특정 일자 또는 기간(window)입니다. 저장된 2분 선택 종목 데이터를 읽어 마지막 실행 결과 카드와 거래표를 갱신합니다.",
    }.get(profile, "프로필을 바꾸면 입력칸과 설명은 즉시 바뀌고, 상세 결과 카드는 마지막 실행 리포트 기준으로 갱신됩니다.")


def _simulation_profile_form_title(profile: str) -> str:
    return {
        "short_term": "단기 리포트 설정",
        "rolling_rank": "롤링 랭킹 설정",
        "short_horizon": "초단기 랭킹 설정",
        "daily_selection": "일일 재선정 설정",
        "rank_weighted": "랭크 가중 설정",
        "intraday_replay": "2분 리플레이 설정",
    }.get(profile, "시뮬레이션 설정")


def _simulation_profile_form_hint(profile: str) -> str:
    return {
        "short_term": "이 프로필은 top / seed / 데이터 기간만 맞추면 됩니다.",
        "rolling_rank": "이 프로필은 기간(window), seed, 데이터 기간만 확인하면 됩니다.",
        "short_horizon": "이 프로필은 기간(window), seed, 데이터 기간, 최대보유일을 확인하면 됩니다.",
        "daily_selection": "이 프로필은 기간(window), seed, 데이터 기간, 최대보유일, 완화/probe 옵션을 확인하면 됩니다.",
        "rank_weighted": "이 프로필은 기간(window), seed, 데이터 기간, rank weights를 확인하면 됩니다.",
        "intraday_replay": "이 프로필은 특정 일자 또는 기간(window)만 맞추면 저장된 2분 선택 데이터를 바로 리플레이합니다.",
    }.get(profile, "현재 프로필에 필요한 입력만 확인하면 됩니다.")


def _latest_simulation_result_profile(sim_catalog: dict[str, dict[str, object]], sim_prefs: dict[str, object]) -> str:
    preferred = str((sim_prefs or {}).get("profile") or "").strip().lower()
    if preferred in sim_catalog:
        return preferred
    latest_profile = "daily_selection"
    latest_key = ""
    for profile, meta in sim_catalog.items():
        report = _load_json_report(str(meta.get("report_path") or ""))
        updated = _ts_text(report.get("updated_at"))
        if updated and updated >= latest_key:
            latest_key = updated
            latest_profile = profile
    return latest_profile


def _comparison_tone_style(left: float, right: float, *, prefer_higher: bool = True) -> tuple[str, str]:
    if abs(left - right) < 1e-9:
        return ("color:#d6e1f5;", "color:#d6e1f5;")
    left_better = left > right if prefer_higher else left < right
    if left_better:
        return ("color:#86efac;font-weight:800;", "color:#fca5a5;font-weight:700;")
    return ("color:#fca5a5;font-weight:700;", "color:#86efac;font-weight:800;")


def _save_simulation_run_config(config: dict[str, object]) -> None:
    path = _simulation_run_config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    except Exception:
        pass


def _has_meaningful_account_snapshot(snapshot: dict[str, object]) -> bool:
    if not isinstance(snapshot, dict):
        return False
    if abs(_to_float(snapshot.get("cash_balance"))) > 1e-12:
        return True
    if abs(_to_float(snapshot.get("equity"))) > 1e-12:
        return True
    if abs(_to_float(snapshot.get("total_pnl"))) > 1e-12:
        return True
    if abs(_to_float(snapshot.get("total_return_pct"))) > 1e-12:
        return True
    if _to_int(snapshot.get("active_positions")) > 0:
        return True
    if str(snapshot.get("positions_summary") or "").strip():
        return True
    return False


def _account_snapshot_from_ledger(ledger: dict[str, object]) -> dict[str, object]:
    if not isinstance(ledger, dict):
        return {}
    cash = _to_float(ledger.get("cash"))
    initial_cash = _to_float(ledger.get("initial_cash"))
    realized_pnl = _to_float(ledger.get("realized_pnl"))
    positions_raw = ledger.get("positions")
    positions = positions_raw if isinstance(positions_raw, dict) else {}
    active_positions = sum(1 for row in positions.values() if int(_to_float((row or {}).get("qty"))) > 0)
    position_qty = sum(int(_to_float((row or {}).get("qty"))) for row in positions.values())
    position_symbol = ""
    if positions:
        sorted_symbols = sorted(
            positions.keys(),
            key=lambda s: int(_to_float((positions.get(s) or {}).get("qty"))),
            reverse=True,
        )
        position_symbol = str(sorted_symbols[0] or "") if sorted_symbols else ""
    positions_summary = ", ".join(
        f"{sym}:{int(_to_float((row or {}).get('qty')))}@{_to_float((row or {}).get('avg_price')):.1f}"
        for sym, row in sorted(positions.items())
        if int(_to_float((row or {}).get("qty"))) > 0
    )
    equity_history = ledger.get("equity_history")
    last_equity = 0.0
    if isinstance(equity_history, list) and equity_history:
        last = equity_history[-1]
        if isinstance(last, dict):
            last_equity = _to_float(last.get("equity"))
    equity = last_equity if last_equity > 0 else cash
    total_pnl = (equity - initial_cash) if initial_cash > 0 else realized_pnl
    total_return_pct = ((total_pnl / initial_cash) * 100.0) if initial_cash > 0 else 0.0
    snapshot = {
        "cash_balance": cash,
        "equity": equity,
        "unrealized_pnl": max(0.0, equity - cash - realized_pnl) if equity > 0 else 0.0,
        "realized_pnl": realized_pnl,
        "total_pnl": total_pnl,
        "total_return_pct": total_return_pct,
        "active_positions": active_positions,
        "position_qty": position_qty,
        "position_symbol": position_symbol,
        "positions_summary": positions_summary,
    }
    return snapshot if _has_meaningful_account_snapshot(snapshot) else {}


def _apply_account_snapshot_fallback(
    state: dict[str, object],
    persisted_ui: dict[str, object],
    ledger: dict[str, object] | None = None,
) -> dict[str, object]:
    snapshot = state.get("broker_account_snapshot", {}) if isinstance(state.get("broker_account_snapshot"), dict) else {}
    if not _has_meaningful_account_snapshot(snapshot):
        snapshot = persisted_ui.get("account_snapshot", {}) if isinstance(persisted_ui.get("account_snapshot"), dict) else {}
    if not _has_meaningful_account_snapshot(snapshot) and isinstance(ledger, dict):
        snapshot = _account_snapshot_from_ledger(ledger)
    if not _has_meaningful_account_snapshot(snapshot):
        return state
    cash_empty = abs(_to_float(state.get("cash_balance"))) < 1e-12
    equity_empty = abs(_to_float(state.get("equity"))) < 1e-12
    pnl_empty = abs(_to_float(state.get("total_pnl"))) < 1e-12 and abs(_to_float(state.get("total_return_pct"))) < 1e-12
    positions_empty = (
        _to_int(state.get("active_positions")) <= 0
        and not str(state.get("positions_summary") or "").strip()
    )
    if cash_empty:
        state["cash_balance"] = _to_float(snapshot.get("cash_balance"))
    if equity_empty:
        state["equity"] = _to_float(snapshot.get("equity"))
    if pnl_empty:
        state["total_pnl"] = _to_float(snapshot.get("total_pnl"))
        state["total_return_pct"] = _to_float(snapshot.get("total_return_pct"))
        state["unrealized_pnl"] = _to_float(snapshot.get("unrealized_pnl"))
        state["realized_pnl"] = _to_float(snapshot.get("realized_pnl"))
    if positions_empty:
        state["active_positions"] = _to_int(snapshot.get("active_positions"))
        state["position_qty"] = _to_int(snapshot.get("position_qty"))
        state["positions_summary"] = str(snapshot.get("positions_summary") or "")
        state["position_symbol"] = str(snapshot.get("position_symbol") or "")
    return state


def _load_ledger_snapshot(path: str) -> dict[str, object]:
    ledger_path = Path(path)
    if not ledger_path.exists():
        return {}
    try:
        data = json.loads(ledger_path.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _last_known_ui_path() -> Path:
    return Path(os.getenv("LAST_KNOWN_UI_PATH", "data/last_known_ui_state.json"))


def _load_last_known_ui() -> dict[str, object]:
    path = _last_known_ui_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_last_known_ui(snapshot: dict[str, object]) -> None:
    path = _last_known_ui_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))


def _has_meaningful_payload(value: object) -> bool:
    if isinstance(value, dict):
        return any(_has_meaningful_payload(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_meaningful_payload(v) for v in value)
    if isinstance(value, str):
        return bool(value.strip())
    return value not in {None, 0, 0.0, False}


def _simulation_trade_type_label(value: object) -> str:
    text = str(value or "").strip()
    return {
        "controlled_chase_or_pullback": "단기 추세/눌림",
        "bearish_exception": "약세 예외",
    }.get(text, text or "-")


def _live_transaction_rows(ledger: dict[str, object]) -> list[dict[str, object]]:
    trades = list(ledger.get("trades") or []) if isinstance(ledger.get("trades"), list) else []
    buy_lots: dict[str, list[dict[str, object]]] = {}
    closed: list[dict[str, object]] = []
    for row in trades:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip()
        side = str(row.get("side") or "").strip().upper()
        qty = max(0, _to_int(row.get("qty")))
        price = _to_float(row.get("price"))
        ts = str(row.get("ts") or "").strip()
        if not symbol or qty <= 0:
            continue
        if side == "BUY":
            buy_lots.setdefault(symbol, []).append(
                {
                    "ts": ts,
                    "qty": qty,
                    "price": price,
                    "entry_mode": str(row.get("entry_mode") or row.get("strategy_profile") or ""),
                    "setup_state": str(row.get("setup_state") or row.get("sentiment_class") or ""),
                    "a_grade_opening": bool(row.get("a_grade_opening", False)),
                }
            )
            continue
        if side != "SELL":
            continue
        remaining = qty
        matched_buy = None
        while remaining > 0 and buy_lots.get(symbol):
            lot = buy_lots[symbol][0]
            lot_qty = max(0, _to_int(lot.get("qty")))
            if lot_qty <= 0:
                buy_lots[symbol].pop(0)
                continue
            matched_buy = matched_buy or lot
            if lot_qty <= remaining:
                remaining -= lot_qty
                buy_lots[symbol].pop(0)
            else:
                lot["qty"] = lot_qty - remaining
                remaining = 0
        closed.append(
            {
                "symbol": symbol,
                "buy_ts": str((matched_buy or {}).get("ts") or ""),
                "sell_ts": ts,
                "buy_price": _to_float((matched_buy or {}).get("price")),
                "sell_price": price,
                "qty": qty,
                "realized_pnl": _to_float(row.get("realized_pnl")),
                "return_pct": _pct_from_prices(_to_float((matched_buy or {}).get("price")), price),
                "entry_mode": str((matched_buy or {}).get("entry_mode") or row.get("entry_mode") or row.get("strategy_profile") or ""),
                "setup_state": str((matched_buy or {}).get("setup_state") or row.get("setup_state") or row.get("sentiment_class") or ""),
                "regime": str(row.get("regime") or ""),
                "a_grade_opening": bool((matched_buy or {}).get("a_grade_opening") or row.get("a_grade_opening")),
            }
        )
    return closed


def _pct_from_prices(buy_price: float, sell_price: float) -> float:
    if buy_price <= 0:
        return 0.0
    return ((sell_price / buy_price) - 1.0) * 100.0


def _hold_days_label(buy_ts: object, sell_ts: object) -> str:
    buy_text = str(buy_ts or "").strip()
    sell_text = str(sell_ts or "").strip()
    if not buy_text or not sell_text:
        return "-"
    try:
        buy_dt = datetime.fromisoformat(buy_text.replace(" ", "T"))
        sell_dt = datetime.fromisoformat(sell_text.replace(" ", "T"))
    except Exception:
        return "-"
    days = max(0, (sell_dt.date() - buy_dt.date()).days)
    return f"{days}일"


def _weekday_label(text: object) -> str:
    raw = str(text or "").strip()
    if not raw:
        return "미상"
    try:
        dt = datetime.fromisoformat(raw.replace(" ", "T"))
    except Exception:
        return "미상"
    return ["월", "화", "수", "목", "금", "토", "일"][dt.weekday()]


def _slice_market_history(rows: list[dict[str, object]], market_range: str) -> list[dict[str, object]]:
    clean = [row for row in rows if isinstance(row, dict)]
    if not clean:
        return []
    key = str(market_range or "1w").strip().lower()
    if key in {"all", "max"}:
        return clean
    days_map = {"1d": 1, "1w": 7, "1m": 30}
    days = days_map.get(key, 7)
    last_text = str(clean[-1].get("updated_at") or "").strip()
    try:
        last_dt = datetime.fromisoformat(last_text.replace(" ", "T"))
    except Exception:
        return clean[-min(len(clean), 48 if days <= 1 else 120 if days <= 7 else 480) :]
    cutoff = last_dt - timedelta(days=days)
    subset = []
    for row in clean:
        text = str(row.get("updated_at") or "").strip()
        try:
            row_dt = datetime.fromisoformat(text.replace(" ", "T"))
        except Exception:
            continue
        if row_dt >= cutoff:
            subset.append(row)
    return subset or clean


def _market_status_from_snapshot(index_change_pct: float, breadth_ratio: float, sentiment_score: float) -> str:
    if sentiment_score >= 67.0 or (index_change_pct >= 0.7 and breadth_ratio >= 55.0):
        return "강세"
    if sentiment_score <= 33.0 or (index_change_pct <= -0.7 and breadth_ratio <= 45.0):
        return "약세"
    return "중립"


def _recent_close_series(symbol: object, *, market: str = "kr", limit: int = 24) -> list[float]:
    sym = str(symbol or "").strip()
    if not sym:
        return []
    cache_path = Path("data") / "backtest_cache" / f"{market.lower()}_{sym}_daily.json"
    if not cache_path.exists():
        return []
    try:
        raw = json.loads(cache_path.read_text())
    except Exception:
        return []
    bars = raw.get("bars") if isinstance(raw, dict) else None
    if not isinstance(bars, list):
        return []
    values: list[float] = []
    for row in bars[-max(5, limit):]:
        if not isinstance(row, dict):
            continue
        values.append(_to_float(row.get("close")))
    return [v for v in values if v > 0]


def _latest_known_price(symbol: object, *, market: str = "kr") -> float:
    series = _recent_close_series(symbol, market=market, limit=2)
    return float(series[-1]) if series else 0.0


def _trade_stats(trades: list[dict[str, object]], *, pnl_key: str, return_key: str) -> dict[str, float]:
    pnls = [_to_float((row or {}).get(pnl_key)) for row in trades]
    returns = [_to_float((row or {}).get(return_key)) for row in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    win_rate = (len(wins) / len(pnls) * 100.0) if pnls else 0.0
    profit_factor = (sum(wins) / abs(sum(losses))) if losses and abs(sum(losses)) > 0 else (999.0 if wins else 0.0)
    expectancy = (sum(pnls) / len(pnls)) if pnls else 0.0
    payoff = (avg_win / abs(avg_loss)) if avg_loss < 0 else (999.0 if avg_win > 0 else 0.0)
    avg_return = (sum(returns) / len(returns)) if returns else 0.0
    return {
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "payoff": payoff,
        "avg_return": avg_return,
    }


def _weekday_heatmap_html(items: list[tuple[str, float]]) -> str:
    labels = ["월", "화", "수", "목", "금"]
    value_map = {label: value for label, value in items}
    max_abs = max([abs(value_map.get(label, 0.0)) for label in labels] or [1.0]) or 1.0
    cells: list[str] = []
    for label in labels:
        value = value_map.get(label, 0.0)
        intensity = min(1.0, abs(value) / max_abs) if max_abs > 0 else 0.0
        if value > 0:
            bg = f"rgba(31, 211, 138, {0.18 + intensity * 0.42:.2f})"
            border = "#2c7a59"
        elif value < 0:
            bg = f"rgba(255, 126, 126, {0.18 + intensity * 0.42:.2f})"
            border = "#8b4747"
        else:
            bg = "#101a2b"
            border = "#22344d"
        cells.append(
            "<div class='weekday-cell' style='"
            f"background:{bg};border-color:{border}'>"
            f"<div class='weekday-name'>{html.escape(label)}</div>"
            f"<div class='weekday-value'>{value:+.2f}%</div>"
            "</div>"
        )
    return "<div class='weekday-heatmap'>" + "".join(cells) + "</div>"


def _trade_bar_date(symbol: object, bar_index: object, *, market: str = "kr") -> str:
    sym = str(symbol or "").strip()
    idx = _to_int(bar_index)
    if not sym or idx < 0:
        return "-"
    cache_path = Path("data") / "backtest_cache" / f"{market.lower()}_{sym}_daily.json"
    if not cache_path.exists():
        return "-"
    try:
        raw = json.loads(cache_path.read_text())
    except Exception:
        return "-"
    bars = raw.get("bars") if isinstance(raw, dict) else None
    if not isinstance(bars, list) or idx >= len(bars):
        return "-"
    row = bars[idx] if isinstance(bars[idx], dict) else {}
    date_text = str(row.get("date") or "").strip()
    return date_text or "-"


def _stock_readiness_meta(row: dict[str, object]) -> tuple[str, str, str]:
    action = str(row.get("action") or "HOLD").upper()
    trend_ok = bool(row.get("factor_trend_ok"))
    structure_ok = bool(row.get("factor_structure_ok"))
    breakout_ok = bool(row.get("factor_breakout_ok"))
    overheat = bool(row.get("factor_overheat"))
    overextended = bool(row.get("factor_overextended"))
    bearish_exception_ready = bool(row.get("factor_bearish_exception_ready"))
    confirm_progress = _to_int(row.get("confirm_progress"))
    confirm_needed = max(1, _to_int(row.get("confirm_needed")))

    if action == "BUY" and trend_ok and structure_ok and breakout_ok and not overheat:
        if confirm_progress >= confirm_needed:
            return ("ready", "매수 가능", "조건 충족, 진입 확인 완료")
        return ("watch", "진입 대기", "조건 충족, 확인 신호 대기")
    if action == "SELL":
        return ("exit", "청산 우선", "청산 또는 리스크 대응 구간")
    if overheat:
        return ("blocked", "과열 차단", "과열 조건으로 신규 진입 제한")
    if overextended:
        return ("blocked", "추격 과열", "단기 과열 연장으로 늦은 추격 진입을 제한")
    if bearish_exception_ready:
        return ("watch", "약세 예외 후보", "약세장에서도 상대강도가 높은 예외 후보")
    if not trend_ok or not structure_ok:
        return ("blocked", "추세 미충족", "추세 또는 구조 조건 미충족")
    return ("watch", "감시 중", "추가 확인 전 감시 구간")


def _stock_board_bucket(row: dict[str, object]) -> str:
    qty = _to_int(row.get("qty"))
    readiness, _, _ = _stock_readiness_meta(row)
    if qty > 0:
        return "holding"
    if bool(row.get("selected")):
        return "candidate"
    if readiness in {"blocked", "exit"}:
        return "blocked"
    return "candidate"


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
        "trend_entry_filter": "추세 진입 필터",
    }
    return mapping.get(key, text or "-")


def _format_flow_qty(value: object) -> str:
    num = _to_float(value)
    if abs(num) >= 1_000_000:
        return f"{num/1_000_000:+.2f}M주"
    if abs(num) >= 1_000:
        return f"{num/1_000:+.1f}K주"
    return f"{num:+.0f}주"


def _tone_style(value: object, *, strong: bool = False) -> str:
    num = _to_float(value)
    if num > 0:
        return "color:#9ef0b8;" if strong else "color:#86efac;"
    if num < 0:
        return "color:#ffb4b4;" if strong else "color:#fca5a5;"
    return "color:#d6e1f5;"


def _has_chase_risk(entry_blockers: list[object]) -> bool:
    keys = {
        str(x or "").split(":", 1)[-1].split("(", 1)[0].strip().lower()
        for x in entry_blockers
        if str(x).strip()
    }
    return bool(
        keys
        & {
            "late_chase",
            "mid_band_late_chase",
            "high_rsi_upper_band",
            "market_surge_chase",
            "strong_overextension",
            "overextended_continuation",
        }
    )


def _restore_monitored_symbols(
    monitored_symbols_raw: str,
    *,
    selection_detail: object = None,
    fallback_rows: object = None,
    target_count: int = 0,
) -> list[str]:
    restored: list[str] = []
    seen: set[str] = set()

    def _push(symbol: object) -> None:
        text = str(symbol or "").strip()
        if not text or text in seen:
            return
        seen.add(text)
        restored.append(text)

    for token in str(monitored_symbols_raw or "").split(","):
        _push(token)

    if isinstance(selection_detail, dict):
        for token in list(selection_detail.get("selected_symbols") or []):
            _push(token)

    desired = max(1, int(target_count or 0))
    fallback_list = list(fallback_rows) if isinstance(fallback_rows, list) else []
    if len(restored) < desired:
        for row in fallback_list[:desired]:
            if isinstance(row, dict):
                _push(row.get("symbol"))

    return restored


def _prefer_richer_rows(primary_rows: object, secondary_rows: object) -> list[dict[str, object]]:
    primary = [row for row in list(primary_rows) if isinstance(row, dict)] if isinstance(primary_rows, list) else []
    secondary = [row for row in list(secondary_rows) if isinstance(row, dict)] if isinstance(secondary_rows, list) else []
    return secondary if len(secondary) > len(primary) else primary


def _stock_status_card_html(
    row: dict[str, object],
    name_map: dict[str, str],
    intraday_chart_map: dict[str, dict[str, object]] | None = None,
) -> str:
    readiness, readiness_label, readiness_desc = _stock_readiness_meta(row)
    mini_series = _recent_close_series(row.get("symbol"))
    symbol = str(row.get("symbol") or "").strip()
    intraday_payload = (intraday_chart_map or {}).get(symbol, {}) if symbol else {}
    mini_chart = ""
    mini_summary = ""
    if bool(intraday_payload.get("available")):
        mini_chart = str(intraday_payload.get("chart") or "")
        mini_summary = str(intraday_payload.get("summary") or "")
    elif mini_series:
        mini_chart = _sparkline_svg(mini_series, color="#67e8f9", unit="", width=260, height=90)
    qty = _to_int(row.get("qty"))
    pinned_holding = qty > 0
    pinned_badge = "<span class='stock-badge pinned'>보유 유지</span>" if pinned_holding else ""
    pinned_desc = (
        "<div class='desc' style='margin-top:4px;color:#bfe7ff'>전량 청산 전까지 선정 종목으로 유지됩니다.</div>"
        if pinned_holding
        else ""
    )
    snapshot_source_label = str(row.get("snapshot_source_label") or "").strip()
    snapshot_updated_at = str(row.get("snapshot_updated_at") or "").strip()
    fallback_badges = (
        f"<span class='stock-badge {'stale' if snapshot_source_label else 'live'}'>{html.escape(snapshot_source_label or '실시간')}</span>"
        + (f"<span class='stock-badge stale'>{html.escape(snapshot_updated_at)}</span>" if snapshot_updated_at else "")
    )
    search_text = " ".join(
        [
            str(_symbol_label(str(row.get("symbol") or ""), name_map)),
            str(row.get("sector") or ""),
            str(row.get("action") or ""),
            str(row.get("decision_reason") or ""),
            str(row.get("watch_reason") or ""),
            str(readiness_label or ""),
        ]
    ).lower()
    watch_reason = _humanize_watch_reason(row.get("watch_reason"))
    market_type = _humanize_market_type(row.get("market_type"))
    entry_blockers = list(row.get("entry_blockers") or []) if isinstance(row.get("entry_blockers"), list) else []
    blocker_text = ", ".join(_humanize_blocker(x) for x in entry_blockers[:3] if str(x).strip())
    gap_text = f"{_to_float(row.get('gap_from_prev_close_pct')):+.2f}%"
    foreign_net = _to_float(row.get("foreign_net_qty"))
    institution_net = _to_float(row.get("institution_net_qty"))
    factor_data_ready = bool(row.get("factor_data_ready"))
    factor_bar_count = int(_to_float(row.get("factor_daily_bar_count")))
    factor_required_bars = max(1, int(_to_float(row.get("factor_required_daily_bars"))))
    factor_data_reason = str(row.get("factor_data_reason") or "").strip()
    factor_status_text = (
        f"일봉 {factor_bar_count}/{factor_required_bars} | {_display_text(factor_data_reason, 'ready')}"
        if factor_bar_count > 0
        else _display_text(factor_data_reason, "일봉 데이터 없음")
    )
    chase_risk = _has_chase_risk(entry_blockers)
    a_grade_opening = foreign_net > 0 and institution_net > 0 and (not bool(row.get("vi_active"))) and (not chase_risk)
    watch_badge = f"<span class='stock-badge watch'>{html.escape('WATCH ' + watch_reason)}</span>" if watch_reason and watch_reason != "-" else ""
    flow_sync_badge = (
        "<span class='stock-badge selected'>수급 동행</span>"
        if foreign_net > 0 and institution_net > 0
        else ""
    )
    a_grade_badge = (
        "<span class='stock-badge live'>A급 오프닝</span>"
        if a_grade_opening
        else ""
    )
    chase_risk_badge = (
        "<span class='stock-badge stale'>추격 금지</span>"
        if chase_risk
        else ""
    )
    market_type_badge = f"<span>시장타입 {html.escape(market_type)}</span>" if market_type else ""
    watch_reason_line = f"<div class='stock-reason'>Watchlist: {html.escape(watch_reason)}</div>" if watch_reason and watch_reason != "-" else ""
    blocker_line = f"<div class='stock-reason'>Blockers: {html.escape(blocker_text)}</div>" if blocker_text else ""
    flow_line = (
        "<div class='stock-reason'>"
        f"수급: 외인 {html.escape(_format_flow_qty(row.get('foreign_net_qty')))} / "
        f"기관 {html.escape(_format_flow_qty(row.get('institution_net_qty')))}"
        "</div>"
    )
    vi_gap_line = (
        "<div class='stock-reason'>"
        f"VI {'YES' if bool(row.get('vi_active')) else 'NO'} | "
        f"갭 {html.escape(gap_text)} | "
        f"시초강도 {html.escape(_display_text(row.get('opening_strength'), '-'))}"
        "</div>"
    )
    return (
        f"<div class='stock-card {readiness}' data-stock-search=\"{html.escape(search_text)}\">"
        "<div class='stock-head'>"
        "<div>"
        f"<div class='stock-title'>{html.escape(_symbol_label(str(row.get('symbol') or ''), name_map))}</div>"
        f"<div class='stock-sub'>{html.escape(str(row.get('sector') or 'UNMAPPED'))}</div>"
        "</div>"
        "<div class='stock-head-right'>"
        f"<span class='stock-badge {'selected' if row.get('selected') else 'watch'}'>{'선정' if row.get('selected') else '감시'}</span>"
        f"{pinned_badge}"
        f"{watch_badge}"
        f"{flow_sync_badge}"
        f"{a_grade_badge}"
        f"{chase_risk_badge}"
        f"<span class='readiness-pill {readiness}'>{html.escape(readiness_label)}</span>"
        f"<span class='signal-pill signal-{html.escape(str(row.get('action') or 'HOLD').lower())}'>{html.escape(str(row.get('action') or '-'))}</span>"
        f"{fallback_badges}"
        "</div>"
        "</div>"
        "<div class='stock-status-summary'>"
        f"<div class='label'>{html.escape(readiness_label)}</div>"
        f"<div class='desc'>{html.escape(readiness_desc)}</div>"
        f"{pinned_desc}"
        "</div>"
        "<div class='stock-price-row'>"
        f"<div><div class='k'>현재가</div><div class='v'>{_to_float(row.get('price')):,.0f}</div></div>"
        f"<div><div class='k'>점수</div><div class='v'>{_to_float(row.get('score')):+.2f}</div></div>"
        f"<div><div class='k'>수익률</div><div class='v'>{_to_float(row.get('return_pct')):+.2f}%</div></div>"
        f"<div><div class='k'>확인</div><div class='v'>{int(_to_float(row.get('confirm_progress')))} / {int(_to_float(row.get('confirm_needed')))}</div></div>"
        "</div>"
        + (
            f"<div class='stock-mini-chart'>{mini_chart}<div class='k' style='margin-top:6px'>{html.escape(mini_summary or '최근 타임라인')}</div></div>"
            if mini_chart
            else ""
        )
        + 
        "<div class='stock-metrics'>"
        f"<div class='metric-chip'><span>보유</span><strong>{int(_to_float(row.get('qty')))}주</strong></div>"
        f"<div class='metric-chip'><span>평단</span><strong>{_to_float(row.get('avg_price')):,.0f}</strong></div>"
        f"<div class='metric-chip'><span>RSI</span><strong>{_factor_metric_text(row, 'factor_daily_rsi', fmt='{:.1f}')}</strong></div>"
        f"<div class='metric-chip'><span>ATR14</span><strong>{_factor_metric_text(row, 'factor_atr14_pct', fmt='{:.2f}%')}</strong></div>"
        f"<div class='metric-chip'><span>RAM</span><strong>{_factor_metric_text(row, 'factor_risk_adjusted_momentum', fmt='{:.2f}')}</strong></div>"
        f"<div class='metric-chip'><span>TEF</span><strong>{_factor_metric_text(row, 'factor_trend_efficiency', fmt='{:.2f}')}</strong></div>"
        f"<div class='metric-chip'><span>TQP</span><strong>{_factor_metric_text(row, 'factor_top_rank_quality_penalty', fmt='{:.2f}')}</strong></div>"
        f"<div class='metric-chip'><span>관심도</span><strong>{_factor_metric_text(row, 'factor_attention_ratio', fmt='{:.2f}')}</strong></div>"
        f"<div class='metric-chip'><span>스파이크</span><strong>{_factor_metric_text(row, 'factor_value_spike_ratio', fmt='{:.2f}')}</strong></div>"
        f"<div class='metric-chip'><span>PQ</span><strong>{_factor_metric_text(row, 'factor_participation_quality', fmt='{:.2f}')}</strong></div>"
        f"<div class='metric-chip'><span>연장패널티</span><strong>{_factor_metric_text(row, 'factor_overextension_penalty', fmt='{:.2f}')}</strong></div>"
        f"<div class='metric-chip'><span>일봉</span><strong>{factor_bar_count} / {factor_required_bars}</strong></div>"
        f"<div class='metric-chip'><span>외인</span><strong style='{_tone_style(foreign_net, strong=True)}'>{html.escape(_format_flow_qty(foreign_net))}</strong></div>"
        f"<div class='metric-chip'><span>기관</span><strong style='{_tone_style(institution_net, strong=True)}'>{html.escape(_format_flow_qty(institution_net))}</strong></div>"
        f"<div class='metric-chip'><span>갭</span><strong style='{_tone_style(row.get('gap_from_prev_close_pct'))}'>{html.escape(gap_text)}</strong></div>"
        f"<div class='metric-chip'><span>VI</span><strong style='{'color:#ffd36b;' if bool(row.get('vi_active')) else 'color:#d6e1f5;'}'>{'YES' if bool(row.get('vi_active')) else 'NO'}</strong></div>"
        f"<div class='metric-chip'><span>데이터</span><strong>{int(_to_float(row.get('data_age_sec')))}s</strong></div>"
        f"<div class='metric-chip'><span>쿨다운</span><strong>{int(_to_float(row.get('cooldown_left_sec')))}s</strong></div>"
        "</div>"
        "<div class='stock-factor-line'>"
        f"<span>추세 {'-' if not factor_data_ready else (1 if row.get('factor_trend_ok') else 0)}</span>"
        f"<span>구조 {'-' if not factor_data_ready else (1 if row.get('factor_structure_ok') else 0)}</span>"
        f"<span>돌파 {'-' if not factor_data_ready else (1 if row.get('factor_breakout_ok') else 0)}</span>"
        f"<span>과열 {'-' if not factor_data_ready else (1 if row.get('factor_overheat') else 0)}</span>"
        f"<span>추격과열 {'-' if not factor_data_ready else (1 if row.get('factor_overextended') else 0)}</span>"
        f"<span>약세예외 {1 if row.get('factor_bearish_exception_ready') else 0}</span>"
        f"{market_type_badge}"
        "</div>"
        f"<div class='stock-reason'>Factor data: {html.escape(factor_status_text)}</div>"
        f"<div class='stock-reason'>Why: {html.escape(str(row.get('decision_reason') or '-'))}</div>"
        f"{flow_line}"
        f"{vi_gap_line}"
        f"{watch_reason_line}"
        f"{blocker_line}"
        "</div>"
    )


def _selection_reason_badges_html(
    row: dict[str, object],
    *,
    selected_symbols: set[str],
    selected_limit: int,
) -> str:
    symbol = str(row.get("symbol") or "").strip()
    rank = max(1, _to_int(row.get("rank")))
    momentum_pct = _to_float(row.get("momentum_pct"))
    ret5_pct = _to_float(row.get("ret5_pct"))
    attention_ratio = _to_float(row.get("attention_ratio"))
    value_spike_ratio = _to_float(row.get("value_spike_ratio"))
    daily_rsi = _to_float(row.get("daily_rsi"))
    top_rank_quality_penalty = _to_float(row.get("top_rank_quality_penalty"))
    overextended = bool(row.get("overextended"))
    bearish_exception_ready = bool(row.get("bearish_exception_ready"))

    badges: list[tuple[str, str]] = []
    if symbol and symbol in selected_symbols:
        badges.append(("good", "오늘 선정"))
    elif rank <= max(1, selected_limit):
        badges.append(("hold", "편입 보류"))
    else:
        badges.append(("wait", "순위 대기"))
    if rank == 1:
        badges.append(("good", "실전 우선"))
    elif rank in {2, 3}:
        badges.append(("wait", "보조 후보"))
    if momentum_pct > 0:
        badges.append(("good", "20일 상승"))
    if ret5_pct > 0:
        badges.append(("good", "5일 강세"))
    if attention_ratio >= 1.2:
        badges.append(("good", "관심 증가"))
    if value_spike_ratio >= 1.3:
        badges.append(("good", "거래대금 유입"))
    if top_rank_quality_penalty > 0:
        badges.append(("hold", f"TQP {top_rank_quality_penalty:.2f}"))
    if 55.0 <= daily_rsi <= 78.0:
        badges.append(("good", "RSI 적정"))
    elif daily_rsi > 78.0:
        badges.append(("hold", "과열 주의"))
    if bearish_exception_ready:
        badges.append(("hold", "약세 예외"))
    if overextended:
        badges.append(("hold", "추격 과열"))

    return "".join(
        f"<span class='reason-badge {html.escape(level)}'>{html.escape(label)}</span>"
        for level, label in badges[:5]
    )


def _selection_history_cards_html(rows: object, name_map: dict[str, str]) -> str:
    items = list(rows) if isinstance(rows, list) else []
    cards: list[str] = []
    for row in items[:8]:
        if not isinstance(row, dict):
            continue
        cards.append(
            "<div class='selection-history-item'>"
            f"<div class='selection-history-title'>{html.escape(_symbol_label(str(row.get('symbol') or ''), name_map))}</div>"
            "<div class='selection-history-metrics'>"
            f"<span class='reason-badge good'>누적 {int(_to_float(row.get('selected_count')))}회</span>"
            f"<span class='reason-badge wait'>연속 {int(_to_float(row.get('current_streak_days')))}일</span>"
            "</div>"
            f"<div class='selection-history-date'>최근 {html.escape(_display_text(row.get('last_selected_day'), '-'))}</div>"
            "</div>"
        )
    return f"<div class='selection-history-grid'>{''.join(cards)}</div>" if cards else "<div class='k'>선정 이력 집계 전입니다.</div>"


def _selection_history_stats_from_file(
    path_str: str,
    current_symbols: list[str],
) -> tuple[list[dict[str, object]], float, str]:
    path = Path(path_str)
    if not path.exists():
        return [], 0.0, "선정 이력 파일이 아직 없습니다."
    try:
        raw = json.loads(path.read_text())
    except Exception:
        return [], 0.0, "선정 이력 파일을 읽지 못했습니다."
    days = [row for row in list(raw.get("days") or []) if isinstance(row, dict)]
    days.sort(key=lambda row: str(row.get("day") or ""))
    current_clean = [str(sym).strip() for sym in current_symbols if str(sym).strip()]
    latest_day_symbols = [str(sym).strip() for sym in list(days[-1].get("symbols") or []) if str(sym).strip()] if days else []
    if (not current_clean) or (latest_day_symbols and len(current_clean) < len(latest_day_symbols)):
        current_clean = latest_day_symbols or current_clean
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
            }
        )
    prev_set: set[str] = set()
    current_set = set(current_clean)
    if len(days) >= 2:
        prev_set = {str(x).strip() for x in list(days[-2].get("symbols") or []) if str(x).strip()}
    union = current_set | prev_set
    changed = current_set.symmetric_difference(prev_set)
    turnover_pct = ((len(changed) / len(union)) * 100.0) if union else 0.0
    note = "전일 비교는 다음 거래일부터 가능합니다." if len(days) < 2 else (
        "전일 대비 교체율이 높습니다. 일일 로테이션이 과할 수 있습니다."
        if turnover_pct > 50.0
        else "전일 대비 교체율이 안정적입니다."
    )
    return stats, turnover_pct, note


def _load_opening_review_history_rows(path_str: str = "data/opening_review_history.json") -> list[dict[str, object]]:
    path = Path(path_str)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except Exception:
        return []
    rows = [row for row in list(raw.get("days") or []) if isinstance(row, dict)]
    rows.sort(key=lambda row: str(row.get("day") or ""))
    return rows[-30:]


def _load_selected_intraday_price_summary(path_str: str = "data/selected_intraday_prices.json") -> dict[str, object]:
    path = Path(path_str)
    if not path.exists():
        return {"available": False, "rows": 0, "symbols": 0, "days": 0, "updated_at": "", "last_bar_ts": ""}
    try:
        raw = json.loads(path.read_text())
    except Exception:
        return {"available": False, "rows": 0, "symbols": 0, "days": 0, "updated_at": "", "last_bar_ts": ""}
    rows = [row for row in list(raw.get("rows") or []) if isinstance(row, dict)]
    symbols = sorted({str(row.get("symbol") or "").strip() for row in rows if str(row.get("symbol") or "").strip()})
    bar_ts_values = [str(row.get("bar_ts") or "").strip() for row in rows if str(row.get("bar_ts") or "").strip()]
    day_values = sorted({ts[:10] for ts in bar_ts_values if len(ts) >= 10})
    return {
        "available": bool(rows),
        "rows": len(rows),
        "symbols": len(symbols),
        "days": len(day_values),
        "updated_at": str(raw.get("updated_at") or ""),
        "last_bar_ts": max(bar_ts_values) if bar_ts_values else "",
    }


def _load_selected_intraday_day_options(path_str: str = "data/selected_intraday_prices.json") -> list[str]:
    path = Path(path_str)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except Exception:
        return []
    rows = [row for row in list(raw.get("rows") or []) if isinstance(row, dict)]
    days = sorted({str(row.get("bar_ts") or "").strip()[:10] for row in rows if str(row.get("bar_ts") or "").strip()[:10]})
    return days[-40:]


def _selected_intraday_signal_timeline_svg(
    indexed: list[float | None],
    *,
    labels: list[str],
    decision_markers: list[tuple[int, float, str, str]],
    trade_markers: list[tuple[int, float, str, str]],
    summary: str,
    width: int = 460,
    height: int = 170,
) -> str:
    valid_indexed = [float(v) for v in indexed if v is not None]
    if not valid_indexed:
        return "<div class='k'>차트 데이터가 없습니다.</div>"
    pad = 16.0
    lo = min(valid_indexed)
    hi = max(valid_indexed)
    if hi <= lo:
        hi = lo + 1.0
    span_x = max(1.0, width - (pad * 2.0))
    span_y = max(1.0, height - (pad * 2.0))
    denom = max(1, len(indexed) - 1)

    def _x(i: int) -> float:
        return pad + (span_x * i / denom)

    def _y(v: float) -> float:
        return (height - pad) - ((v - lo) / (hi - lo) * span_y)

    parts: list[str] = [
        f"<svg viewBox='0 0 {width} {height}' preserveAspectRatio='none' style='width:100%;height:180px'>",
        "<rect x='0' y='0' width='100%' height='100%' fill='#091321' rx='10'/>",
    ]
    for idx in range(5):
        gy = pad + (span_y * idx / 4)
        parts.append(
            f"<line x1='{pad}' y1='{gy:.1f}' x2='{width-pad}' y2='{gy:.1f}' stroke='#203149' stroke-width='1'/>"
        )
    if lo < 0 < hi:
        zy = _y(0.0)
        parts.append(
            f"<line x1='{pad}' y1='{zy:.1f}' x2='{width-pad}' y2='{zy:.1f}' stroke='#5a708f' stroke-dasharray='4 4' stroke-width='1'/>"
        )
    line_seg: list[str] = []
    for idx, value in enumerate(indexed):
        if value is None:
            if len(line_seg) >= 2:
                parts.append(f"<polyline fill='none' stroke='#67e8f9' stroke-width='2.4' points='{' '.join(line_seg)}'/>")
            line_seg = []
            continue
        line_seg.append(f"{_x(idx):.1f},{_y(float(value)):.1f}")
    if len(line_seg) >= 2:
        parts.append(f"<polyline fill='none' stroke='#67e8f9' stroke-width='2.4' points='{' '.join(line_seg)}'/>")

    marker_palette = {
        "BUY": "#22c55e",
        "SELL": "#ff7e7e",
        "HOLD": "#9fb6d7",
    }
    for idx, value, signal, label in decision_markers:
        color = marker_palette.get(signal, "#9fb6d7")
        xx = _x(idx)
        yy = _y(value)
        if signal == "BUY":
            points = f"{xx:.1f},{yy-7:.1f} {xx-6:.1f},{yy+6:.1f} {xx+6:.1f},{yy+6:.1f}"
        elif signal == "SELL":
            points = f"{xx:.1f},{yy+7:.1f} {xx-6:.1f},{yy-6:.1f} {xx+6:.1f},{yy-6:.1f}"
        else:
            points = f"{xx-4:.1f},{yy:.1f} {xx:.1f},{yy-4:.1f} {xx+4:.1f},{yy:.1f} {xx:.1f},{yy+4:.1f}"
        parts.append(f"<polygon points='{points}' fill='{color}' opacity='0.95'/>")
        parts.append(
            f"<title>{html.escape(label)}</title>"
        )

    for idx, value, side, label in trade_markers:
        color = "#16a34a" if side == "BUY" else "#dc2626"
        xx = _x(idx)
        yy = _y(value)
        parts.append(f"<circle cx='{xx:.1f}' cy='{yy:.1f}' r='7' fill='#091321' stroke='{color}' stroke-width='2.4'/>")
        parts.append(
            f"<text x='{xx:.1f}' y='{yy+3.5:.1f}' text-anchor='middle' fill='{color}' font-size='8' font-weight='800'>{html.escape(side[:1])}</text>"
        )
        parts.append(f"<title>{html.escape(label)}</title>")

    tick_positions = sorted({0, max(0, len(labels) // 2), max(0, len(labels) - 1)})
    for pos in tick_positions:
        if 0 <= pos < len(labels):
            parts.append(
                f"<text x='{_x(pos):.1f}' y='{height-2:.1f}' text-anchor='middle' fill='#8ea5c5' font-size='10'>{html.escape(labels[pos])}</text>"
            )
    parts.append("</svg>")
    parts.append(
        "<div class='k'>"
        "<span style='display:inline-flex;align-items:center;gap:6px;margin-right:12px'><span style='width:10px;height:10px;background:#67e8f9;border-radius:999px;display:inline-block'></span>가격 흐름</span>"
        "<span style='display:inline-flex;align-items:center;gap:6px;margin-right:12px'><span style='width:0;height:0;border-left:6px solid transparent;border-right:6px solid transparent;border-bottom:10px solid #22c55e;display:inline-block'></span>BUY 신호</span>"
        "<span style='display:inline-flex;align-items:center;gap:6px;margin-right:12px'><span style='width:0;height:0;border-left:6px solid transparent;border-right:6px solid transparent;border-top:10px solid #ff7e7e;display:inline-block'></span>SELL 신호</span>"
        "<span style='display:inline-flex;align-items:center;gap:6px'><span style='width:12px;height:12px;border:2px solid #16a34a;border-radius:999px;display:inline-block'></span>실제 체결</span>"
        "</div>"
    )
    if summary:
        parts.append(f"<div class='k'>{html.escape(summary)}</div>")
    return "".join(parts)


def _build_intraday_signal_payload(
    ordered: list[tuple[str, list[dict[str, object]]]],
    *,
    latest_day: str,
    ledger_report: dict[str, object] | None = None,
    width: int = 460,
    height: int = 170,
) -> dict[str, object]:
    if len(ordered) < 2:
        return {"available": False, "chart": "", "summary": ""}
    avg_prices: list[float | None] = [
        (sum(_to_float(row.get("price")) for row in vals) / float(len(vals))) if vals else None
        for _, vals in ordered
    ]
    base = next((float(v) for v in avg_prices if v is not None and v > 0), 0.0)
    if base <= 0:
        return {"available": False, "chart": "", "summary": ""}
    indexed: list[float | None] = [(((float(price) / base) - 1.0) * 100.0) if price is not None and price > 0 else None for price in avg_prices]
    labels = [ts[11:16] if len(ts) >= 16 else ts for ts, _ in ordered]
    ordered_ts = [ts for ts, _ in ordered]

    def _dominant_signal(rows_at_bar: list[dict[str, object]]) -> str:
        final_actions = [str(row.get("action") or "").strip().upper() for row in rows_at_bar]
        raw_actions = [str(row.get("signal_raw") or "").strip().upper() for row in rows_at_bar]
        if "SELL" in final_actions:
            return "SELL"
        if "BUY" in final_actions:
            return "BUY"
        if "SELL" in raw_actions:
            return "SELL"
        if "BUY" in raw_actions:
            return "BUY"
        return "HOLD"

    signal_counts = {"BUY": 0, "SELL": 0, "HOLD": 0}
    decision_markers: list[tuple[int, float, str, str]] = []
    prev_signal = ""
    for idx, (_ts, rows_at_bar) in enumerate(ordered):
        if not rows_at_bar or indexed[idx] is None:
            prev_signal = ""
            continue
        signal = _dominant_signal(rows_at_bar)
        signal_counts[signal] = int(signal_counts.get(signal, 0)) + 1
        if signal in {"BUY", "SELL"} or (prev_signal and signal != prev_signal and prev_signal in {"BUY", "SELL"}):
            reason = str((rows_at_bar[0] or {}).get("decision_reason") or signal)
            decision_markers.append(
                (
                    idx,
                    float(indexed[idx]),
                    signal,
                    f"{labels[idx]} {signal} signal | {reason[:120]}",
                )
            )
        prev_signal = signal

    ts_to_index = {ts: idx for idx, ts in enumerate(ordered_ts)}
    symbol_set = {
        str(row.get("symbol") or "").strip()
        for _ts, rows_at_bar in ordered
        for row in rows_at_bar
        if str(row.get("symbol") or "").strip()
    }
    trade_rows = list((ledger_report or {}).get("trades") or []) if isinstance((ledger_report or {}).get("trades"), list) else []
    latest_day_trade_rows = [
        row for row in trade_rows
        if isinstance(row, dict)
        and str(row.get("ts") or "").startswith(latest_day)
        and str(row.get("symbol") or "").strip() in symbol_set
        and str(row.get("side") or "").strip().upper() in {"BUY", "SELL"}
    ]

    def _nearest_index_for_trade(ts_text: str) -> int:
        base_text = str(ts_text or "").strip()
        if len(base_text) >= 19 and base_text in ts_to_index:
            return int(ts_to_index[base_text])
        best_idx = 0
        best_gap = None
        try:
            trade_dt = datetime.strptime(base_text[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return 0
        for idx, ts in enumerate(ordered_ts):
            try:
                bar_dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            gap = abs((bar_dt - trade_dt).total_seconds())
            if best_gap is None or gap < best_gap:
                best_gap = gap
                best_idx = idx
        return best_idx

    trade_markers: list[tuple[int, float, str, str]] = []
    for row in latest_day_trade_rows:
        side = str(row.get("side") or "").strip().upper()
        idx = _nearest_index_for_trade(str(row.get("ts") or ""))
        if indexed[idx] is None:
            continue
        trade_markers.append(
            (
                idx,
                float(indexed[idx]),
                side,
                f"{str(row.get('ts') or '')[11:16]} {side} fill {str(row.get('symbol') or '')} @{_to_float(row.get('price')):,.0f}",
            )
        )

    chart = _selected_intraday_signal_timeline_svg(
        indexed,
        labels=labels,
        decision_markers=decision_markers,
        trade_markers=trade_markers,
        summary=(
            f"최근 저장일 {latest_day or '-'} | window {sum(1 for v in indexed if v is not None)} / {len(indexed)} bars | 마지막 {next((float(v) for v in reversed(indexed) if v is not None), 0.0):+.2f}% | "
            f"BUY 신호 {signal_counts.get('BUY', 0)} | SELL 신호 {signal_counts.get('SELL', 0)} | "
            f"체결 {len(latest_day_trade_rows)}건"
        ),
        width=width,
        height=height,
    )
    return {
        "available": True,
        "chart": chart,
        "summary": (
            f"최근 저장일 {latest_day or '-'} | window {sum(1 for v in indexed if v is not None)} / {len(indexed)} bars | 마지막 {next((float(v) for v in reversed(indexed) if v is not None), 0.0):+.2f}% | "
            f"BUY {signal_counts.get('BUY', 0)} / SELL {signal_counts.get('SELL', 0)} / HOLD {signal_counts.get('HOLD', 0)}"
        ),
    }


def _load_selected_intraday_symbol_chart_map(
    path_str: str = "data/selected_intraday_prices.json",
    *,
    ledger_report: dict[str, object] | None = None,
    max_points: int = 48,
    bar_interval_minutes: int = 2,
    market_open_hhmm: str = "09:00",
    market_close_hhmm: str = "15:30",
) -> dict[str, dict[str, object]]:
    path = Path(path_str)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except Exception:
        return {}
    rows = [row for row in list(raw.get("rows") or []) if isinstance(row, dict)]
    if not rows:
        return {}
    rows.sort(key=lambda row: str(row.get("bar_ts") or ""))
    latest_day = ""
    for row in reversed(rows):
        ts = str(row.get("bar_ts") or "").strip()
        if len(ts) >= 10:
            latest_day = ts[:10]
            break
    per_symbol_all: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        sym = str(row.get("symbol") or "").strip()
        ts = str(row.get("bar_ts") or "").strip()
        if not sym or not ts or _to_float(row.get("price")) <= 0:
            continue
        per_symbol_all.setdefault(sym, []).append(row)

    def _parse_hhmm_local(text: str, fallback_h: int, fallback_m: int) -> tuple[int, int]:
        raw = str(text or "").strip()
        if ":" not in raw:
            return fallback_h, fallback_m
        hh_text, mm_text = raw.split(":", 1)
        try:
            hh = max(0, min(23, int(hh_text)))
            mm = max(0, min(59, int(mm_text)))
            return hh, mm
        except Exception:
            return fallback_h, fallback_m

    open_h, open_m = _parse_hhmm_local(market_open_hhmm, 9, 0)
    close_h, close_m = _parse_hhmm_local(market_close_hhmm, 15, 30)

    def _is_market_slot(dt_obj: datetime) -> bool:
        if dt_obj.weekday() >= 5:
            return False
        slot_minutes = (dt_obj.hour * 60) + dt_obj.minute
        open_minutes = (open_h * 60) + open_m
        close_minutes = (close_h * 60) + close_m
        return open_minutes <= slot_minutes <= close_minutes

    def _step_to_prev_market_slot(dt_obj: datetime) -> datetime:
        step = timedelta(minutes=max(1, int(bar_interval_minutes)))
        cur = dt_obj
        while True:
            cur = cur - step
            if _is_market_slot(cur):
                return cur

    latest_dt = None
    for row in reversed(rows):
        try:
            latest_dt = datetime.strptime(str(row.get("bar_ts") or "")[:19], "%Y-%m-%d %H:%M:%S")
            break
        except Exception:
            continue
    if latest_dt is None:
        return {}

    slot_dts = [latest_dt]
    while len(slot_dts) < max(2, int(max_points)):
        slot_dts.append(_step_to_prev_market_slot(slot_dts[-1]))
    slot_dts.reverse()
    fixed_slots = [dt_obj.strftime("%Y-%m-%d %H:%M:%S") for dt_obj in slot_dts]

    out: dict[str, dict[str, object]] = {}
    for sym, sym_rows_all in per_symbol_all.items():
        grouped: dict[str, list[dict[str, object]]] = {}
        for row in sym_rows_all[-max(2, max_points * 4):]:
            grouped.setdefault(str(row.get("bar_ts") or "").strip(), []).append(row)
        ordered = [(slot, list(grouped.get(slot) or [])) for slot in fixed_slots]
        payload = _build_intraday_signal_payload(
            ordered,
            latest_day=(fixed_slots[-1][:10] if fixed_slots else latest_day),
            ledger_report=ledger_report,
            width=260,
            height=104,
        )
        if bool(payload.get("available")):
            out[sym] = payload
    return out


def _load_selected_intraday_replay_chart(
    path_str: str = "data/selected_intraday_prices.json",
    *,
    ledger_report: dict[str, object] | None = None,
) -> dict[str, object]:
    path = Path(path_str)
    if not path.exists():
        return {"available": False, "chart": "", "summary": ""}
    try:
        raw = json.loads(path.read_text())
    except Exception:
        return {"available": False, "chart": "", "summary": ""}
    rows = [row for row in list(raw.get("rows") or []) if isinstance(row, dict)]
    if not rows:
        return {"available": False, "chart": "", "summary": ""}
    rows.sort(key=lambda row: str(row.get("bar_ts") or ""))
    latest_day = ""
    for row in reversed(rows):
        ts = str(row.get("bar_ts") or "").strip()
        if len(ts) >= 10:
            latest_day = ts[:10]
            break
    day_rows = [row for row in rows if str(row.get("bar_ts") or "").startswith(latest_day)] if latest_day else rows[-48:]
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in day_rows:
        ts = str(row.get("bar_ts") or "").strip()
        price = _to_float(row.get("price"))
        if not ts or price <= 0:
            continue
        grouped.setdefault(ts, []).append(row)
    ordered = sorted(grouped.items())
    if len(ordered) < 2:
        return {"available": False, "chart": "", "summary": ""}
    return _build_intraday_signal_payload(
        ordered,
        latest_day=latest_day,
        ledger_report=ledger_report,
    )


def _today_trade_summary_from_ledger(ledger_report: dict[str, object], day_key: str) -> dict[str, object]:
    trades = list(ledger_report.get("trades") or []) if isinstance(ledger_report, dict) else []
    today_rows = [row for row in trades if isinstance(row, dict) and str(row.get("ts") or "").startswith(day_key)]
    buy_count = sum(1 for row in today_rows if str(row.get("side") or "").upper() == "BUY")
    sell_rows = [row for row in today_rows if str(row.get("side") or "").upper() == "SELL"]
    realized_pnl = sum(_to_float(row.get("realized_pnl")) for row in sell_rows)
    win_count = sum(1 for row in sell_rows if _to_float(row.get("realized_pnl")) > 0)
    loss_count = sum(1 for row in sell_rows if _to_float(row.get("realized_pnl")) < 0)
    return {
        "trade_rows": today_rows,
        "trade_count": len(today_rows),
        "buy_count": buy_count,
        "sell_count": len(sell_rows),
        "realized_pnl": realized_pnl,
        "win_count": win_count,
        "loss_count": loss_count,
        "symbols": sorted(
            {
                str(row.get("symbol") or "").strip()
                for row in today_rows
                if str(row.get("symbol") or "").strip()
            }
        ),
    }


def _today_candidate_outcome(
    symbol: str,
    stock_row: dict[str, object] | None,
    trade_rows: list[dict[str, object]],
) -> tuple[str, str]:
    symbol = str(symbol or "").strip()
    rows = [row for row in trade_rows if isinstance(row, dict) and str(row.get("symbol") or "").strip() == symbol]
    buy_rows = [row for row in rows if str(row.get("side") or "").upper() == "BUY"]
    sell_rows = [row for row in rows if str(row.get("side") or "").upper() == "SELL"]
    if sell_rows:
        pnl = sum(_to_float(row.get("realized_pnl")) for row in sell_rows)
        return ("청산", f"청산 {pnl:+,.0f}")
    if stock_row and _to_int(stock_row.get("qty")) > 0:
        ret = _to_float(stock_row.get("return_pct"))
        return ("보유", f"보유 {ret:+.2f}%")
    if buy_rows:
        avg_price = sum(_to_float(row.get("price")) for row in buy_rows) / max(1, len(buy_rows))
        return ("진입", f"진입 {avg_price:,.0f}")
    if stock_row:
        bucket = _stock_board_bucket(stock_row)
        if bucket == "blocked":
            blockers = list(stock_row.get("entry_blockers") or []) if isinstance(stock_row.get("entry_blockers"), list) else []
            reason = _humanize_blocker(blockers[0]) if blockers else _display_text(stock_row.get("decision_reason"), "차단")
            return ("차단", reason)
        if bool(stock_row.get("watchlist")):
            return ("감시", _humanize_watch_reason(stock_row.get("watch_reason")))
        readiness, label, _ = _stock_readiness_meta(stock_row)
        if readiness in {"ready", "watch"}:
            return ("감시", label)
    return ("미체결", "체결 없음")


def _today_candidate_timeline_html(
    symbol: str,
    stock_row: dict[str, object] | None,
    trade_rows: list[dict[str, object]],
) -> str:
    outcome_label, outcome_detail = _today_candidate_outcome(symbol, stock_row, trade_rows)
    steps = [
        ("선정", "done"),
    ]
    if stock_row and bool(stock_row.get("watchlist")):
        steps.append(("감시", "watch"))
    elif stock_row and _stock_board_bucket(stock_row) == "blocked":
        steps.append(("차단", "blocked"))
    elif stock_row:
        readiness, _, _ = _stock_readiness_meta(stock_row)
        steps.append(("대기" if readiness in {"ready", "watch"} else "감시", "watch"))
    else:
        steps.append(("대기", "watch"))

    outcome_tone = {
        "청산": "done",
        "보유": "watch",
        "진입": "done",
        "차단": "blocked",
        "감시": "watch",
        "미체결": "idle",
    }.get(outcome_label, "idle")
    steps.append((outcome_label, outcome_tone))

    chips = []
    tone_style = {
        "done": "background:#163423;border:1px solid #2d7a52;color:#b8ffd5;",
        "watch": "background:#17253b;border:1px solid #335a88;color:#cfe6ff;",
        "blocked": "background:#32181b;border:1px solid #8b4747;color:#ffc9c9;",
        "idle": "background:#1d2330;border:1px solid #44506a;color:#d9e3f7;",
    }
    for label, tone in steps:
        chips.append(
            f"<span style='display:inline-flex;align-items:center;padding:4px 8px;border-radius:999px;font-size:11px;font-weight:700;{tone_style.get(tone, tone_style['idle'])}'>{html.escape(label)}</span>"
        )
    return (
        "<div style='margin-top:8px'>"
        f"<div style='display:flex;gap:6px;flex-wrap:wrap'>{''.join(chips)}</div>"
        f"<div class='k' style='margin-top:6px'>최종 결과: {html.escape(outcome_detail)}</div>"
        "</div>"
    )


def _candidate_key_risk_badge(row: dict[str, object], stock_row: dict[str, object] | None = None) -> str:
    blockers = []
    if stock_row and isinstance(stock_row.get("entry_blockers"), list):
        blockers = [x for x in list(stock_row.get("entry_blockers") or []) if str(x).strip()]
    label = ""
    tone = "watch"
    if blockers:
        key = _humanize_blocker(blockers[0])
        label = f"핵심 차단 {key}"
        tone = "blocked"
    else:
        daily_rsi = _to_float(row.get("daily_rsi") if "daily_rsi" in row else row.get("factor_daily_rsi"))
        attention = _to_float(row.get("attention_ratio") if "attention_ratio" in row else row.get("factor_attention_ratio"))
        spike = _to_float(row.get("value_spike_ratio") if "value_spike_ratio" in row else row.get("factor_value_spike_ratio"))
        trend_ok = bool(row.get("trend_ok") if "trend_ok" in row else row.get("factor_trend_ok"))
        structure_ok = bool(row.get("structure_ok") if "structure_ok" in row else row.get("factor_structure_ok"))
        breakout_ok = bool(row.get("breakout_ok") if "breakout_ok" in row else row.get("factor_breakout_ok"))
        overextended = bool(row.get("overextended") if "overextended" in row else row.get("factor_overextended"))
        if overextended or daily_rsi >= 80.0:
            label = "핵심 리스크 과열 추격"
            tone = "blocked"
        elif not trend_ok or not structure_ok:
            label = "핵심 리스크 추세/구조"
            tone = "blocked"
        elif not breakout_ok:
            label = "핵심 리스크 돌파 미확인"
            tone = "watch"
        elif attention < 1.0 or spike < 1.0:
            label = "핵심 리스크 수급 약화"
            tone = "watch"
    if not label:
        return ""
    style = {
        "blocked": "background:#32181b;border:1px solid #8b4747;color:#ffc9c9;",
        "watch": "background:#17253b;border:1px solid #335a88;color:#cfe6ff;",
    }.get(tone, "background:#1d2330;border:1px solid #44506a;color:#d9e3f7;")
    return (
        f"<div style='margin-top:8px'>"
        f"<span style='display:inline-flex;align-items:center;padding:4px 8px;border-radius:999px;font-size:11px;font-weight:700;{style}'>{html.escape(label)}</span>"
        f"</div>"
    )


def _block_reason_meta(reason: str) -> tuple[str, str, str]:
    text = (reason or "").strip()
    lowered = text.lower()
    if any(token in lowered for token in ("overheat", "과열")):
        return ("hold", "과열 제한", "단기 급등 또는 과열 신호로 신규 진입을 제한합니다.")
    if any(token in lowered for token in ("overextended", "extension", "추격")):
        return ("hold", "추격 과열", "이미 많이 달아난 구간이라 늦은 추격 진입을 제한합니다.")
    if any(token in lowered for token in ("bearish_regime", "bearish", "약세")):
        return ("hold", "약세장 제한", "약세장에서는 예외 조건을 통과한 종목만 진입합니다.")
    if any(token in lowered for token in ("trend", "structure", "추세", "구조")):
        return ("wait", "추세 미충족", "상승 추세 또는 구조 조건이 아직 부족합니다.")
    if any(token in lowered for token in ("score", "momentum", "rsi", "점수", "모멘텀")):
        return ("wait", "강도 부족", "점수, 모멘텀, RSI 같은 진입 강도가 기준에 미달합니다.")
    if any(token in lowered for token in ("cooldown", "stale", "session", "쿨다운", "지연", "세션")):
        return ("hold", "운영 제한", "쿨다운, 데이터 지연, 세션 조건으로 일시 제한된 상태입니다.")
    if any(token in lowered for token in ("risk", "heat", "loss", "리스크", "손실", "portfolio")):
        return ("hold", "리스크 제어", "포트폴리오 리스크 또는 손실 제한으로 진입을 막고 있습니다.")
    return ("wait", "기타 필터", "보조 필터 또는 운영 조건이 아직 충족되지 않았습니다.")


def _block_reason_cards_html(reason_histogram: object) -> str:
    if not isinstance(reason_histogram, dict):
        return ""
    cards: list[str] = []
    items = list(reason_histogram.items())[:6]
    total = sum(max(0, _to_int(v)) for _, v in items)
    for reason, count_raw in items:
        count = max(0, _to_int(count_raw))
        tone, label, desc = _block_reason_meta(str(reason))
        share = (count / total * 100.0) if total > 0 else 0.0
        cards.append(
            "<div class='block-card'>"
            f"<div class='block-card-top'><span class='reason-badge {html.escape(tone)}'>{html.escape(label)}</span>"
            f"<strong>{count}회</strong></div>"
            f"<div class='block-reason-name'>{html.escape(str(reason))}</div>"
            f"<div class='block-reason-desc'>{html.escape(desc)}</div>"
            f"<div class='k' style='margin-top:6px'>비중 {share:.1f}%</div>"
            "</div>"
        )
    return "".join(cards)


def _fallback_stock_rows_from_snapshot(
    factor_snapshot: object,
    *,
    selected_symbols: set[str] | None = None,
    source_label: str = "마지막 유효값",
    updated_at: str = "",
) -> list[dict[str, object]]:
    rows = list(factor_snapshot) if isinstance(factor_snapshot, list) else []
    selected = selected_symbols or set()
    out: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "symbol": row.get("symbol"),
                "price": _latest_known_price(row.get("symbol")),
                "action": "HOLD",
                "selected": str(row.get("symbol") or "") in selected,
                "score": row.get("score", 0.0),
                "qty": 0,
                "avg_price": 0.0,
                "return_pct": 0.0,
                "volatility_pct": row.get("volatility_pct", 0.0),
                "atr_proxy_pct": row.get("atr14_pct", 0.0),
                "factor_momentum_pct": row.get("momentum_pct", 0.0),
                "factor_relative_pct": row.get("relative_pct", 0.0),
                "factor_trend_pct": row.get("trend_pct", 0.0),
                "factor_vol_penalty_pct": row.get("volatility_pct", 0.0),
                "factor_ret5_pct": row.get("ret5_pct", 0.0),
                "factor_atr14_pct": row.get("atr14_pct", 0.0),
                "factor_daily_rsi": row.get("daily_rsi", 50.0),
                "factor_attention_ratio": row.get("attention_ratio", 0.0),
                "factor_value_spike_ratio": row.get("value_spike_ratio", 0.0),
                "factor_near_high_pct": row.get("near_high_pct", 0.0),
                "factor_trend_ok": row.get("trend_ok", False),
                "factor_structure_ok": row.get("structure_ok", False),
                "factor_breakout_ok": row.get("breakout_ok", False),
                "factor_overheat": row.get("overheat", False),
                "factor_overextended": row.get("overextended", False),
                "factor_overextension_penalty": row.get("overextension_penalty", 0.0),
                "factor_bearish_exception_ready": False,
                "sector": row.get("sector", "UNMAPPED"),
                "signal_raw": "SNAPSHOT",
                "confirm_needed": 1,
                "confirm_progress": 0,
                "data_age_sec": 0,
                "cooldown_left_sec": 0,
                "snapshot_source_label": source_label,
                "snapshot_updated_at": updated_at,
                "decision_reason": "장외 시간 스냅샷 기준 진단 카드",
            }
        )
    return out


class OffHoursSnapshotService:
    def __init__(self, refresh_sec: int = 300) -> None:
        self.refresh_sec = max(60, int(refresh_sec))
        self._lock = threading.Lock()
        self._last_fetch = 0.0
        self._last: dict[str, object] = {"updated_at": None, "rows": []}

    def get(self, *, force: bool = False) -> dict[str, object]:
        with self._lock:
            if force or (time.time() - self._last_fetch) >= self.refresh_sec:
                self._last = self._fetch()
                self._last_fetch = time.time()
            return dict(self._last)

    def _fetch(self) -> dict[str, object]:
        settings = load_settings()
        seed_symbols = [
            x for x in selection_universe_symbols(settings) if x.strip().isdigit()
        ]
        cached_symbols: list[str] = []
        for path in Path("data/backtest_cache").glob("kr_*_daily.json"):
            name = path.name
            if not name.startswith("kr_") or not name.endswith("_daily.json"):
                continue
            code = name[len("kr_") : -len("_daily.json")].strip().upper()
            if code.isdigit():
                cached_symbols.append(code)
        symbols = list(dict.fromkeys(seed_symbols + cached_symbols))
        symbols = symbols[: max(80, int(settings.candidate_refresh_top_n) * 10)]
        ready = _prepare_market_data(market="KR", symbols=symbols, fetch_limit=180)
        ranked: list[tuple[float, str, dict[str, object]]] = []
        for sym, bars in ready:
            try:
                score, factors = _multi_factor_rank_score(bars, market_index_pct=0.0, settings=settings)
            except Exception:
                continue
            if not factors:
                try:
                    factors = _trend_strategy_metrics(bars, market_index_pct=0.0)
                    score = float(factors.get("score", -999.0)) if factors else -999.0
                except Exception:
                    factors = {}
                    score = -999.0
            if not factors:
                try:
                    factors = _trend_strategy_metrics(bars, market_index_pct=0.0)
                    score = float(factors.get("score", -999.0)) if factors else -999.0
                except Exception:
                    factors = {}
                    score = -999.0
            if not factors:
                continue
            row = {
                "symbol": sym,
                "score": round(float(score), 3),
                "momentum_pct": round(float(factors.get("momentum_pct", 0.0)), 2),
                "relative_pct": round(float(factors.get("relative_pct", 0.0)), 2),
                "trend_pct": round(float(factors.get("trend_pct", 0.0)), 2),
                "volatility_pct": round(float(factors.get("volatility_pct", 0.0)), 2),
                "ret5_pct": round(float(factors.get("ret5_pct", 0.0)), 2),
                "atr14_pct": round(float(factors.get("atr14_pct", 0.0)), 2),
                "daily_rsi": round(float(factors.get("daily_rsi", 50.0)), 1),
                "attention_ratio": round(float(factors.get("attention_ratio", 0.0)), 2),
                "value_spike_ratio": round(float(factors.get("value_spike_ratio", 0.0)), 2),
                "near_high_pct": round(float(factors.get("near_high_pct", 0.0)), 2),
                "trend_ok": bool(factors.get("trend_ok", 0.0)),
                "structure_ok": bool(factors.get("structure_ok", 0.0)),
                "breakout_ok": bool(factors.get("breakout_ok", 0.0)),
                "overheat": bool(factors.get("overheat", 0.0)),
                "overextended": bool(factors.get("overextended", 0.0)),
                "overextension_penalty": round(float(factors.get("overextension_penalty", 0.0)), 3),
            }
            ranked.append((float(score), sym, row))
        ranked.sort(reverse=True)
        top_rows = [row for _, _, row in ranked[: max(1, int(settings.trend_select_count))]]
        return {
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "rows": top_rows,
        }


def _event_summary_cards_html(events: list[str]) -> str:
    if not events:
        return ""
    categories = [
        ("ORDER", "주문 이벤트", ("ORDER", "FILL", "CANCEL", "REJECT")),
        ("RISK", "리스크 이벤트", ("RISK", "HALT", "STOP")),
        ("REGIME", "레짐 변화", ("REGIME", "ROTATE", "SELECT")),
        ("ERROR", "오류/예외", ("ERROR", "FAIL", "EXCEPTION")),
    ]
    cards: list[str] = []
    recent = events[-50:]
    for key, label, tokens in categories:
        matched = [evt for evt in recent if any(token in evt.upper() for token in tokens)]
        latest = matched[-1] if matched else "최근 이벤트 없음"
        cards.append(
            "<div class='ops-card'>"
            f"<div class='section-title'>{html.escape(label)}</div>"
            f"<div class='v'>{len(matched)}</div>"
            f"<div class='k' style='margin-top:6px'>{html.escape(_display_text(latest, '최근 이벤트 없음'))}</div>"
            "</div>"
        )
    return "".join(cards)


def _order_summary_cards_html(reconcile: object, orders: object) -> str:
    rec = reconcile if isinstance(reconcile, dict) else {}
    order_list = list(orders) if isinstance(orders, list) else []
    latest_order = order_list[-1] if order_list else {}
    latest_symbol = _symbol_label(str(latest_order.get("symbol") or ""), _symbol_name_map()) if latest_order else "-"
    latest_status = str(latest_order.get("status") or "주문 없음") if latest_order else "주문 없음"
    cards = [
        ("대기 주문", f"{int(_to_float(rec.get('pending')))}", "미체결 또는 확인 대기"),
        ("정합 완료", f"{int(_to_float(rec.get('reconciled_this_loop')))}", "최근 루프 반영"),
        ("시간초과", f"{int(_to_float(rec.get('timeout_this_loop')))}", "응답 지연/누락"),
        ("최근 주문", latest_status, latest_symbol),
    ]
    return "".join(
        "<div class='ops-card'>"
        f"<div class='section-title'>{html.escape(title)}</div>"
        f"<div class='v'>{html.escape(value)}</div>"
        f"<div class='k' style='margin-top:6px'>{html.escape(detail)}</div>"
        "</div>"
        for title, value, detail in cards
    )


def _sparkline_svg(
    values: list[float],
    *,
    color: str,
    unit: str,
    width: int = 460,
    height: int = 150,
) -> str:
    if not values:
        return "<div class='k'>차트 데이터가 없습니다.</div>"

    pad = 16.0
    ymin = min(values)
    ymax = max(values)
    real_min = ymin
    real_max = ymax
    if ymax == ymin:
        ymax = ymin + 1.0

    span_x = max(1.0, width - (pad * 2.0))
    span_y = max(1.0, height - (pad * 2.0))
    denom = max(1, len(values) - 1)

    points: list[str] = []
    for i, v in enumerate(values):
        x = pad + (span_x * i / denom)
        y = (height - pad) - ((v - ymin) / (ymax - ymin) * span_y)
        points.append(f"{x:.1f},{y:.1f}")

    zero_line = ""
    if ymin < 0 < ymax:
        zy = (height - pad) - ((0 - ymin) / (ymax - ymin) * span_y)
        zero_line = (
            f"<line x1='{pad}' y1='{zy:.1f}' x2='{width-pad}' y2='{zy:.1f}' "
            "stroke='#5a708f' stroke-dasharray='4 4' stroke-width='1'/>"
        )

    latest = values[-1]
    summary = f"최신 {latest:+.2f}{unit} | 최소 {real_min:+.2f}{unit} | 최대 {real_max:+.2f}{unit}"
    grid_lines = "".join(
        f"<line x1='{pad}' y1='{pad + (span_y * idx / 4):.1f}' x2='{width-pad}' y2='{pad + (span_y * idx / 4):.1f}' stroke='#203149' stroke-width='1'/>"
        for idx in range(5)
    )
    gradient_id = f"sparkFill{abs(hash((color, unit, len(values))))}"
    return (
        f"<svg viewBox='0 0 {width} {height}' preserveAspectRatio='none' style='width:100%;height:160px'>"
        "<defs>"
        f"<linearGradient id='{gradient_id}' x1='0' x2='0' y1='0' y2='1'>"
        f"<stop offset='0%' stop-color='{color}' stop-opacity='0.28'/>"
        f"<stop offset='100%' stop-color='{color}' stop-opacity='0.03'/>"
        "</linearGradient>"
        "</defs>"
        "<rect x='0' y='0' width='100%' height='100%' fill='#091321' rx='10'/>"
        f"{grid_lines}"
        f"{zero_line}"
        f"<polygon fill='url(#{gradient_id})' points='{' '.join(points + [f'{width-pad:.1f},{height-pad:.1f}', f'{pad:.1f},{height-pad:.1f}'])}'/>"
        f"<polyline fill='none' stroke='{color}' stroke-width='2.5' points='{' '.join(points)}'/>"
        "</svg>"
        f"<div class='k'>{summary}</div>"
    )


def _fetch_yahoo_index_daily_series(symbol: str, *, months: int = 6) -> list[dict[str, float | str]]:
    range_token = f"{max(1, int(months))}mo"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range_token}&interval=1d"
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []
    result = ((data.get("chart") or {}).get("result") or [])
    if not result:
        return []
    result0 = result[0] or {}
    timestamps = list(result0.get("timestamp") or [])
    q = (((result0.get("indicators") or {}).get("quote") or [{}])[0] or {})
    close_raw = list(q.get("close") or [])
    n = min(len(timestamps), len(close_raw))
    out: list[dict[str, float | str]] = []
    for idx in range(n):
        raw_close = close_raw[idx]
        if raw_close is None:
            continue
        try:
            close = float(raw_close)
            ts = int(float(timestamps[idx]))
        except Exception:
            continue
        if close <= 0:
            continue
        out.append(
            {
                "date": time.strftime("%Y-%m-%d", time.gmtime(ts)),
                "close": close,
            }
        )
    return out


def _sma(values: list[float], period: int) -> list[float | None]:
    if period <= 1:
        return [float(v) for v in values]
    out: list[float | None] = []
    window_sum = 0.0
    for i, v in enumerate(values):
        window_sum += v
        if i >= period:
            window_sum -= values[i - period]
        if i + 1 < period:
            out.append(None)
        else:
            out.append(window_sum / period)
    return out


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out: list[float] = []
    prev = values[0]
    for v in values:
        prev = (alpha * v) + ((1.0 - alpha) * prev)
        out.append(prev)
    return out


def _rsi(values: list[float], period: int = 14) -> list[float | None]:
    if len(values) < 2:
        return [None for _ in values]
    gains: list[float] = [0.0]
    losses: list[float] = [0.0]
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(0.0, d))
        losses.append(max(0.0, -d))
    avg_gain = _sma(gains, period)
    avg_loss = _sma(losses, period)
    out: list[float | None] = []
    for g, l in zip(avg_gain, avg_loss):
        if g is None or l is None:
            out.append(None)
            continue
        if l == 0:
            out.append(100.0)
            continue
        rs = g / l
        out.append(100.0 - (100.0 / (1.0 + rs)))
    return out


def _macd(values: list[float]) -> tuple[list[float], list[float], list[float]]:
    if not values:
        return [], [], []
    ema12 = _ema(values, 12)
    ema26 = _ema(values, 26)
    macd_line = [a - b for a, b in zip(ema12, ema26)]
    signal = _ema(macd_line, 9)
    hist = [m - s for m, s in zip(macd_line, signal)]
    return macd_line, signal, hist


def _line_overlay_svg(
    series: list[tuple[str, str, list[float | None]]],
    *,
    width: int = 460,
    height: int = 160,
    y_min: float | None = None,
    y_max: float | None = None,
    bands: list[tuple[float, str]] | None = None,
    zone_spans: list[tuple[int, int, str, float]] | None = None,
    x_tick_labels: list[tuple[int, str]] | None = None,
    summary: str = "",
) -> str:
    valid_values = [
        float(v)
        for _, _, vals in series
        for v in vals
        if v is not None
    ]
    if not valid_values:
        return "<div class='k'>차트 데이터가 없습니다.</div>"
    pad = 16.0
    lo = min(valid_values) if y_min is None else y_min
    hi = max(valid_values) if y_max is None else y_max
    if hi <= lo:
        hi = lo + 1.0
    span_x = max(1.0, width - (pad * 2.0))
    span_y = max(1.0, height - (pad * 2.0))
    n = max(len(vals) for _, _, vals in series)
    denom = max(1, n - 1)

    def _x(i: int) -> float:
        return pad + (span_x * i / denom)

    def _y(v: float) -> float:
        return (height - pad) - ((v - lo) / (hi - lo) * span_y)

    parts: list[str] = [
        f"<svg viewBox='0 0 {width} {height}' preserveAspectRatio='none' style='width:100%;height:170px'>",
        "<rect x='0' y='0' width='100%' height='100%' fill='#091321' rx='10'/>",
    ]
    if zone_spans:
        for start_idx, end_idx, color, opacity in zone_spans:
            x1 = _x(max(0, start_idx))
            x2 = _x(min(denom, max(start_idx, end_idx)))
            rect_x = min(x1, x2)
            rect_w = max(2.0, abs(x2 - x1))
            parts.append(
                f"<rect x='{rect_x:.1f}' y='{pad:.1f}' width='{rect_w:.1f}' height='{span_y:.1f}' "
                f"fill='{color}' opacity='{max(0.03, min(0.2, opacity)):.2f}' rx='6'/>"
            )
    for idx in range(5):
        gy = pad + (span_y * idx / 4)
        parts.append(
            f"<line x1='{pad}' y1='{gy:.1f}' x2='{width-pad}' y2='{gy:.1f}' stroke='#203149' stroke-width='1'/>"
        )
    if bands:
        for val, color in bands:
            yy = _y(val)
            parts.append(
                f"<line x1='{pad}' y1='{yy:.1f}' x2='{width-pad}' y2='{yy:.1f}' stroke='{color}' stroke-dasharray='4 4' stroke-width='1'/>"
            )
    if lo < 0 < hi:
        zy = _y(0.0)
        parts.append(
            f"<line x1='{pad}' y1='{zy:.1f}' x2='{width-pad}' y2='{zy:.1f}' stroke='#5a708f' stroke-dasharray='4 4' stroke-width='1'/>"
        )

    for _, color, vals in series:
        seg: list[str] = []
        for i, v in enumerate(vals):
            if v is None:
                if seg:
                    parts.append(
                        f"<polyline fill='none' stroke='{color}' stroke-width='2.2' points='{' '.join(seg)}'/>"
                    )
                    seg = []
                continue
            seg.append(f"{_x(i):.1f},{_y(float(v)):.1f}")
        if seg:
            parts.append(
                f"<polyline fill='none' stroke='{color}' stroke-width='2.2' points='{' '.join(seg)}'/>"
            )

    if x_tick_labels:
        for tick_idx, label in x_tick_labels:
            xx = _x(max(0, min(denom, tick_idx)))
            parts.append(
                f"<text x='{xx:.1f}' y='{height-2:.1f}' text-anchor='middle' fill='#8ea5c5' font-size='10'>{html.escape(label)}</text>"
            )

    parts.append("</svg>")
    legend = " ".join(
        f"<span style='display:inline-flex;align-items:center;gap:6px;margin-right:12px'>"
        f"<span style='width:10px;height:10px;border-radius:999px;background:{html.escape(color)};display:inline-block'></span>"
        f"{html.escape(label)}</span>"
        for label, color, _ in series
    )
    parts.append(f"<div class='k'>{legend}</div>")
    if summary:
        parts.append(f"<div class='k'>{html.escape(summary)}</div>")
    return "".join(parts)


def _drawdown_series(values: list[float]) -> list[float]:
    if not values:
        return []
    peak = values[0]
    out: list[float] = []
    for value in values:
        peak = max(peak, value)
        out.append(((value - peak) / peak * 100.0) if peak > 0 else 0.0)
    return out


def _cumulative_series(values: list[float]) -> list[float]:
    total = 0.0
    out: list[float] = []
    for value in values:
        total += value
        out.append(total)
    return out


def _bar_series_svg(
    values: list[float],
    *,
    positive_color: str = "#1fd38a",
    negative_color: str = "#ff7e7e",
    width: int = 460,
    height: int = 170,
    unit: str = "",
    summary: str = "",
) -> str:
    if not values:
        return "<div class='k'>차트 데이터가 없습니다.</div>"
    pad = 16.0
    lo = min(min(values), 0.0)
    hi = max(max(values), 0.0)
    if hi <= lo:
        hi = lo + 1.0
    span_x = max(1.0, width - (pad * 2.0))
    span_y = max(1.0, height - (pad * 2.0))
    zero_y = (height - pad) - ((0.0 - lo) / (hi - lo) * span_y)
    grid_lines = "".join(
        f"<line x1='{pad}' y1='{pad + (span_y * idx / 4):.1f}' x2='{width-pad}' y2='{pad + (span_y * idx / 4):.1f}' stroke='#203149' stroke-width='1'/>"
        for idx in range(5)
    )
    slot_width = span_x / max(1, len(values))
    bar_width = max(3.0, slot_width - 4.0)
    bars: list[str] = []
    for idx, value in enumerate(values):
        x = pad + (slot_width * idx) + max(1.0, (slot_width - bar_width) / 2.0)
        y = (height - pad) - ((value - lo) / (hi - lo) * span_y)
        top = min(y, zero_y)
        bar_h = max(1.5, abs(zero_y - y))
        color = positive_color if value >= 0 else negative_color
        bars.append(
            f"<rect x='{x:.1f}' y='{top:.1f}' width='{bar_width:.1f}' height='{bar_h:.1f}' rx='2' fill='{color}' opacity='0.92'/>"
        )
    latest = values[-1]
    default_summary = f"최신 {latest:+.2f}{unit} | 최대 {max(values):+.2f}{unit} | 최소 {min(values):+.2f}{unit}"
    return (
        f"<svg viewBox='0 0 {width} {height}' preserveAspectRatio='none' style='width:100%;height:170px'>"
        "<rect x='0' y='0' width='100%' height='100%' fill='#091321' rx='10'/>"
        f"{grid_lines}"
        f"<line x1='{pad}' y1='{zero_y:.1f}' x2='{width-pad}' y2='{zero_y:.1f}' stroke='#5a708f' stroke-dasharray='4 4' stroke-width='1'/>"
        f"{''.join(bars)}"
        "</svg>"
        f"<div class='k'>{html.escape(summary or default_summary)}</div>"
    )


def _category_bar_svg(
    items: list[tuple[str, float]],
    *,
    width: int = 460,
    height: int = 210,
    positive_color: str = "#1fd38a",
    negative_color: str = "#ff7e7e",
    unit: str = "",
    summary: str = "",
) -> str:
    if not items:
        return "<div class='k'>차트 데이터가 없습니다.</div>"
    pad_top = 16.0
    pad_left = 110.0
    pad_right = 18.0
    row_h = max(18.0, (height - (pad_top * 2.0)) / max(1, len(items)))
    usable_w = max(80.0, width - pad_left - pad_right)
    max_abs = max(abs(value) for _, value in items) or 1.0
    zero_x = pad_left + (usable_w * 0.5)
    rows: list[str] = [
        f"<svg viewBox='0 0 {width} {height}' preserveAspectRatio='none' style='width:100%;height:210px'>",
        "<rect x='0' y='0' width='100%' height='100%' fill='#091321' rx='10'/>",
        f"<line x1='{zero_x:.1f}' y1='{pad_top:.1f}' x2='{zero_x:.1f}' y2='{height-pad_top:.1f}' stroke='#5a708f' stroke-dasharray='4 4' stroke-width='1'/>",
    ]
    for idx, (label, value) in enumerate(items):
        cy = pad_top + (row_h * idx) + (row_h * 0.5)
        bar_half = (abs(value) / max_abs) * (usable_w * 0.48)
        color = positive_color if value >= 0 else negative_color
        x = zero_x if value >= 0 else zero_x - bar_half
        rows.append(
            f"<text x='{pad_left-8:.1f}' y='{cy+4:.1f}' text-anchor='end' fill='#c8d8ef' font-size='11'>{html.escape(label)}</text>"
        )
        rows.append(
            f"<rect x='{x:.1f}' y='{cy-(row_h*0.28):.1f}' width='{max(2.0, bar_half):.1f}' height='{max(8.0, row_h*0.56):.1f}' rx='3' fill='{color}' opacity='0.92'/>"
        )
        rows.append(
            f"<text x='{(x + bar_half + 6.0) if value >= 0 else (x - 6.0):.1f}' y='{cy+4:.1f}' text-anchor='{'start' if value >= 0 else 'end'}' fill='#e8f1ff' font-size='11'>{value:+.2f}{html.escape(unit)}</text>"
        )
    rows.append("</svg>")
    if summary:
        rows.append(f"<div class='k'>{html.escape(summary)}</div>")
    return "".join(rows)


def _latest_event(events: list[str], keyword: str) -> str:
    for line in reversed(events):
        if keyword in line:
            return line
    return ""


def _parse_select_event(line: str) -> dict[str, object]:
    # New format: SELECT symbols=A,B,C regime=NEUTRAL conf=0.94 ref=...
    m_new = re.search(
        r"SELECT symbols=(\S+) regime=(\S+)(?: conf=([\-0-9.]+))?(?: idx=([\-+0-9.]+)%?)? ref=(.+)$",
        line,
    )
    if m_new:
        symbols_raw = m_new.group(1).strip()
        symbols = [x.strip() for x in symbols_raw.split(",") if x.strip()]
        return {
            "symbols": symbols,
            "primary_symbol": symbols[0] if symbols else "",
            "regime": m_new.group(2),
            "confidence": _to_float(m_new.group(3)),
            "market_index_pct": _to_float(m_new.group(4)),
            "reference": m_new.group(5).strip(),
        }

    # Legacy format: SELECT symbol=A regime=NEUTRAL score=1.23 ref=...
    m_old = re.search(r"SELECT symbol=(\S+) regime=(\S+) score=([\-0-9.]+) ref=(.+)$", line)
    if m_old:
        return {
            "symbols": [m_old.group(1)],
            "primary_symbol": m_old.group(1),
            "regime": m_old.group(2),
            "score": _to_float(m_old.group(3)),
            "reference": m_old.group(4).strip(),
        }
    return {}


def _symbol_name_map() -> dict[str, str]:
    cached = getattr(_symbol_name_map, "_cache", None)
    if isinstance(cached, dict) and cached:
        return cached
    # Common KRX symbols used for labels before automatic universe data is loaded.
    defaults = {
        "005930": "삼성전자",
        "000660": "SK하이닉스",
        "035420": "NAVER",
        "005380": "현대차",
        "055550": "신한지주",
        "105560": "KB금융",
        "316140": "우리금융지주",
        "068270": "셀트리온",
        "035720": "카카오",
        "051910": "LG화학",
        "207940": "삼성바이오로직스",
    }
    cache_path = _symbol_name_cache_path()
    merged = dict(defaults)
    merged.update(_load_symbol_name_cache(cache_path))
    raw = os.getenv("SYMBOL_NAME_MAP", "").strip()
    for token in raw.split(","):
        item = token.strip()
        if not item or ":" not in item:
            continue
        code, name = item.split(":", 1)
        c = code.strip()
        n = name.strip()
        if c and n:
            merged[c] = n
    if len(merged) < 100:
        fetched = _fetch_kind_symbol_name_map()
        if fetched:
            merged.update(fetched)
            _save_symbol_name_cache(cache_path, merged)
    setattr(_symbol_name_map, "_cache", merged)
    return merged


def _symbol_label(symbol: str, name_map: dict[str, str]) -> str:
    code = str(symbol or "").strip()
    if not code:
        return "-"
    name = name_map.get(code)
    if not name:
        return code
    return f"{name}({code})"


def _format_symbol_list(raw: str, name_map: dict[str, str]) -> str:
    items = [x.strip() for x in str(raw or "").split(",") if x.strip()]
    if not items:
        return "-"
    return ", ".join(_symbol_label(x, name_map) for x in items)


def _format_positions_summary(raw: str, name_map: dict[str, str]) -> str:
    items = [x.strip() for x in str(raw or "").split(",") if x.strip()]
    if not items:
        return "-"
    out: list[str] = []
    for item in items:
        if ":" not in item:
            out.append(item)
            continue
        code, rest = item.split(":", 1)
        out.append(f"{_symbol_label(code.strip(), name_map)}:{rest.strip()}")
    return ", ".join(out)


def _regime_reason(index_change_pct: float, breadth_pct: float) -> tuple[str, str]:
    if index_change_pct >= 0.7 and breadth_pct >= 55.0:
        return "BULLISH", "지수 강세 + 시장 확산 강세"
    if index_change_pct <= -0.7 and breadth_pct <= 45.0:
        return "BEARISH", "지수 약세 + 시장 확산 약세"
    return "NEUTRAL", "강/약 조건 미충족"


def _service_cached(service: object) -> dict[str, object]:
    # Return immediately from cache to keep page rendering responsive.
    with service._lock:
        return service._with_countdown(dict(service._cache))


def _service_refresh_async(service: object) -> None:
    # Refresh in background so UI requests do not block on external API latency.
    with service._lock:
        inflight = bool(getattr(service, "_async_inflight", False))
        stale = (time.time() - float(getattr(service, "_last_fetch", 0.0))) >= int(
            getattr(service, "refresh_sec", 60)
        )
        has_data = bool(service._cache.get("updated_at"))
        if inflight or (not stale and has_data):
            return
        setattr(service, "_async_inflight", True)

    def _run() -> None:
        try:
            service.get(force=True)
        except Exception:
            pass
        finally:
            with service._lock:
                setattr(service, "_async_inflight", False)

    threading.Thread(target=_run, daemon=True, name=f"refresh-{service.__class__.__name__}").start()


class BotController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self.state = BotState()
        self._phase = "idle"  # idle | starting | running | stopping
        self._status_message = "Bot is idle."
        self._last_start_ts = 0.0
        self._start_cooldown_sec = 2.0

    def start(self) -> tuple[bool, str]:
        with self._lock:
            now = time.time()
            if self._phase in {"starting", "running", "stopping"}:
                return False, f"Bot is {self._phase}."
            if (now - self._last_start_ts) < self._start_cooldown_sec:
                return False, "Start cooldown in progress."

            self._stop_event = threading.Event()
            self._phase = "starting"
            self._status_message = "Start requested."
            self._last_start_ts = now

            def _target() -> None:
                try:
                    with self._lock:
                        self._phase = "running"
                        self._status_message = "Bot is running."
                    run_bot(self._stop_event, self.state)
                    with self._lock:
                        self._phase = "idle"
                        self._status_message = "Bot stopped."
                except Exception as exc:
                    self.state.running = False
                    self.state.last_error = str(exc)
                    ts = time.strftime("%Y-%m-%d %H:%M:%S")
                    self.state.events.append(f"{ts} Startup error: {exc}")
                    with self._lock:
                        self._phase = "idle"
                        self._status_message = f"Startup error: {exc}"

            self._thread = threading.Thread(target=_target, daemon=True, name="bot-runner")
            self._thread.start()
            return True, "Start requested."

    def stop(self) -> tuple[bool, str]:
        thread_ref: threading.Thread | None = None
        with self._lock:
            if self._phase in {"idle"} or not self._thread or not self._thread.is_alive():
                return False, "Bot is not running."
            if self._phase == "stopping":
                return False, "Stop already requested."
            self._phase = "stopping"
            self._status_message = "Stop requested."
            self._stop_event.set()
            thread_ref = self._thread

        if thread_ref:
            thread_ref.join(timeout=3.0)

        with self._lock:
            if not self._thread or not self._thread.is_alive():
                self._phase = "idle"
                self._status_message = "Bot stopped."
                self.state.running = False
        return True, "Stop requested."

    def status(self) -> dict[str, object]:
        with self._lock:
            alive = bool(self._thread and self._thread.is_alive())
            data = asdict(self.state)
            data["thread_alive"] = alive
            data["controller_phase"] = self._phase
            data["controller_message"] = self._status_message
            data["uptime_sec"] = (
                round(time.time() - self.state.started_at, 1) if self.state.started_at else 0.0
            )
            data["events"] = list(self.state.events)
            return data


class MarketVibeService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.refresh_sec = int(os.getenv("MARKET_VIBE_REFRESH_SEC", "600"))
        self.index_code = os.getenv("MARKET_INDEX_CODE", "001").strip() or "001"
        self.api_id = os.getenv("MARKET_VIBE_API_ID", "ka20003").strip() or "ka20003"
        self.history_points = int(os.getenv("MARKET_VIBE_HISTORY_POINTS", "2000"))
        self.history_path = Path(os.getenv("MARKET_VIBE_HISTORY_PATH", "data/market_history.json"))
        self.backfill_days = int(os.getenv("MARKET_VIBE_BACKFILL_DAYS", "45"))
        loaded_history = self._backfill_history(self._load_history())
        self._history: deque[dict[str, object]] = deque(loaded_history, maxlen=self.history_points)
        self._cache: dict[str, object] = {
            "updated_at": loaded_history[-1]["updated_at"] if loaded_history else None,
            "index_code": self.index_code,
            "index_name": None,
            "index_value": None,
            "index_change": None,
            "index_change_pct": None,
            "rising": None,
            "falling": None,
            "unchanged": None,
            "upper_limit": None,
            "lower_limit": None,
            "breadth_ratio": None,
            "vibe": "UNKNOWN",
            "top_sectors_up": [],
            "top_sectors_down": [],
            "analysis": {},
            "history": loaded_history,
            "source": {},
            "raw_snapshot": {},
            "stats": {},
            "last_error": None,
            "refresh_sec": self.refresh_sec,
        }
        self._last_fetch = 0.0
        self._prev_snapshot: dict[str, float] | None = None

    def _history_sort_key(self, row: dict[str, object]) -> tuple[str, str]:
        updated_at = str(row.get("updated_at") or "").strip()
        return (updated_at[:10], updated_at)

    def _load_history(self) -> list[dict[str, object]]:
        if not self.history_path.exists():
            return []
        try:
            data = json.loads(self.history_path.read_text())
            if not isinstance(data, list):
                return []
            clean: list[dict[str, object]] = []
            for row in data[-self.history_points :]:
                if not isinstance(row, dict):
                    continue
                clean.append(
                    {
                        "updated_at": row.get("updated_at"),
                        "index_value": _to_float(row.get("index_value")),
                        "index_change_pct": _to_float(row.get("index_change_pct")),
                        "breadth_ratio": _to_float(row.get("breadth_ratio")),
                        "sentiment_score": _to_float(row.get("sentiment_score")),
                        "rising": _to_int(row.get("rising")),
                        "falling": _to_int(row.get("falling")),
                    }
                )
            return clean
        except Exception:
            return []

    def _save_history(self) -> None:
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        self.history_path.write_text(json.dumps(list(self._history), ensure_ascii=False, indent=2))

    def _fetch_yahoo_index_daily_series(self, symbol: str, *, days: int) -> list[dict[str, float | str]]:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=6mo&interval=1d"
        try:
            r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            data = r.json()
        except Exception:
            return []
        result = ((data.get("chart") or {}).get("result") or [])
        if not result:
            return []
        result0 = result[0] or {}
        timestamps = list(result0.get("timestamp") or [])
        q = (((result0.get("indicators") or {}).get("quote") or [{}])[0] or {})
        close_raw = list(q.get("close") or [])
        n = min(len(timestamps), len(close_raw))
        out: list[dict[str, float | str]] = []
        prev_close = None
        for idx in range(n):
            raw_close = close_raw[idx]
            if raw_close is None:
                continue
            try:
                close = float(raw_close)
                ts = int(float(timestamps[idx]))
            except Exception:
                continue
            if close <= 0:
                continue
            date_text = time.strftime("%Y-%m-%d", time.gmtime(ts))
            change_pct = 0.0 if prev_close is None or prev_close <= 0 else ((close / prev_close) - 1.0) * 100.0
            out.append(
                {
                    "date": date_text,
                    "close": close,
                    "change_pct": change_pct,
                }
            )
            prev_close = close
        return out[-max(10, int(days)) :]

    def _approximate_history_from_index(self, *, days: int) -> list[dict[str, object]]:
        kospi = self._fetch_yahoo_index_daily_series("^KS11", days=days + 40)
        kosdaq = self._fetch_yahoo_index_daily_series("^KQ11", days=days + 40)
        if not kospi:
            return []
        kosdaq_map = {str(row.get("date")): row for row in kosdaq if isinstance(row, dict)}
        kospi_closes = [float(row.get("close", 0.0)) for row in kospi]
        approx_rows: list[dict[str, object]] = []
        for idx, row in enumerate(kospi[-max(10, int(days)) :]):
            if not isinstance(row, dict):
                continue
            date_text = str(row.get("date") or "").strip()
            if not date_text:
                continue
            kospi_close = float(row.get("close", 0.0))
            kospi_pct = float(row.get("change_pct", 0.0))
            kosdaq_row = kosdaq_map.get(date_text) or {}
            kosdaq_pct = float(kosdaq_row.get("change_pct", kospi_pct if kospi else 0.0))
            avg_pct = (kospi_pct + kosdaq_pct) / 2.0
            abs_idx = max(0, len(kospi) - max(10, int(days)) + idx)
            ma5_window = kospi_closes[max(0, abs_idx - 4) : abs_idx + 1]
            ma20_window = kospi_closes[max(0, abs_idx - 19) : abs_idx + 1]
            ma5 = statistics.fmean(ma5_window) if ma5_window else kospi_close
            ma20 = statistics.fmean(ma20_window) if ma20_window else kospi_close
            trend_bias = 0.0
            if ma20 > 0:
                trend_bias += ((kospi_close / ma20) - 1.0) * 100.0
            if ma5 > 0:
                trend_bias += ((kospi_close / ma5) - 1.0) * 60.0
            breadth_ratio = max(12.0, min(88.0, 50.0 + (avg_pct * 11.0) + (trend_bias * 1.6)))
            sentiment_score = max(5.0, min(95.0, 50.0 + (avg_pct * 9.0) + (trend_bias * 2.2)))
            rising = int(round(900 * (breadth_ratio / 100.0)))
            falling = max(0, 900 - rising)
            approx_rows.append(
                {
                    "updated_at": f"{date_text} 15:30:00",
                    "index_value": round(kospi_close, 2),
                    "index_change_pct": round(avg_pct, 2),
                    "breadth_ratio": round(breadth_ratio, 2),
                    "sentiment_score": round(sentiment_score, 1),
                    "rising": rising,
                    "falling": falling,
                }
            )
        return approx_rows

    def _backfill_history(self, loaded_history: list[dict[str, object]]) -> list[dict[str, object]]:
        if self.backfill_days <= 0:
            return loaded_history
        try:
            existing_dates = {
                str(row.get("updated_at") or "").strip()[:10]
                for row in loaded_history
                if isinstance(row, dict) and str(row.get("updated_at") or "").strip()
            }
            backfill = self._approximate_history_from_index(days=self.backfill_days)
            changed = False
            merged = list(loaded_history)
            for row in backfill:
                row_date = str(row.get("updated_at") or "").strip()[:10]
                if not row_date or row_date in existing_dates:
                    continue
                merged.append(row)
                existing_dates.add(row_date)
                changed = True
            merged.sort(key=self._history_sort_key)
            merged = merged[-self.history_points :]
            if changed:
                self.history_path.parent.mkdir(parents=True, exist_ok=True)
                self.history_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2))
            return merged
        except Exception:
            return loaded_history

    def get(self, *, force: bool = False) -> dict[str, object]:
        with self._lock:
            stale = (time.time() - self._last_fetch) >= self.refresh_sec
            if not force and not stale and self._cache.get("updated_at"):
                return self._with_countdown(dict(self._cache))

        try:
            fresh = self._fetch_market_vibe()
            with self._lock:
                self._cache.update(fresh)
                self._cache["last_error"] = None
                self._last_fetch = time.time()
                return self._with_countdown(dict(self._cache))
        except Exception as exc:
            with self._lock:
                self._cache["last_error"] = str(exc)
                return self._with_countdown(dict(self._cache))

    def _with_countdown(self, data: dict[str, object]) -> dict[str, object]:
        remain = max(0, int(self.refresh_sec - (time.time() - self._last_fetch)))
        data["next_update_in_sec"] = remain
        return data

    def _fetch_market_vibe(self) -> dict[str, object]:
        settings = load_settings()
        api = KiwoomAPI(settings)
        api.login()
        response = api.request(
            "POST",
            "/api/dostk/sect",
            json_body={"inds_cd": self.index_code},
            headers={"api-id": self.api_id},
        )

        rows = response.get("all_inds_idex") or []
        if not rows:
            raise RuntimeError(f"No market rows in response: {response}")

        base = next((r for r in rows if str(r.get("stk_cd")) == self.index_code), rows[0])

        rising = _to_int(base.get("rising"))
        falling = _to_int(base.get("fall"))
        unchanged = _to_int(base.get("stdns"))
        upper = _to_int(base.get("upl"))
        lower = _to_int(base.get("lst"))

        total = rising + falling + unchanged
        breadth_ratio = (rising / total) if total > 0 else 0.0

        idx_pct = _to_float(base.get("flu_rt"))
        if idx_pct >= 0.7 and breadth_ratio >= 0.55:
            vibe = "RISK-ON"
        elif idx_pct <= -0.7 and breadth_ratio <= 0.45:
            vibe = "RISK-OFF"
        else:
            vibe = "MIXED"

        sortable: list[tuple[float, str]] = []
        for row in rows:
            name = str(row.get("stk_nm", "")).strip()
            if not name or name == str(base.get("stk_nm", "")).strip():
                continue
            sortable.append((_to_float(row.get("flu_rt")), name))
        sortable.sort(key=lambda x: x[0], reverse=True)

        top_up = [f"{name} ({val:+.2f}%)" for val, name in sortable[:3]]
        top_down = [f"{name} ({val:+.2f}%)" for val, name in sorted(sortable[-3:], key=lambda x: x[0])]

        sector_returns = [val for val, _ in sortable]
        sector_mean = statistics.fmean(sector_returns) if sector_returns else 0.0
        sector_median = statistics.median(sector_returns) if sector_returns else 0.0
        sector_dispersion = statistics.pstdev(sector_returns) if len(sector_returns) >= 2 else 0.0
        positive_sectors = len([x for x in sector_returns if x > 0])
        negative_sectors = len([x for x in sector_returns if x < 0])

        breadth_pct = breadth_ratio * 100.0
        breadth_score = max(0.0, min(100.0, breadth_pct))
        index_score = max(0.0, min(100.0, 50.0 + (idx_pct * 6.0)))
        dispersion_penalty = min(30.0, sector_dispersion * 3.0)
        sentiment_score = max(0.0, min(100.0, (breadth_score * 0.5) + (index_score * 0.5) - dispersion_penalty))

        if sentiment_score >= 67:
            regime = "BULLISH"
        elif sentiment_score <= 33:
            regime = "BEARISH"
        else:
            regime = "NEUTRAL"

        momentum_delta_pct = 0.0
        breadth_delta_pct = 0.0
        if self._prev_snapshot is not None:
            momentum_delta_pct = idx_pct - self._prev_snapshot.get("index_change_pct", 0.0)
            breadth_delta_pct = breadth_pct - self._prev_snapshot.get("breadth_pct", 0.0)

        self._prev_snapshot = {"index_change_pct": idx_pct, "breadth_pct": breadth_pct}

        notes: list[str] = []
        if breadth_pct >= 60:
            notes.append("Breadth is strong: participation is broad.")
        elif breadth_pct <= 45:
            notes.append("Breadth is weak: downside participation is broad.")
        else:
            notes.append("Breadth is balanced: mixed participation.")

        if sector_dispersion >= 2.5:
            notes.append("Sector dispersion is high: rotation risk is elevated.")
        elif sector_dispersion <= 1.2:
            notes.append("Sector dispersion is low: tape is moving in sync.")

        if idx_pct >= 1.0 and momentum_delta_pct > 0:
            notes.append("Index upside momentum is accelerating.")
        elif idx_pct <= -1.0 and momentum_delta_pct < 0:
            notes.append("Downside momentum is accelerating.")
        elif abs(momentum_delta_pct) < 0.15:
            notes.append("Momentum shift is small vs last market snapshot.")

        analysis = {
            "sentiment_score": round(sentiment_score, 1),
            "regime": regime,
            "breadth_score": round(breadth_score, 1),
            "index_score": round(index_score, 1),
            "dispersion_penalty": round(dispersion_penalty, 1),
            "momentum_delta_pct": round(momentum_delta_pct, 2),
            "breadth_delta_pct": round(breadth_delta_pct, 2),
            "sector_mean_pct": round(sector_mean, 2),
            "sector_median_pct": round(sector_median, 2),
            "sector_dispersion": round(sector_dispersion, 2),
            "positive_sectors": positive_sectors,
            "negative_sectors": negative_sectors,
            "notes": notes,
        }

        snapshot = {
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "index_value": abs(_to_float(base.get("cur_prc"))),
            "index_change_pct": round(idx_pct, 2),
            "breadth_ratio": round(breadth_pct, 2),
            "sentiment_score": round(sentiment_score, 1),
            "rising": rising,
            "falling": falling,
        }
        if not self._history or self._history[-1].get("updated_at") != snapshot["updated_at"]:
            self._history.append(snapshot)
            self._save_history()

        limit_ratio = ((upper + lower) / total * 100.0) if total > 0 else 0.0
        up_down_ratio = (rising / max(1, falling))
        trde_qty = _to_float(base.get("trde_qty"))
        trde_prica = _to_float(base.get("trde_prica"))

        return {
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "index_code": self.index_code,
            "index_name": str(base.get("stk_nm")),
            "index_value": abs(_to_float(base.get("cur_prc"))),
            "index_change": _to_float(base.get("pred_pre")),
            "index_change_pct": idx_pct,
            "rising": rising,
            "falling": falling,
            "unchanged": unchanged,
            "upper_limit": upper,
            "lower_limit": lower,
            "breadth_ratio": round(breadth_ratio * 100, 1),
            "top_sectors_up": top_up,
            "top_sectors_down": top_down,
            "analysis": analysis,
            "history": list(self._history),
            "stats": {
                "trade_qty": trde_qty,
                "trade_value_eok": trde_prica,
                "limit_ratio": round(limit_ratio, 2),
                "up_down_ratio": round(up_down_ratio, 2),
                "market_total_count": total,
            },
            "source": {
                "provider": "Kiwoom Securities OpenAPI",
                "guide_site": "https://openapi.kiwoom.com/guide/apiguide?dummyVal=0",
                "api_domain": settings.base_url,
                "endpoint": "/api/dostk/sect",
                "api_id": self.api_id,
                "request_body": {"inds_cd": self.index_code},
                "field_map": {
                    "index_value": "cur_prc",
                    "index_change": "pred_pre",
                    "index_change_pct": "flu_rt",
                    "rising": "rising",
                    "falling": "fall",
                    "unchanged": "stdns",
                    "upper_limit": "upl",
                    "lower_limit": "lst",
                },
            },
            "raw_snapshot": {
                "base_row": base,
                "sector_count": len(rows),
            },
            "refresh_sec": self.refresh_sec,
        }


controller = BotController()
market_vibe = MarketVibeService()


def _restart_bot_with_retry(*, retries: int = 20, sleep_sec: float = 0.25) -> tuple[bool, str]:
    controller.stop()
    last_msg = "unknown"
    for _ in range(max(1, retries)):
        started, msg = controller.start()
        last_msg = msg
        if started:
            return True, msg
        if "running" in msg.lower():
            return True, msg
        time.sleep(max(0.05, sleep_sec))
    return False, last_msg


class GlobalMarketService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.refresh_sec = int(os.getenv("GLOBAL_TICKER_REFRESH_SEC", "600"))
        self._last_fetch = 0.0
        self._cache: dict[str, object] = {
            "updated_at": None,
            "quotes": [],
            "sources": {
                "stooq": "https://stooq.com/q/l/?s=...&f=sd2t2ohlcvn&e=csv",
                "kiwoom": "https://openapi.kiwoom.com/guide/apiguide?dummyVal=0",
            },
            "last_error": None,
            "refresh_sec": self.refresh_sec,
        }
        self._stooq_symbols: list[tuple[str, str, str]] = [
            ("^spx", "S&P 500", "index"),
            ("^ndq", "NASDAQ", "index"),
            ("usdkrw", "USD/KRW", "fx"),
            ("xauusd", "GOLD", "commodity"),
            ("hg.f", "COPPER", "commodity"),
            ("cl.f", "WTI", "commodity"),
        ]

    def get(self, *, force: bool = False) -> dict[str, object]:
        with self._lock:
            stale = (time.time() - self._last_fetch) >= self.refresh_sec
            if not force and not stale and self._cache.get("updated_at"):
                return self._with_countdown(dict(self._cache))

        try:
            fresh = self._fetch()
            with self._lock:
                self._cache.update(fresh)
                self._cache["last_error"] = None
                self._last_fetch = time.time()
                return self._with_countdown(dict(self._cache))
        except Exception as exc:
            with self._lock:
                self._cache["last_error"] = str(exc)
                return self._with_countdown(dict(self._cache))

    def _with_countdown(self, data: dict[str, object]) -> dict[str, object]:
        remain = max(0, int(self.refresh_sec - (time.time() - self._last_fetch)))
        data["next_update_in_sec"] = remain
        return data

    def _fetch(self) -> dict[str, object]:
        quotes: list[dict[str, object]] = []
        stooq_map: dict[str, dict[str, object]] = {}
        stooq_sources: list[str] = []

        for symbol, _label, _kind in self._stooq_symbols:
            url = f"https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcvn&e=csv"
            stooq_sources.append(url)
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            reader = csv.reader(io.StringIO(response.text.strip()))
            row = next(reader, [])
            if len(row) < 9:
                continue
            symbol_r, dt, tm, opn, hi, lo, close, vol, name = row[:9]
            if "N/D" in row:
                continue
            open_v = _to_float(opn)
            close_v = _to_float(close)
            pct = ((close_v - open_v) / open_v * 100.0) if open_v else 0.0
            stooq_map[symbol.lower()] = {
                "code": symbol_r,
                "name": name,
                "label": name,
                "value": round(close_v, 3),
                "change_pct": round(pct, 2),
                "source": "stooq",
                "time": f"{dt} {tm}",
            }

        for symbol, label, _kind in self._stooq_symbols:
            row = stooq_map.get(symbol.lower())
            if row:
                row["label"] = label
                quotes.append(row)

        # KOSDAQ from Kiwoom (code 101)
        settings = load_settings()
        api = KiwoomAPI(settings)
        api.login()
        kq = api.request(
            "POST",
            "/api/dostk/sect",
            json_body={"inds_cd": "101"},
            headers={"api-id": "ka20003"},
        )
        kq_rows = kq.get("all_inds_idex") or []
        if kq_rows:
            base = next((x for x in kq_rows if str(x.get("stk_cd")) == "101"), kq_rows[0])
            quotes.append(
                {
                    "code": "101",
                    "name": str(base.get("stk_nm")),
                    "label": "KOSDAQ",
                    "value": abs(_to_float(base.get("cur_prc"))),
                    "change_pct": _to_float(base.get("flu_rt")),
                    "source": "kiwoom",
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )

        return {
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "quotes": quotes,
            "sources": {
                "stooq": stooq_sources,
                "kiwoom_domain": settings.base_url,
                "kiwoom_endpoint": "/api/dostk/sect",
                "kiwoom_api_id": "ka20003",
                "kiwoom_body": {"inds_cd": "101"},
            },
            "refresh_sec": self.refresh_sec,
        }


global_market = GlobalMarketService()
offhours_snapshot = OffHoursSnapshotService()


class DiagnosticsService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: dict[str, object] = {
            "updated_at": None,
            "ok": False,
            "summary": "진단 이력이 없습니다.",
            "checks": [],
        }

    def get(self) -> dict[str, object]:
        with self._lock:
            return dict(self._last)

    def run(self) -> dict[str, object]:
        settings = load_settings()
        checks: list[dict[str, object]] = []
        all_ok = True

        def add(name: str, ok: bool, detail: str) -> None:
            nonlocal all_ok
            checks.append({"name": name, "ok": ok, "detail": detail})
            if not ok:
                all_ok = False

        add("APP KEY", bool(settings.app_key), "설정됨" if settings.app_key else "누락")
        add("SECRET KEY", bool(settings.secret_key), "설정됨" if settings.secret_key else "누락")
        add("ACCOUNT_NO", bool(settings.account_no), settings.account_no or "누락")
        add("PRICE_PATH", bool(settings.price_path), settings.price_path or "누락")
        add("ORDER_PATH", bool(settings.order_path), settings.order_path or "누락")
        add(
            "슬랙 웹훅",
            (not settings.slack_enabled) or bool(settings.slack_webhook_url),
            "활성+설정됨" if settings.slack_enabled and settings.slack_webhook_url else (
                "비활성" if not settings.slack_enabled else "활성인데 URL 누락"
            ),
        )

        if settings.app_key and settings.secret_key:
            try:
                api = KiwoomAPI(settings)
                tk = api.login()
                add("API 로그인", True, f"성공 (expires={tk.expires_dt})")
                if settings.price_path:
                    try:
                        px = api.get_last_price(settings.symbol)
                        add("현재가 조회", px > 0, f"{settings.symbol}={px:,.2f}")
                    except Exception as exc:
                        add("현재가 조회", False, str(exc))
            except Exception as exc:
                add("API 로그인", False, str(exc))
        else:
            add("API 로그인", False, "키 누락으로 건너뜀")

        mode_ok = settings.trade_mode in {"DRY", "LIVE"}
        add(
            "거래 모드",
            mode_ok,
            f"{settings.trade_mode} / LIVE_ARMED={settings.live_armed}",
        )

        summary = "모든 핵심 진단 통과" if all_ok else "일부 항목 실패"
        result = {
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ok": all_ok,
            "summary": summary,
            "checks": checks,
        }
        with self._lock:
            self._last = dict(result)
        return result


diagnostics = DiagnosticsService()


def _pwa_manifest() -> dict[str, object]:
    return {
        "id": "/",
        "name": "AITRADER Dashboard",
        "short_name": "AITRADER",
        "description": "Kiwoom Auto Trader mobile dashboard",
        "lang": "ko-KR",
        "dir": "ltr",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "display_override": ["standalone", "minimal-ui", "browser"],
        "orientation": "portrait-primary",
        "prefer_related_applications": False,
        "background_color": "#07111d",
        "theme_color": "#0b1524",
        "icons": [
            {
                "src": "/app-icon.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any maskable",
            }
        ],
        "shortcuts": [
            {
                "name": "시장 컨텍스트",
                "short_name": "시장",
                "url": "/?market_range=1w#market-section",
            },
            {
                "name": "거래 성과",
                "short_name": "성과",
                "url": "/#performance-section",
            },
            {
                "name": "전략 가이드",
                "short_name": "가이드",
                "url": "/help",
            },
        ],
    }


def _pwa_service_worker_js() -> str:
    return """
const CACHE_NAME = 'aitrader-shell-v2-disabled';
self.addEventListener('install', (event) => {
  event.waitUntil(self.skipWaiting());
});
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
      .then(() => self.registration.unregister())
  );
});
self.addEventListener('fetch', (event) => {
  event.respondWith(fetch(event.request));
});
""".strip()


def _pwa_icon_svg() -> str:
    return """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512'>
<defs>
  <linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>
    <stop offset='0%' stop-color='#123657'/>
    <stop offset='100%' stop-color='#1ca46c'/>
  </linearGradient>
</defs>
<rect width='512' height='512' rx='110' fill='#07111d'/>
<rect x='36' y='36' width='440' height='440' rx='92' fill='url(#g)' opacity='0.18'/>
<path d='M88 364h336' stroke='#89d8ff' stroke-width='20' stroke-linecap='round'/>
<path d='M108 314l84-92 70 46 138-146' fill='none' stroke='#d9f5ff' stroke-width='28' stroke-linecap='round' stroke-linejoin='round'/>
<circle cx='192' cy='222' r='18' fill='#7bf0b7'/>
<circle cx='262' cy='268' r='18' fill='#7bf0b7'/>
<circle cx='400' cy='122' r='18' fill='#7bf0b7'/>
<text x='72' y='132' font-family='Arial, sans-serif' font-size='76' font-weight='700' fill='#eef4ff'>AT</text>
</svg>"""


class Handler(BaseHTTPRequestHandler):
    def _client_ip(self) -> str:
        forwarded = str(self.headers.get("X-Forwarded-For", "") or "").strip()
        if forwarded:
            return forwarded.split(",")[0].strip()
        host = self.client_address[0] if self.client_address else ""
        return str(host or "").strip()

    def _is_local_client(self) -> bool:
        client_ip = self._client_ip()
        return client_ip in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}

    def _parse_cookie_jar(self) -> SimpleCookie[str]:
        jar: SimpleCookie[str] = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", "") or "")
        except Exception:
            pass
        return jar

    def _read_trusted_device_token(self) -> str:
        jar = self._parse_cookie_jar()
        morsel = jar.get("aitrader_trusted_device")
        return str(morsel.value).strip() if morsel else ""

    def _send_base_headers(self, *, content_type: str, content_length: int, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")

    def _settings_for_access(self):
        return load_settings()

    def _is_authorized_request(self, settings) -> bool:
        if not bool(settings.web_access_enabled):
            return True
        if self._is_local_client():
            return True
        token = self._read_trusted_device_token()
        if not token:
            return False
        devices = _cleanup_trusted_web_devices(
            _load_trusted_web_devices(),
            ttl_days=settings.web_trusted_device_days,
            max_devices=settings.web_max_trusted_devices,
        )
        meta = devices.get(token)
        if not meta:
            if devices != _load_trusted_web_devices():
                _save_trusted_web_devices(devices)
            return False
        meta["last_seen_ts"] = f"{time.time():.3f}"
        meta["last_seen_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        meta["last_ip"] = self._client_ip()
        devices[token] = meta
        _save_trusted_web_devices(devices)
        return True

    def _trusted_devices_summary(self, settings) -> tuple[int, str]:
        devices = _cleanup_trusted_web_devices(
            _load_trusted_web_devices(),
            ttl_days=settings.web_trusted_device_days,
            max_devices=settings.web_max_trusted_devices,
        )
        _save_trusted_web_devices(devices)
        labels = []
        for meta in devices.values():
            label = str(meta.get("label") or meta.get("last_ip") or "모바일 기기").strip()
            labels.append(label)
        return len(devices), ", ".join(labels[:3])

    def _render_access_gate(self, settings, *, error: str = "") -> None:
        trusted_count, trusted_labels = self._trusted_devices_summary(settings)
        local_mode = "이 PC(localhost)는 항상 허용됩니다." if self._is_local_client() else "현재 접속 기기는 아직 신뢰 기기로 등록되지 않았습니다."
        error_html = (
            f"<div style='margin:0 0 12px;padding:12px 14px;border-radius:12px;background:#4a1f1f;color:#ffd6d6;border:1px solid #7a3434'>{html.escape(error)}</div>"
            if error else ""
        )
        body = f"""<!doctype html>
<html lang='ko'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>AITRADER Access</title>
  <style>
    :root {{ color-scheme: dark; }}
    body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; background:#101418; color:#eef3f7; }}
    .wrap {{ min-height:100vh; display:grid; place-items:center; padding:24px; }}
    .card {{ width:min(520px, 100%); background:#182129; border:1px solid #293640; border-radius:20px; padding:24px; box-shadow:0 24px 70px rgba(0,0,0,.35); }}
    .k {{ color:#9fb0bf; font-size:14px; line-height:1.55; }}
    h1 {{ margin:0 0 8px; font-size:28px; }}
    input {{ width:100%; box-sizing:border-box; margin-top:8px; border-radius:12px; border:1px solid #364854; background:#0f161c; color:#eef3f7; padding:14px; font-size:16px; }}
    button {{ margin-top:14px; width:100%; border:0; border-radius:12px; padding:14px 16px; font-weight:700; background:#3ca46b; color:white; font-size:16px; cursor:pointer; }}
    .meta {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; margin:16px 0; }}
    .pill {{ border-radius:14px; background:#121a20; border:1px solid #2a3a46; padding:12px; }}
    .pill strong {{ display:block; font-size:13px; color:#8fa2b3; margin-bottom:4px; }}
  </style>
</head>
<body>
  <div class='wrap'>
    <div class='card'>
      <h1>웹 접근 보호</h1>
      <div class='k'>이 대시보드는 현재 <strong>이 PC와 신뢰된 휴대폰</strong>만 열 수 있게 보호됩니다. 휴대폰은 한 번 인증하면 {settings.web_trusted_device_days}일 동안 다시 입력 없이 사용할 수 있습니다.</div>
      <div class='meta'>
        <div class='pill'><strong>현재 정책</strong>{local_mode}</div>
        <div class='pill'><strong>등록된 기기</strong>{trusted_count} / {settings.web_max_trusted_devices}<br><span class='k'>{html.escape(trusted_labels or '아직 없음')}</span></div>
      </div>
      {error_html}
      <form method='post' action='/access-unlock'>
        <label class='k' for='accessKey'>접근 키</label>
        <input id='accessKey' name='access_key' type='password' autocomplete='current-password' placeholder='접근 키를 입력하세요' />
        <button type='submit'>이 기기 허용</button>
      </form>
      <div class='k' style='margin-top:14px'>키를 바꾸고 싶으면 PC에서 설정 창의 <strong>웹 접근 보호</strong> 항목을 수정하면 됩니다.</div>
    </div>
  </div>
</body>
</html>"""
        self._html(body, status=HTTPStatus.UNAUTHORIZED)

    def _set_trusted_device_cookie(self, token: str, *, max_age_days: int) -> None:
        max_age = max(1, max_age_days) * 86400
        cookie = (
            f"aitrader_trusted_device={token}; Path=/; Max-Age={max_age}; "
            "HttpOnly; SameSite=Lax"
        )
        self.send_header("Set-Cookie", cookie)

    def _clear_trusted_device_cookie(self) -> None:
        self.send_header("Set-Cookie", "aitrader_trusted_device=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")

    def _write_response_bytes(self, data: bytes) -> None:
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLEOFError, ssl.SSLZeroReturnError) as exc:
            logging.info(
                "Client disconnected while sending response: %s path=%s client=%s",
                exc.__class__.__name__,
                getattr(self, "path", "-"),
                getattr(self, "client_address", ("-", 0))[0],
            )

    def _bytes(self, data: bytes, *, content_type: str, status: int = 200) -> None:
        self._send_base_headers(content_type=content_type, content_length=len(data), status=status)
        self.end_headers()
        self._write_response_bytes(data)

    def _html(self, body: str, status: int = 200) -> None:
        data = body.encode("utf-8")
        self._send_base_headers(content_type="text/html; charset=utf-8", content_length=len(data), status=status)
        self.end_headers()
        self._write_response_bytes(data)

    def _json(self, obj: dict[str, object], status: int = 200) -> None:
        data = dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self._send_base_headers(content_type="application/json; charset=utf-8", content_length=len(data), status=status)
        self.end_headers()
        self._write_response_bytes(data)

    def _redirect(self, path: str = "/") -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", path)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        qs = parse_qs(parsed_url.query)
        settings = self._settings_for_access()
        if path == "/access":
            self._render_access_gate(settings, error=(qs.get("error", [""])[0] or "").strip())
            return
        if path == "/access-logout":
            token = self._read_trusted_device_token()
            if token:
                devices = _cleanup_trusted_web_devices(
                    _load_trusted_web_devices(),
                    ttl_days=settings.web_trusted_device_days,
                    max_devices=settings.web_max_trusted_devices,
                )
                devices.pop(token, None)
                _save_trusted_web_devices(devices)
            self.send_response(HTTPStatus.SEE_OTHER)
            self._clear_trusted_device_cookie()
            self.send_header("Location", "/access")
            self.end_headers()
            return
        if not self._is_authorized_request(settings):
            self._render_access_gate(settings)
            return
        if path == "/manifest.webmanifest":
            self._bytes(
                dumps(_pwa_manifest(), ensure_ascii=False, indent=2).encode("utf-8"),
                content_type="application/manifest+json; charset=utf-8",
            )
            return

        if path == "/sw.js":
            self._bytes(
                _pwa_service_worker_js().encode("utf-8"),
                content_type="application/javascript; charset=utf-8",
            )
            return

        if path == "/app-icon.svg":
            self._bytes(
                _pwa_icon_svg().encode("utf-8"),
                content_type="image/svg+xml",
            )
            return

        if path == "/status":
            st = controller.status()
            settings = load_settings()
            st = _apply_account_snapshot_fallback(
                st,
                _load_last_known_ui(),
                _load_ledger_snapshot(str(settings.ledger_path)),
            )
            st["market"] = _service_cached(market_vibe)
            st["global_market"] = _service_cached(global_market)
            st["diagnostics"] = diagnostics.get()
            st["offhours_snapshot"] = offhours_snapshot.get()
            _service_refresh_async(market_vibe)
            _service_refresh_async(global_market)
            self._json(st)
            return

        if path == "/diagnostics":
            self._json(diagnostics.get())
            return

        if path == "/help":
            st = controller.status()
            mk = _service_cached(market_vibe)
            glb = _service_cached(global_market)
            _service_refresh_async(market_vibe)
            _service_refresh_async(global_market)
            settings = load_settings()
            offhours = offhours_snapshot.get()
            name_map = _symbol_name_map()
            events = [str(x) for x in st.get("events", [])]
            select_event = _latest_event(events, "SELECT symbols=") or _latest_event(events, "SELECT symbol=")
            rotate_event = _latest_event(events, "ROTATE_TARGET")
            regime_event = _latest_event(events, "REGIME_SHIFT")
            regime_candidate_event = _latest_event(events, "REGIME_CANDIDATE")
            risk_event = _latest_event(events, "RISK_EXIT")
            risk_halt_event = _latest_event(events, "RISK_HALT")
            no_trade_event = _latest_event(events, "NO_TRADE_SUMMARY ")
            parsed = _parse_select_event(select_event)
            current_regime_raw = str(st.get("market_regime") or mk.get("analysis", {}).get("regime", "UNKNOWN"))
            current_regime = html.escape(_display_label(current_regime_raw, kind="regime"))
            selected_symbol_code = str(st.get("selected_symbol") or settings.symbol)
            selected_symbol = html.escape(_display_text(_symbol_label(selected_symbol_code, name_map), "미선정"))
            monitored_symbols_raw = str(st.get("monitored_symbols") or "")
            fallback_snapshot_rows = _prefer_richer_rows(st.get("factor_snapshot"), offhours.get("rows"))
            restored_monitored_symbols = _restore_monitored_symbols(
                monitored_symbols_raw,
                selection_detail=st.get("selection_detail"),
                fallback_rows=fallback_snapshot_rows,
                target_count=settings.trend_select_count,
            )
            if restored_monitored_symbols:
                monitored_symbols_raw = ",".join(restored_monitored_symbols)
                st["monitored_symbols"] = monitored_symbols_raw
            monitored_symbol_labels = [_symbol_label(x, name_map) for x in restored_monitored_symbols]
            monitored_symbol_set = set(restored_monitored_symbols)
            monitored_symbols_display = html.escape(", ".join(monitored_symbol_labels))
            live_stock_rows = list(st.get("stock_statuses") or [])
            use_fallback_rows = (
                not live_stock_rows
                or (
                    isinstance(fallback_snapshot_rows, list)
                    and len(fallback_snapshot_rows) > len(live_stock_rows)
                    and len(live_stock_rows) <= 1
                )
            )
            stock_rows_for_display = (live_stock_rows if not use_fallback_rows else []) or _fallback_stock_rows_from_snapshot(
                fallback_snapshot_rows,
                selected_symbols=monitored_symbol_set,
                source_label="장외 스냅샷",
                updated_at=str(offhours.get("updated_at") or ""),
            )
            monitored_symbols_chips = "".join(
                f"<span class='chip' style='font-size:13px'>{html.escape(x)}</span>"
                for x in monitored_symbol_labels
            )
            monitored_symbol_count = len(restored_monitored_symbols)
            positions_summary_display = html.escape(
                _format_positions_summary(str(st.get("positions_summary") or ""), name_map)
            )
            selection_ref = html.escape(_display_text(st.get("strategy_reference"), "기준 산출 전"))
            selection_score = _to_float(st.get("selection_score"))
            selection_reason_text = html.escape(_display_text(st.get("selection_reason"), "선정 사유 집계 전"))
            daily_selection_status = html.escape(_display_text(st.get("daily_selection_status"), "당일 선정 상태 집계 전"))
            daily_selection_day = html.escape(_display_text(st.get("daily_selection_day"), "미확정"))
            selection_detail = st.get("selection_detail") if isinstance(st.get("selection_detail"), dict) else {}
            watchlist_only_mode = (
                isinstance(selection_detail, dict)
                and not list(selection_detail.get("selected_symbols") or [])
                and bool(list(selection_detail.get("analysis_watch_symbols") or []))
            )
            if watchlist_only_mode:
                selection_ref = html.escape("방어형 감시 모드")
                if not str(st.get("selection_reason") or "").strip():
                    st["selection_reason"] = "약세장에서는 신규 롱 진입 대신 상위 감시 후보만 유지합니다."
                selection_reason_text = html.escape(_display_text(st.get("selection_reason"), "방어형 감시 모드"))
            selected_factor = (
                selection_detail.get("selected_factor")
                if isinstance(selection_detail.get("selected_factor"), dict)
                else {}
            )
            selected_sector_summary = ", ".join(
                f"{_symbol_label(str(x.get('symbol') or ''), name_map)}:{str(x.get('sector') or 'UNMAPPED')}"
                for x in list(selection_detail.get("selected_sectors", []))[:5]
                if isinstance(x, dict)
            )
            selection_exact_summary = html.escape(
                (
                    f"rank {int(_to_float(selection_detail.get('selected_rank')))}"
                    f"/{max(1, int(_to_float(selection_detail.get('ranked_size'))))} | "
                    f"score {float(selection_detail.get('selected_score', 0.0)):+.2f} | "
                    f"M {float(selected_factor.get('momentum_pct', 0.0)):+.2f}% | "
                    f"5D {float(selected_factor.get('ret5_pct', 0.0)):+.2f}% | "
                    f"상대강도 {float(selected_factor.get('relative_pct', 0.0)):+.2f}% | "
                    f"추세 {float(selected_factor.get('trend_pct', 0.0)):+.2f}% | "
                    f"변동성 {float(selected_factor.get('volatility_pct', 0.0)):.2f}% | "
                    f"ATR {float(selected_factor.get('atr14_pct', 0.0)):.2f}% | "
                    f"RAM {float(selected_factor.get('risk_adjusted_momentum', 0.0)):.2f} | "
                    f"TEF {float(selected_factor.get('trend_efficiency', 0.0)):.2f} | "
                    f"RSI {float(selected_factor.get('daily_rsi', 0.0)):.1f} | "
                    f"ATTN {float(selected_factor.get('attention_ratio', 0.0)):.2f} | "
                    f"SPIKE {float(selected_factor.get('value_spike_ratio', 0.0)):.2f}"
                )
            )
            focus_has_selection = bool(selection_detail) or bool(monitored_symbol_set)
            selected_focus_metrics = (
                "".join(
                    [
                        f"<div class='rank-metric'>선정 점수 <strong>{selection_score:+.2f}</strong></div>",
                        f"<div class='rank-metric'>20일 수익률 <strong>{float(selected_factor.get('momentum_pct', 0.0)):+.2f}%</strong></div>",
                        f"<div class='rank-metric'>5일 수익률 <strong>{float(selected_factor.get('ret5_pct', 0.0)):+.2f}%</strong></div>",
                        f"<div class='rank-metric'>상대강도 <strong>{float(selected_factor.get('relative_pct', 0.0)):+.2f}%</strong></div>",
                        f"<div class='rank-metric'>추세 <strong>{float(selected_factor.get('trend_pct', 0.0)):+.2f}%</strong></div>",
                        f"<div class='rank-metric'>RAM <strong>{float(selected_factor.get('risk_adjusted_momentum', 0.0)):.2f}</strong></div>",
                        f"<div class='rank-metric'>TEF <strong>{float(selected_factor.get('trend_efficiency', 0.0)):.2f}</strong></div>",
                        f"<div class='rank-metric'>TQP <strong>{float(selected_factor.get('top_rank_quality_penalty', 0.0)):.2f}</strong></div>",
                        f"<div class='rank-metric'>RSI <strong>{float(selected_factor.get('daily_rsi', 0.0)):.1f}</strong></div>",
                        f"<div class='rank-metric'>관심도 <strong>{float(selected_factor.get('attention_ratio', 0.0)):.2f}</strong></div>",
                        f"<div class='rank-metric'>스파이크 <strong>{float(selected_factor.get('value_spike_ratio', 0.0)):.2f}</strong></div>",
                    ]
                )
                if focus_has_selection
                else "<div class='k'>핵심 지표 집계 중입니다.</div>"
            )
            selected_focus_badges = (
                _selection_reason_badges_html(
                    {
                        "symbol": selected_symbol_code,
                        "rank": selection_detail.get("selected_rank"),
                        "momentum_pct": selected_factor.get("momentum_pct"),
                        "ret5_pct": selected_factor.get("ret5_pct"),
                        "attention_ratio": selected_factor.get("attention_ratio"),
                        "value_spike_ratio": selected_factor.get("value_spike_ratio"),
                        "daily_rsi": selected_factor.get("daily_rsi"),
                    },
                    selected_symbols=monitored_symbol_set,
                    selected_limit=settings.trend_select_count,
                )
                if focus_has_selection
                else ""
            )
            selection_rank_rows = "".join(
                "<tr>"
                f"<td>{int(_to_float(x.get('rank')))}</td>"
                f"<td>{html.escape(_symbol_label(str(x.get('symbol') or ''), name_map))}</td>"
                f"<td>{_to_float(x.get('score')):+.2f}</td>"
                f"<td>{_to_float(x.get('momentum_pct')):+.2f}%</td>"
                f"<td>{_to_float(x.get('ret5_pct')):+.2f}%</td>"
                f"<td>{_to_float(x.get('relative_pct')):+.2f}%</td>"
                f"<td>{_to_float(x.get('trend_pct')):+.2f}%</td>"
                f"<td>{_to_float(x.get('volatility_pct')):.2f}%</td>"
                f"<td>{_to_float(x.get('risk_adjusted_momentum')):.2f}</td>"
                f"<td>{_to_float(x.get('trend_efficiency')):.2f}</td>"
                f"<td>{_to_float(x.get('top_rank_quality_penalty')):.2f}</td>"
                f"<td>{_to_float(x.get('attention_ratio')):.2f}</td>"
                f"<td>{_to_float(x.get('value_spike_ratio')):.2f}</td>"
                f"<td>{_to_float(x.get('daily_rsi')):.1f}</td>"
                "</tr>"
                for x in list(selection_detail.get("top_ranked", []))[:5]
                if isinstance(x, dict)
            )
            selection_rank_cards = "".join(
                (
                    "<div class='rank-card'>"
                    "<div class='rank-card-top'>"
                    "<div>"
                    f"<div class='rank-badge'>상위 후보 #{int(_to_float(x.get('rank')))}</div>"
                    f"<div class='v' style='font-size:18px;margin-top:8px'>{html.escape(_symbol_label(str(x.get('symbol') or ''), name_map))}</div>"
                    f"<div class='k' style='margin-top:4px'>RSI {_to_float(x.get('daily_rsi')):.1f} · 관심도 {_to_float(x.get('attention_ratio')):.2f} · 스파이크 {_to_float(x.get('value_spike_ratio')):.2f} · TQP {_to_float(x.get('top_rank_quality_penalty')):.2f} · 연장패널티 {_to_float(x.get('overextension_penalty')):.2f}</div>"
                    "</div>"
                    f"<div class='rank-score'>{_to_float(x.get('score')):+.2f}</div>"
                    "</div>"
                    f"<div class='reason-badges'>{_selection_reason_badges_html(x, selected_symbols=monitored_symbol_set, selected_limit=settings.trend_select_count)}</div>"
                    "<div class='rank-meta'>"
                    f"<div class='rank-metric'>20일 수익률 <strong>{_to_float(x.get('momentum_pct')):+.2f}%</strong></div>"
                    f"<div class='rank-metric'>5일 수익률 <strong>{_to_float(x.get('ret5_pct')):+.2f}%</strong></div>"
                    f"<div class='rank-metric'>상대강도 <strong>{_to_float(x.get('relative_pct')):+.2f}%</strong></div>"
                    f"<div class='rank-metric'>추세 <strong>{_to_float(x.get('trend_pct')):+.2f}%</strong></div>"
                        f"<div class='rank-metric'>변동성 <strong>{_to_float(x.get('volatility_pct')):.2f}%</strong></div>"
                        f"<div class='rank-metric'>TQP <strong>{_to_float(x.get('top_rank_quality_penalty')):.2f}</strong></div>"
                    "</div>"
                    "</div>"
                )
                for x in list(selection_detail.get("top_ranked", []))[:5]
                if isinstance(x, dict)
            )
            fallback_reason_text = html.escape(_display_text(selection_detail.get("fallback_reason"), ""))
            universe_seed_symbols = selection_universe_symbols(settings)
            universe_seed_count = len(universe_seed_symbols)
            universe_source_label = "KIND 자동 유니버스" if settings.auto_universe_enabled else "수동 유니버스"
            universe_scope_text = (
                f"{universe_source_label} 기준으로 전 종목을 스캔하고, "
                f"하루 한 번 후보 풀을 갱신한 뒤 상위 {max(3, int(settings.candidate_refresh_top_n))}개 후보를 운영 준비 대상으로 반영합니다."
            )
            universe_seed_text = (
                f"현재 수동 seed {universe_seed_count}개"
                if universe_seed_count > 1
                else "수동 seed 없이 자동 유니버스만 사용"
            )
            config_state = (qs.get("config", [""])[0] or "").strip().lower()
            cfg_base_url = html.escape(settings.base_url)
            cfg_account_no = html.escape(settings.account_no)
            cfg_price_path = html.escape(settings.price_path)
            cfg_order_path = html.escape(settings.order_path)
            cfg_slack_webhook = html.escape(settings.slack_webhook_url)
            cfg_slack_keywords = html.escape(settings.slack_event_keywords)
            slack_enabled_checked = "checked" if settings.slack_enabled else ""
            regime_idx_pct = _to_float(mk.get("index_change_pct"))
            regime_breadth_pct = _to_float(mk.get("breadth_ratio"))
            regime_calc, regime_basis = _regime_reason(regime_idx_pct, regime_breadth_pct)
            stock_status_cards = "".join(
                (
                    (
                        lambda factor_ready, factor_count, factor_need, factor_reason: (
                    f"<div class='stock-card {_stock_readiness_meta(r)[0]}'>"
                    "<div class='stock-head'>"
                    "<div>"
                    f"<div class='stock-title'>{html.escape(_symbol_label(str(r.get('symbol') or ''), name_map))}</div>"
                    f"<div class='stock-sub'>{html.escape(str(r.get('sector') or 'UNMAPPED'))}</div>"
                    "</div>"
                    "<div class='stock-head-right'>"
                    f"<span class='stock-badge {'selected' if r.get('selected') else 'watch'}'>{'선정' if r.get('selected') else '감시'}</span>"
                    f"<span class='readiness-pill {_stock_readiness_meta(r)[0]}'>{_stock_readiness_meta(r)[1]}</span>"
                    f"<span class='signal-pill signal-{html.escape(str(r.get('action') or 'HOLD').lower())}'>{html.escape(str(r.get('action') or '-'))}</span>"
                    "</div>"
                    "</div>"
                    "<div class='stock-status-summary'>"
                    f"<div class='label'>{_stock_readiness_meta(r)[1]}</div>"
                    f"<div class='desc'>{_stock_readiness_meta(r)[2]}</div>"
                    "</div>"
                    "<div class='stock-price-row'>"
                    f"<div><div class='k'>현재가</div><div class='v'>{_to_float(r.get('price')):,.0f}</div></div>"
                    f"<div><div class='k'>점수</div><div class='v'>{_to_float(r.get('score')):+.2f}</div></div>"
                    f"<div><div class='k'>수익률</div><div class='v'>{_to_float(r.get('return_pct')):+.2f}%</div></div>"
                    f"<div><div class='k'>확인</div><div class='v'>{int(_to_float(r.get('confirm_progress')))} / {int(_to_float(r.get('confirm_needed')))}</div></div>"
                    "</div>"
                    "<div class='stock-metrics'>"
                    f"<div class='metric-chip'><span>보유</span><strong>{int(_to_float(r.get('qty')))}주</strong></div>"
                    f"<div class='metric-chip'><span>평단</span><strong>{_to_float(r.get('avg_price')):,.0f}</strong></div>"
                    f"<div class='metric-chip'><span>RSI</span><strong>{_factor_metric_text(r, 'factor_daily_rsi', fmt='{:.1f}')}</strong></div>"
                    f"<div class='metric-chip'><span>ATR14</span><strong>{_factor_metric_text(r, 'factor_atr14_pct', fmt='{:.2f}%')}</strong></div>"
                    f"<div class='metric-chip'><span>관심도</span><strong>{_factor_metric_text(r, 'factor_attention_ratio', fmt='{:.2f}')}</strong></div>"
                    f"<div class='metric-chip'><span>스파이크</span><strong>{_factor_metric_text(r, 'factor_value_spike_ratio', fmt='{:.2f}')}</strong></div>"
                    f"<div class='metric-chip'><span>연장패널티</span><strong>{_factor_metric_text(r, 'factor_overextension_penalty', fmt='{:.2f}')}</strong></div>"
                    f"<div class='metric-chip'><span>일봉</span><strong>{factor_count} / {factor_need}</strong></div>"
                    f"<div class='metric-chip'><span>데이터</span><strong>{int(_to_float(r.get('data_age_sec')))}s</strong></div>"
                    f"<div class='metric-chip'><span>쿨다운</span><strong>{int(_to_float(r.get('cooldown_left_sec')))}s</strong></div>"
                    "</div>"
                    "<div class='gate-row'>"
                    f"<span class='gate {'pass' if r.get('factor_trend_ok') else 'fail'}'>Trend</span>"
                    f"<span class='gate {'pass' if r.get('factor_structure_ok') else 'fail'}'>Structure</span>"
                    f"<span class='gate {'pass' if r.get('factor_breakout_ok') else 'fail'}'>Breakout</span>"
                    f"<span class='gate {'fail' if r.get('factor_overheat') else 'pass'}'>{'Overheat' if r.get('factor_overheat') else 'Cool'}</span>"
                    "</div>"
                    "<div class='stock-factor-line'>"
                    f"<span>20D {_factor_metric_text(r, 'factor_momentum_pct', fmt='{:+.2f}%')}</span>"
                    f"<span>5D {_factor_metric_text(r, 'factor_ret5_pct', fmt='{:+.2f}%')}</span>"
                    f"<span>상대강도 {_factor_metric_text(r, 'factor_relative_pct', fmt='{:+.2f}%')}</span>"
                    f"<span>추세 {_factor_metric_text(r, 'factor_trend_pct', fmt='{:+.2f}%')}</span>"
                    f"<span>변동성 {_to_float(r.get('volatility_pct')):.2f}%</span>"
                    "</div>"
                    f"<div class='stock-reason'>Factor data: {html.escape((f'일봉 {factor_count}/{factor_need} | ' + _display_text(factor_reason, 'ready')) if factor_count > 0 else _display_text(factor_reason, '일봉 데이터 없음'))}</div>"
                    f"<div class='stock-reason'>{html.escape(str(r.get('decision_reason') or '-'))}</div>"
                    "</div>"
                        )
                    )(
                        bool(r.get('factor_data_ready')),
                        int(_to_float(r.get('factor_daily_bar_count'))),
                        max(1, int(_to_float(r.get('factor_required_daily_bars')))),
                        str(r.get('factor_data_reason') or '').strip(),
                    )
                )
                for r in stock_rows_for_display
            )
            reason_hist_rows = "".join(
                f"<tr><td>{html.escape(str(k))}</td><td>{int(_to_float(v))}</td></tr>"
                for k, v in list((st.get("reason_histogram") or {}).items())[:12]
            )
            reason_hist_cards = _block_reason_cards_html(st.get("reason_histogram"))
            stock_status_map = {
                str(r.get("symbol") or "").strip(): r
                for r in stock_rows_for_display
                if isinstance(r, dict) and str(r.get("symbol") or "").strip()
            }
            top_ranked_today = (
                selection_detail.get("top_ranked")
                if isinstance(selection_detail.get("top_ranked"), list)
                else []
            )
            today_top_candidate_cards = "".join(
                (
                    "<div class='rank-card'>"
                    "<div class='rank-card-top'>"
                    "<div>"
                    f"<div class='rank-badge'>오늘 후보 #{int(_to_float(row.get('rank')))}</div>"
                    f"<div class='v' style='font-size:17px;margin-top:8px'>{html.escape(_symbol_label(str(row.get('symbol') or ''), name_map))}</div>"
                    f"<div class='k' style='margin-top:4px'>점수 {_to_float(row.get('score')):+.2f} | RSI {_to_float(row.get('daily_rsi')):.1f} | {'실전 우선' if int(_to_float(row.get('rank'))) == 1 else '보조 후보'}</div>"
                    "</div>"
                    "</div>"
                    "<div class='rank-meta'>"
                    f"<div class='rank-metric'>20D <strong>{_to_float(row.get('momentum_pct')):+.2f}%</strong></div>"
                    f"<div class='rank-metric'>추세 <strong>{_to_float(row.get('trend_pct')):+.2f}%</strong></div>"
                    f"<div class='rank-metric'>관심도 <strong>{_to_float(row.get('attention_ratio')):.2f}</strong></div>"
                    f"<div class='rank-metric'>스파이크 <strong>{_to_float(row.get('value_spike_ratio')):.2f}</strong></div>"
                    "</div>"
                    f"{_candidate_key_risk_badge(row, stock_status_map.get(str(row.get('symbol') or '').strip()))}"
                    f"{_today_candidate_timeline_html(str(row.get('symbol') or ''), stock_status_map.get(str(row.get('symbol') or '').strip()), list(today_trade_summary.get('trade_rows') or []))}"
                    f"<div class='reason-badges'>{_selection_reason_badges_html(row, selected_symbols=monitored_symbol_set, selected_limit=settings.trend_select_count)}</div>"
                    "</div>"
                )
                for row in top_ranked_today[:3]
                if isinstance(row, dict)
            )
            factor_rows = "".join(
                "<tr>"
                f"<td>{html.escape(_symbol_label(str(x.get('symbol') or ''), name_map))}</td>"
                f"<td>{html.escape(str(x.get('sector') or 'UNMAPPED'))}</td>"
                f"<td>{_to_float(x.get('score')):+.2f}</td>"
                f"<td>{_to_float(x.get('momentum_pct')):+.2f}%</td>"
                f"<td>{_to_float(x.get('ret5_pct')):+.2f}%</td>"
                f"<td>{_to_float(x.get('relative_pct')):+.2f}%</td>"
                f"<td>{_to_float(x.get('trend_pct')):+.2f}%</td>"
                f"<td>{_to_float(x.get('volatility_pct')):.2f}%</td>"
                f"<td>{_to_float(x.get('attention_ratio')):.2f}</td>"
                f"<td>{_to_float(x.get('value_spike_ratio')):.2f}</td>"
                f"<td>{_to_float(x.get('daily_rsi')):.1f}</td>"
                f"<td>{'Y' if x.get('overextended') else '-'}</td>"
                "</tr>"
                for x in st.get("factor_snapshot", [])
            )
            reconcile = st.get("reconcile_stats") or {}
            event_summary_cards = _event_summary_cards_html(events)
            order_rows = "".join(
                "<tr>"
                f"<td>{html.escape(str(o.get('ts') or '-'))}</td>"
                f"<td>{html.escape(_symbol_label(str(o.get('symbol') or ''), name_map))}</td>"
                f"<td>{html.escape(str(o.get('side') or '-'))}</td>"
                f"<td>{int(_to_float(o.get('qty')))}</td>"
                f"<td>{_to_float(o.get('price')):,.0f}</td>"
                f"<td>{html.escape(str(o.get('status') or '-'))}</td>"
                f"<td>{html.escape(str(o.get('detail') or '-'))}</td>"
                "</tr>"
                for o in list(st.get('order_journal', []))[-20:]
            )
            order_summary_cards = _order_summary_cards_html(reconcile, st.get("order_journal"))
            alerts: list[dict[str, str]] = []
            if st.get("last_error"):
                alerts.append(
                    {
                        "key": "last_error",
                        "level": "ERR",
                        "msg": f"봇 오류: {st.get('last_error')}",
                    }
                )
            if st.get("risk_halt_active"):
                alerts.append(
                    {
                        "key": "risk_halt",
                        "level": "WARN",
                        "msg": f"리스크 홀트 ON: {st.get('risk_halt_reason') or '-'}",
                    }
                )
            if st.get("stale_data_active"):
                alerts.append(
                    {
                        "key": "stale_data",
                        "level": "WARN",
                        "msg": f"시세 지연: {st.get('stale_data_reason') or '-'}",
                    }
                )
            if mk.get("last_error") or glb.get("last_error"):
                alerts.append(
                    {
                        "key": "data_source",
                        "level": "WARN",
                        "msg": "외부 데이터 소스 오류 발생(시장/글로벌 데이터 중 1개 이상).",
                    }
                )
            if _to_int(reconcile.get("timeout_this_loop")) > 0:
                alerts.append(
                    {
                        "key": "reconcile_timeout",
                        "level": "WARN",
                        "msg": f"리컨실 timeout 발생: {int(_to_float(reconcile.get('timeout_this_loop')))}건",
                    }
                )
            if _to_int(reconcile.get("pending")) > 0:
                alerts.append(
                    {
                        "key": "reconcile_pending",
                        "level": "INFO",
                        "msg": f"리컨실 pending: {int(_to_float(reconcile.get('pending')))}건",
                    }
                )
            if not alerts:
                alerts.append({"key": "all_ok", "level": "OK", "msg": "현재 핵심 알림 없음"})
            if no_trade_event:
                alerts.append(
                    {
                        "key": "no_trade_summary",
                        "level": "INFO",
                        "msg": f"오늘 무거래 요약: {no_trade_event}",
                    }
                )
            alerts_html = "".join(
                (
                    "<div class='card alert-row alert-level-"
                    + html.escape(a["level"].lower())
                    + "' data-alert-key='"
                    + html.escape(a["key"])
                    + "'>"
                    + f"<div class='k'>[{html.escape(a['level'])}]</div>"
                    + f"<div class='v' style='font-size:13px'>{html.escape(a['msg'])}</div>"
                    + f"<div style='margin-top:6px;text-align:right'><button type='button' class='refresh alert-ack-btn' data-alert-key='{html.escape(a['key'])}'>확인</button></div>"
                    + "</div>"
                )
                for a in alerts
            )
            menu_guide_rows = "".join(
                [
                    "<tr><td>봇 시작 / 봇 중지</td><td>전략 루프 실행을 시작하거나 멈춥니다.</td><td>실거래 전에는 먼저 모의투자로 상태를 점검합니다.</td></tr>",
                    "<tr><td>모의투자 전환 / 실거래 전환</td><td>주문 모드를 전환합니다.</td><td>실거래 전환은 반드시 실거래 승인 체크 후 사용합니다.</td></tr>",
                    "<tr><td>화면 가이드</td><td>대시보드 영역, 버튼, 새로고침 방식, 체크리스트를 설명합니다.</td><td>처음 접속했을 때 가장 먼저 확인합니다.</td></tr>",
                    "<tr><td>전략 가이드</td><td>자동 유니버스 스캔, 선정 기준, 진입, 청산, 예외 규칙을 설명합니다.</td><td>설정 변경 전후 기준을 다시 확인할 때 사용합니다.</td></tr>",
                    "<tr><td>설정</td><td>연결, 리스크, 추세 전략 파라미터를 저장합니다.</td><td>저장 후 봇이 자동 재시작되므로 반영 여부를 운영 상태에서 확인합니다.</td></tr>",
                    "<tr><td>빠른 진단</td><td>API, 경로, 계정, 알림 연결 상태를 점검합니다.</td><td>오류가 있거나 장 시작 전 점검이 필요할 때 실행합니다.</td></tr>",
                    "<tr><td>자동 새로고침</td><td>대시보드 갱신 주기를 한 곳에서만 설정합니다.</td><td>운영 중에는 30~60초, 점검 중에는 더 짧게 사용합니다.</td></tr>",
                ]
            )
            panel_guide_rows = "".join(
                [
                    "<tr><td>헤드라인/시장 개요</td><td>현재 시장 국면, 시장 강도, 상승 비율, 변동성, 관심 방향을 먼저 확인하는 영역입니다.</td></tr>",
                    "<tr><td>운영 상태</td><td>봇 실행 여부, 계좌 상태, 포트폴리오 히트, 손실 캡, 최근 오류를 한 번에 확인합니다.</td></tr>",
                    "<tr><td>오늘의 포커스</td><td>신규 롱이 가능하면 top1 선정 종목을, 약세장/충격장에서는 방어형 감시 후보 목록을 같은 카드에서 보여줍니다.</td></tr>",
                    "<tr><td>종목 실행 보드</td><td>종목별 액션과 추세, 구조, 돌파, 과열 여부를 함께 읽는 실시간 실행 화면입니다. 장외에는 장외 스냅샷 진단 카드로 대체됩니다.</td></tr>",
                    "<tr><td>운영 점검</td><td>중요 알림과 최근 진단 결과를 한 카드에서 빠르게 확인하는 영역입니다.</td></tr>",
                    "<tr><td>차단 사유</td><td>최근 루프에서 진입을 막은 주요 이유와 팩터 스냅샷을 함께 보여줍니다.</td></tr>",
                    "<tr><td>이벤트 스트림 / 주문 저널</td><td>최근 이벤트 요약, 주문 상태, 정합 결과를 운영 보조 정보로 확인합니다.</td></tr>",
                ]
            )
            factor_glossary_rows = "".join(
                [
                    "<tr><td>점수</td><td>멀티팩터 가중합입니다. 높을수록 우선순위가 높습니다.</td></tr>",
                    "<tr><td>20일 수익률</td><td>최근 20거래일 누적 수익률입니다.</td></tr>",
                    "<tr><td>5일 수익률</td><td>최근 5거래일 누적 수익률입니다.</td></tr>",
                    "<tr><td>상대강도</td><td>종목 모멘텀에서 시장 지수 변화를 뺀 값입니다.</td></tr>",
                    "<tr><td>RAM</td><td>위험조정 모멘텀입니다. 같은 상승률이라도 변동성 대비 더 효율적으로 오른 종목을 높게 봅니다.</td></tr>",
                    "<tr><td>TEF</td><td>추세 효율입니다. 이동평균 추세가 변동성 대비 얼마나 깔끔하게 이어지는지 보여줍니다.</td></tr>",
                    "<tr><td>TQP</td><td>top rank 품질 패널티입니다. 상위 1순위 후보가 되기엔 RAM/TEF가 약한 종목이면 감점합니다.</td></tr>",
                    "<tr><td>추세</td><td>현재가와 이동평균선 위치 관계를 점수화한 값입니다.</td></tr>",
                    "<tr><td>관심도</td><td>최근 5일 평균 거래대금이 20일 평균 대비 얼마나 늘었는지 보여줍니다.</td></tr>",
                    "<tr><td>스파이크</td><td>최근 거래대금이 평균 대비 얼마나 급증했는지 보여줍니다.</td></tr>",
                    "<tr><td>ATR14</td><td>최근 14일 평균 변동폭 비율입니다. 손절과 익절 계산에 사용합니다.</td></tr>",
                    "<tr><td>연장패널티</td><td>너무 멀리 올라간 추세 추격을 감점하는 값입니다.</td></tr>",
                    "<tr><td>추격 과열</td><td>점수는 높아도 이미 과도하게 연장된 종목으로 판단된 상태입니다.</td></tr>",
                    "<tr><td>약세 예외 후보</td><td>약세장에서도 상대강도와 거래대금이 매우 강해 제한적으로 진입 검토하는 상태입니다.</td></tr>",
                    "<tr><td>시그널 확인</td><td>BUY 또는 SELL 신호가 연속 확인될 때만 주문을 실행합니다.</td></tr>",
                ]
            )
            checklist_beginner_rows = "".join(
                [
                    "<tr><td>1</td><td>원클릭 진단 실행 후 FAIL 항목 0개 확인</td><td style='text-align:center'><input type='checkbox' class='check-item' data-check-id='beginner-1'/></td></tr>",
                    "<tr><td>2</td><td>모의투자 유지, 실거래 승인 비활성 확인</td><td style='text-align:center'><input type='checkbox' class='check-item' data-check-id='beginner-2'/></td></tr>",
                    "<tr><td>3</td><td>전략 가이드에서 현재 시장 국면과 오늘의 포커스/상위 후보 비교 확인</td><td style='text-align:center'><input type='checkbox' class='check-item' data-check-id='beginner-3'/></td></tr>",
                    "<tr><td>4</td><td>최근 이벤트에서 ERROR/RISK 이벤트 먼저 점검</td><td style='text-align:center'><input type='checkbox' class='check-item' data-check-id='beginner-4'/></td></tr>",
                ]
            )
            checklist_intermediate_rows = "".join(
                [
                    "<tr><td>1</td><td>차단 사유 패널에서 blocked 사유 상위 3개 점검</td><td style='text-align:center'><input type='checkbox' class='check-item' data-check-id='intermediate-1'/></td></tr>",
                    "<tr><td>2</td><td>20일/5일 수익률, 상대강도, 추세, 변동성과 최종 점수 정합성 확인</td><td style='text-align:center'><input type='checkbox' class='check-item' data-check-id='intermediate-2'/></td></tr>",
                    "<tr><td>3</td><td>ATR/손절/익절 배수와 현재 시장 국면별 리스크 강도 매칭 확인</td><td style='text-align:center'><input type='checkbox' class='check-item' data-check-id='intermediate-3'/></td></tr>",
                    "<tr><td>4</td><td>주문 리컨실 대기/시간초과 증가 여부 감시</td><td style='text-align:center'><input type='checkbox' class='check-item' data-check-id='intermediate-4'/></td></tr>",
                ]
            )
            checklist_live_rows = "".join(
                [
                    "<tr><td>1</td><td>실거래 전환 전 모의투자에서 1일 이상 로그 검증</td><td style='text-align:center'><input type='checkbox' class='check-item' data-check-id='live-1'/></td></tr>",
                    "<tr><td>2</td><td>실거래 승인 체크 + 계정/경로/슬랙 재진단</td><td style='text-align:center'><input type='checkbox' class='check-item' data-check-id='live-2'/></td></tr>",
                    "<tr><td>3</td><td>초기 30분은 소수량(보수 프로필)로 모니터링</td><td style='text-align:center'><input type='checkbox' class='check-item' data-check-id='live-3'/></td></tr>",
                    "<tr><td>4</td><td>리스크 홀트/데이터 지연 발생 시 즉시 중지</td><td style='text-align:center'><input type='checkbox' class='check-item' data-check-id='live-4'/></td></tr>",
                ]
            )

            page = f"""<!doctype html>
<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<meta name='theme-color' content='#0b1524'>
<meta name='apple-mobile-web-app-capable' content='yes'>
<meta name='apple-mobile-web-app-status-bar-style' content='black-translucent'>
<meta name='apple-mobile-web-app-title' content='AITRADER'>
<link rel='manifest' href='/manifest.webmanifest'>
<link rel='icon' href='/app-icon.svg' type='image/svg+xml'>
<link rel='apple-touch-icon' href='/app-icon.svg'>
<title>전략 가이드 | Kiwoom Auto Trader</title>
<style>
body{{margin:0;font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI;background:#0b1220;color:#e7efff;}}
.wrap{{max-width:1280px;margin:0 auto;padding:14px;}}
.head{{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px;}}
.card{{background:#111a2b;border:1px solid #253553;border-radius:10px;padding:12px;margin-bottom:10px;}}
.k{{color:#9fb1d1;font-size:12px;}}
.v{{font-weight:700;font-size:16px;}}
.grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;}}
table{{width:100%;border-collapse:collapse;font-size:12px;}}
th,td{{padding:6px;border-bottom:1px solid #253553;text-align:right;}}
th:first-child,td:first-child{{text-align:left;}}
a.btn{{display:inline-block;background:#243a56;color:#d8e7ff;text-decoration:none;padding:8px 12px;border-radius:8px;font-weight:700;}}
code{{background:#1a2a45;padding:2px 6px;border-radius:6px;}}
pre{{background:#0a1322;border-radius:8px;padding:8px;overflow:auto;max-height:180px;}}
@media(max-width:980px){{.grid{{grid-template-columns:1fr;}}}}
</style></head><body>
<div class='wrap'>
  <div class='head'>
    <div>
      <div class='v'>자동매매 전략 가이드</div>
      <div class='k'>자동 유니버스 스캔, 상승 추세 선별, 진입/청산 조건, 예외 규칙을 현재 설정값 기준으로 설명합니다.</div>
    </div>
    <a class='btn' href='/'>대시보드로 돌아가기</a>
  </div>

  <div class='grid'>
    <div class='card'><div class='k'>현재 시장 국면</div><div class='v'>{current_regime}</div></div>
    <div class='card'><div class='k'>현재 선택 종목</div><div class='v'>{selected_symbol}</div></div>
    <div class='card'><div class='k'>선정 기준/점수</div><div class='v'>{selection_ref} / {selection_score:+.2f}</div></div>
  </div>
  <div class='card'>
    <div class='v'>0) 현재 선정 근거 수치</div>
    <div class='k'>{selection_exact_summary}</div>
    <div class='k'>선정 섹터: {html.escape(selected_sector_summary or '-')}</div>
    <pre>{selection_reason_text if selection_reason_text else '-'}</pre>
    <table>
      <thead><tr><th>순위</th><th>종목</th><th>점수</th><th>20일 수익률</th><th>5일 수익률</th><th>상대강도</th><th>추세</th><th>변동성</th><th>RAM</th><th>TEF</th><th>TQP</th><th>관심도</th><th>스파이크</th><th>RSI</th></tr></thead>
      <tbody>{selection_rank_rows if selection_rank_rows else "<tr><td colspan='14'>랭킹 데이터 없음</td></tr>"}</tbody>
    </table>
    {(f"<div class='k' style='margin-top:6px;color:#e89a56'>Fallback 이유: {fallback_reason_text}</div>" if fallback_reason_text else "")}
  </div>

  <div class='card'>
      <div class='v'>1) 어떻게 종목을 고르고 언제 다시 고르나?</div>
    <div class='k'>운용 흐름</div>
    <ul>
      <li>후보 스캔: 자동 유니버스를 하루 1회 갱신하고 점수 상위 <code>{max(3, int(settings.candidate_refresh_top_n))}</code>개 후보를 운영 준비 대상으로 압축합니다.</li>
      <li>최종 선정: 장 시작 전 하루 1회 오늘의 1순위 운용 종목을 확정하고, 장중 신규 진입 판단은 기본적으로 이 top1 종목에 가장 먼저 집중합니다.</li>
      <li>장중 교체: 같은 날에는 시장 국면이 바뀌어도 종목을 갈아타지 않고, 리스크 강도와 청산 기준만 조정합니다.</li>
          <li>알림: 선정 종목이 바뀌면 Slack으로 변경 종목과 선정 이유를 보내고, 장 시작 직후에는 오프닝 브리프 1회, 정규장 중에는 매시각 요약 리포트를 보냅니다. 리포트에서는 top1과 top2~top3 보조 후보를 구분해 보여줍니다.</li>
    </ul>
    <div class='k'>시장 국면 판정 기준 (리스크 강도 조절용)</div>
    <ul>
      <li>강세: 지수등락률 ≥ <code>+0.70%</code> 그리고 상승비율 ≥ <code>55%</code></li>
      <li>약세: 지수등락률 ≤ <code>-0.70%</code> 그리고 상승비율 ≤ <code>45%</code></li>
      <li>그 외는 중립</li>
    </ul>
    <div class='k'>현재 판정 근거: 지수등락 <code>{regime_idx_pct:+.2f}%</code> / 상승비율 <code>{regime_breadth_pct:.1f}%</code> → <strong>{_display_label(regime_calc, kind="regime")}</strong> ({regime_basis})</div>
    <div class='k'>현재 시장 국면 신뢰도: <code>{round(float(st.get('regime_confidence', 0.0)) * 100.0, 1)}%</code></div>
    <div class='k'>종목 스캔 범위</div>
    <ul>
      <li>현재 소스: <code>{html.escape(universe_source_label)}</code></li>
      <li>{html.escape(universe_scope_text)}</li>
      <li>시뮬레이션/백테스트는 캐시 기준 KRX seed <code>300</code>개에서 거래대금 상위 <code>100</code>종목을 우선 평가합니다.</li>
      <li>{html.escape(universe_seed_text)}. 자동 유니버스가 일시적으로 비면 <code>SYMBOL</code>로만 최소 부트스트랩합니다.</li>
    </ul>
    <div class='k'>선정 방식</div>
    <ul>
      <li>상승 추세 필터: <code>MA5 &gt; MA20 &gt; MA60</code> 그리고 최근 고점/저점 구조가 우상향이어야 함</li>
      <li>거래대금 관심도: 5일 평균 거래대금 / 20일 평균 거래대금 ≥ <code>{settings.trend_min_turnover_ratio_5_to_20:.2f}</code></li>
      <li>거래대금 스파이크: 최근 20일 평균 대비 ≥ <code>{settings.trend_min_value_spike_ratio:.2f}</code></li>
      <li>변동성 필터: ATR14 비율이 <code>{settings.trend_min_atr14_pct:.1f}%</code> ~ <code>{settings.trend_max_atr14_pct:.1f}%</code></li>
      <li>과열 제외: 1일 상승률 ≥ <code>{settings.trend_overheat_day_pct:.1f}%</code> 또는 2일 상승률 ≥ <code>{settings.trend_overheat_2day_pct:.1f}%</code></li>
      <li>추격 과열 감점: 너무 멀리 연장된 종목은 점수에서 감점하고 후순위로 밀립니다.</li>
      <li>top rank 품질 관리: <code>RAM</code>, <code>TEF</code>, <code>TQP</code>를 함께 사용해 top1 품질을 우선 관리합니다. top2~top3는 보조 관찰 후보로 유지합니다.</li>
    </ul>
  </div>

  <div class='card'>
    <div class='v'>2) 어떤 경우에 매수/매도하나?</div>
    <div class='k'>신규 진입 조건</div>
    <ul>
      <li>하루 1회 선정된 종목 안에서만 신규 진입을 검토합니다.</li>
      <li>실전 기준으로는 top1 종목을 우선 진입 대상으로 보고, top2~top3는 보조 감시/대체 후보 성격으로 해석합니다.</li>
      <li>실행 기준은 <code>{settings.bar_interval_minutes}</code>분봉이며, 현재는 봉 마감 시점에만 판단하도록 고정되어 있습니다.</li>
      <li>일봉 추세 필터 통과 + RSI(14) <code>{settings.trend_daily_rsi_min:.1f}</code> ~ <code>{settings.trend_daily_rsi_max:.1f}</code></li>
      <li>기본 진입은 단기 눌림목 회복이며, 추세·구조가 유지되면 제한적 추격 진입도 허용합니다.</li>
      <li>상단 밴드 근처인데 수급 가속이 약한 늦은 추격, 시장 급등일에 종목만 짧게 튄 탄력 추격은 차단합니다.</li>
      <li>갭 필터: 전일 종가 대비 <code>{settings.trend_gap_skip_down_pct:.1f}%</code> 미만 급락은 제외, <code>{settings.trend_gap_skip_up_pct:.1f}%</code> 이상 과도한 갭상승도 제외</li>
      <li>신호 확인은 기본 <code>{settings.signal_confirm_cycles}</code>회이며, 기술 우선/단기 강세 신호는 더 빠르게 진입할 수 있습니다.</li>
      <li>시장 상태 필터가 켜져 있으면 충격장·약세 국면에서는 일반 신규 BUY를 억제하고, 약세 예외 후보만 제한적으로 허용합니다.</li>
    </ul>
    <div class='k'>청산 원칙</div>
    <ul>
      <li>단기 전략은 수익이 빠르게 나면 조기 익절하며, 보유 기간이 길어질수록 수익 보호를 우선합니다.</li>
      <li><code>2</code>거래일 이상 보유했고 수익이 <code>0.8%</code> 이상이면서 상단권이면 조기 정리합니다.</li>
      <li><code>3</code>거래일 이상 보유했고 수익이 <code>0.2%</code> 이상이면 보수적으로 정리합니다.</li>
      <li>종목 손실 캡: 수익률 ≤ <code>{settings.max_symbol_loss_pct:.2f}%</code></li>
      <li>초기 실패형 청산: 진입 직후 1 bar 안에 약 <code>-2.4%</code> 이상 밀리고 추세가 훼손되면 빠르게 정리합니다.</li>
      <li>시장 국면에 따라 ATR 손절/익절/트레일링 강도를 조정합니다.</li>
      <li>ATR 계산 창: <code>{settings.atr_exit_lookback_days}</code>일</li>
    </ul>
    <div class='k'>추가 제한</div>
    <ul>
      <li>일일 손실 한도: <code>{settings.daily_loss_limit_pct:.2f}%</code> 이하 시 당일 신규 BUY 차단 (리스크 홀트)</li>
      <li>시세 stale 보호: 데이터 지연이 <code>{settings.stale_data_max_age_sec}</code>초 초과 시 신규 BUY 차단</li>
      <li>종목별 주문 쿨다운: 최근 체결 후 <code>{settings.trade_cooldown_sec}</code>초 동안 재주문 차단</li>
      <li>손실 청산 직후 같은 종목은 최소 1개 봉 동안 재진입을 막아 반등 착시 재추격을 줄입니다.</li>
      <li>주문 수량: 기본 <code>{settings.position_size}</code> + 변동성 기반 리스크 사이징(<code>{settings.target_risk_per_trade_pct:.2f}%</code>/trade)</li>
      <li>일 최대 주문: <code>{settings.max_daily_orders}</code></li>
    </ul>
    <div class='k'>설정 반영 규칙</div>
    <ul>
      <li>설정 저장 시 봇은 자동 재시작되어 즉시 반영됩니다.</li>
      <li>현재 화면과 런타임은 단일 추세추종 전략 기준으로 동작합니다.</li>
    </ul>
  </div>

  <div class='card'>
    <div class='v'>3) 지금 선택 이유 (실제 이벤트 로그 기준)</div>
    <div class='k'>최근 SELECT 이벤트</div>
    <pre>{html.escape(_display_text(select_event, '아직 SELECT 이벤트가 없습니다.'))}</pre>
    <div class='k'>최근 REGIME_SHIFT</div>
    <pre>{html.escape(_display_text(regime_event, '아직 REGIME_SHIFT 이벤트가 없습니다.'))}</pre>
    <div class='k'>최근 REGIME_CANDIDATE</div>
    <pre>{html.escape(_display_text(regime_candidate_event, '아직 REGIME_CANDIDATE 이벤트가 없습니다.'))}</pre>
    <div class='k'>최근 ROTATE_TARGET</div>
    <pre>{html.escape(_display_text(rotate_event, '아직 ROTATE_TARGET 이벤트가 없습니다.'))}</pre>
    <div class='k'>최근 RISK_EXIT</div>
    <pre>{html.escape(_display_text(risk_event, '아직 RISK_EXIT 이벤트가 없습니다.'))}</pre>
    <div class='k'>최근 RISK_HALT</div>
    <pre>{html.escape(_display_text(risk_halt_event, '아직 RISK_HALT 이벤트가 없습니다.'))}</pre>
    <div class='k'>최근 NO_TRADE_SUMMARY</div>
    <pre>{html.escape(_display_text(no_trade_event, '아직 NO_TRADE_SUMMARY 이벤트가 없습니다.'))}</pre>
    <div class='k'>SELECT 파싱 결과</div>
    <pre>{html.escape(json.dumps(parsed, ensure_ascii=False, indent=2) if parsed else '파싱 가능한 SELECT 이벤트가 없습니다.')}</pre>
  </div>

</div>
</body></html>"""
            self._html(page)
            return

        if path == "/":
            st = controller.status()
            persisted_ui = _load_last_known_ui()
            settings = load_settings()
            ledger_report = _load_ledger_snapshot(str(settings.ledger_path))
            st = _apply_account_snapshot_fallback(st, persisted_ui, ledger_report)
            mk = _service_cached(market_vibe)
            glb = _service_cached(global_market)
            _service_refresh_async(market_vibe)
            _service_refresh_async(global_market)
            name_map = _symbol_name_map()
            if not _has_meaningful_payload(mk):
                mk = persisted_ui.get("market_vibe", {}) if isinstance(persisted_ui.get("market_vibe"), dict) else {}
            if not _has_meaningful_payload(glb):
                glb = persisted_ui.get("global_market", {}) if isinstance(persisted_ui.get("global_market"), dict) else {}
            ana = mk.get("analysis", {})
            src = mk.get("source", {})
            raw = mk.get("raw_snapshot", {})
            stats = mk.get("stats", {})
            hist = mk.get("history", [])
            market_range = (qs.get("market_range", ["1w"])[0] or "1w").strip().lower()
            if market_range not in {"1d", "1w", "1m", "all"}:
                market_range = "1w"
            hist_view = _slice_market_history(hist, market_range)
            event_list = [str(x) for x in st.get("events", [])]
            events = "\n".join(html.escape(x) for x in event_list[-50:])
            events_json = json.dumps(event_list[-200:], ensure_ascii=False)
            select_event = _latest_event(event_list, "SELECT symbols=") or _latest_event(event_list, "SELECT symbol=")
            rotate_event = _latest_event(event_list, "ROTATE_TARGET")
            regime_event = _latest_event(event_list, "REGIME_SHIFT")
            regime_candidate_event = _latest_event(event_list, "REGIME_CANDIDATE")
            risk_event = _latest_event(event_list, "RISK_EXIT")
            risk_halt_event = _latest_event(event_list, "RISK_HALT")
            no_trade_event = _latest_event(event_list, "NO_TRADE_SUMMARY ")
            parsed = _parse_select_event(select_event)
            sectors_up = "<br>".join(html.escape(x) for x in mk.get("top_sectors_up", []))
            sectors_down = "<br>".join(html.escape(x) for x in mk.get("top_sectors_down", []))
            analysis_notes = "<br>".join(
                f"- {html.escape(str(line))}" for line in ana.get("notes", [])
            )
            base_row = raw.get("base_row", {})
            range_day_map = {"1d": 2, "1w": 7, "1m": 30, "all": 180}
            actual_kospi_rows = _fetch_yahoo_index_daily_series("^KS11", months=12)
            actual_kosdaq_rows = _fetch_yahoo_index_daily_series("^KQ11", months=12)
            actual_limit = range_day_map.get(market_range, 7)
            actual_kospi_view = actual_kospi_rows[-actual_limit:] if actual_limit > 0 else actual_kospi_rows
            actual_kosdaq_view = actual_kosdaq_rows[-actual_limit:] if actual_limit > 0 else actual_kosdaq_rows
            idx_level_series = (
                [_to_float(x.get("close")) for x in actual_kospi_view if isinstance(x, dict)]
                if actual_kospi_view
                else [_to_float(x.get("index_value")) for x in hist_view]
            )
            hist_dates = (
                [str(x.get("date") or "").strip() for x in actual_kospi_view if isinstance(x, dict)]
                if actual_kospi_view
                else [str(x.get("updated_at") or "").strip()[:10] for x in hist_view]
            )
            idx_series: list[float] = []
            if idx_level_series:
                prev_close = idx_level_series[0]
                for close in idx_level_series:
                    change_pct = (((close / prev_close) - 1.0) * 100.0) if prev_close > 0 else 0.0
                    idx_series.append(change_pct)
                    prev_close = close
            breadth_series = [_to_float(x.get("breadth_ratio")) for x in hist_view]
            sentiment_series = [_to_float(x.get("sentiment_score")) for x in hist_view]
            history_first = hist_view[0] if hist_view else {}
            history_last = hist_view[-1] if hist_view else {}
            history_delta_index = (
                (_to_float(idx_level_series[-1]) - _to_float(idx_level_series[0]))
                if len(idx_level_series) >= 2
                else _to_float(history_last.get("index_value")) - _to_float(history_first.get("index_value"))
            )
            history_delta_breadth = _to_float(history_last.get("breadth_ratio")) - _to_float(history_first.get("breadth_ratio"))
            history_delta_sentiment = _to_float(history_last.get("sentiment_score")) - _to_float(history_first.get("sentiment_score"))
            ma20 = _sma(idx_level_series, 20)
            ma60 = _sma(idx_level_series, 60)
            rsi14 = _rsi(idx_level_series, 14)
            macd_line, macd_signal, macd_hist = _macd(idx_level_series)
            x_ticks: list[tuple[int, str]] = []
            if hist_dates:
                tick_positions = sorted({0, max(0, len(hist_dates) // 2), max(0, len(hist_dates) - 1)})
                for pos in tick_positions:
                    label = hist_dates[pos][5:] if len(hist_dates[pos]) >= 10 else hist_dates[pos]
                    x_ticks.append((pos, label))
            regime_per_bar = [
                _market_status_from_snapshot(
                    _to_float(row.get("index_change_pct")),
                    _to_float(row.get("breadth_ratio")),
                    _to_float(row.get("sentiment_score")),
                )
                for row in hist_view
            ]
            zone_color_map = {"강세": "#0f5132", "약세": "#5a1b1b", "중립": "#4b5563"}
            zone_spans: list[tuple[int, int, str, float]] = []
            if regime_per_bar:
                start = 0
                current = regime_per_bar[0]
                for idx, regime_name in enumerate(regime_per_bar[1:], start=1):
                    if regime_name != current:
                        zone_spans.append((start, idx - 1, zone_color_map.get(current, "#4b5563"), 0.10))
                        start = idx
                        current = regime_name
                zone_spans.append((start, len(regime_per_bar) - 1, zone_color_map.get(current, "#4b5563"), 0.10))

            kosdaq_map = {str(row.get("date") or ""): _to_float(row.get("close")) for row in actual_kosdaq_view if isinstance(row, dict)}
            kospi_norm: list[float] = []
            kosdaq_norm: list[float | None] = []
            base_kospi = idx_level_series[0] if idx_level_series else 0.0
            base_kosdaq = next((kosdaq_map.get(d) for d in hist_dates if kosdaq_map.get(d)), 0.0)
            for date_text, kospi_close in zip(hist_dates, idx_level_series):
                kospi_norm.append(((kospi_close / base_kospi) - 1.0) * 100.0 if base_kospi > 0 else 0.0)
                kosdaq_close = kosdaq_map.get(date_text)
                if kosdaq_close and base_kosdaq > 0:
                    kosdaq_norm.append(((kosdaq_close / base_kosdaq) - 1.0) * 100.0)
                else:
                    kosdaq_norm.append(None)

            price_ma_chart = _line_overlay_svg(
                [
                    ("Price", "#2563eb", [float(v) for v in idx_level_series]),
                    ("MA20", "#16a34a", ma20),
                    ("MA60", "#f59e0b", ma60),
                ],
                zone_spans=zone_spans,
                x_tick_labels=x_ticks,
                summary=(
                    f"현재 {_to_float(idx_level_series[-1] if idx_level_series else 0):,.2f} | "
                    f"MA20 {_to_float(ma20[-1] if ma20 else 0):,.2f} | "
                    f"MA60 {_to_float(ma60[-1] if ma60 else 0):,.2f}"
                ),
            )
            rsi_chart = _line_overlay_svg(
                [("RSI(14)", "#b45309", rsi14)],
                y_min=0.0,
                y_max=100.0,
                bands=[(70.0, "#ef4444"), (30.0, "#10b981")],
                zone_spans=zone_spans,
                x_tick_labels=x_ticks,
                summary=f"RSI(14) {_to_float(rsi14[-1] if rsi14 else 0):.2f} | 70 과열 / 30 과매도",
            )
            macd_chart = _line_overlay_svg(
                [
                    ("MACD", "#1d4ed8", [float(v) for v in macd_line]),
                    ("Signal(9)", "#7c3aed", [float(v) for v in macd_signal]),
                    ("Hist", "#059669", [float(v) for v in macd_hist]),
                ],
                zone_spans=zone_spans,
                x_tick_labels=x_ticks,
                summary=(
                    f"MACD {_to_float(macd_line[-1] if macd_line else 0):+.3f} | "
                    f"Signal {_to_float(macd_signal[-1] if macd_signal else 0):+.3f} | "
                    f"Hist {_to_float(macd_hist[-1] if macd_hist else 0):+.3f}"
                ),
            )
            breadth_chart = _sparkline_svg(breadth_series, color="#0a7a5a", unit="%")
            market_compare_chart = _line_overlay_svg(
                [
                    ("KOSPI", "#60a5fa", [float(v) for v in kospi_norm]),
                    ("KOSDAQ", "#f59e0b", kosdaq_norm),
                ],
                y_min=min([v for v in kospi_norm + [x for x in kosdaq_norm if x is not None]] + [0.0]) - 1.0 if (kospi_norm or kosdaq_norm) else -1.0,
                y_max=max([v for v in kospi_norm + [x for x in kosdaq_norm if x is not None]] + [0.0]) + 1.0 if (kospi_norm or kosdaq_norm) else 1.0,
                zone_spans=zone_spans,
                x_tick_labels=x_ticks,
                summary=(
                    f"범위 내 누적 변화 | "
                    f"KOSPI {(_to_float(kospi_norm[-1] if kospi_norm else 0)):+.2f}% | "
                    f"KOSDAQ {(_to_float(kosdaq_norm[-1] if kosdaq_norm and kosdaq_norm[-1] is not None else 0)):+.2f}%"
                ),
            )
            market_history_rows = "".join(
                "<tr>"
                f"<td>{html.escape(str(x.get('updated_at') or '-'))}</td>"
                f"<td>{_to_float(x.get('index_value')):,.2f}</td>"
                f"<td>{_to_float(x.get('index_change_pct')):+.2f}%</td>"
                f"<td>{_to_float(x.get('breadth_ratio')):.1f}%</td>"
                f"<td>{_to_float(x.get('sentiment_score')):.1f}</td>"
                f"<td>{_market_status_from_snapshot(_to_float(x.get('index_change_pct')), _to_float(x.get('breadth_ratio')), _to_float(x.get('sentiment_score')))}</td>"
                "</tr>"
                for x in list(hist_view)[-12:]
                if isinstance(x, dict)
            )
            market_range_tabs = "".join(
                (
                    f"<a class='range-tab{' active' if market_range == key else ''}' href='/?market_range={key}'>"
                    f"{label}</a>"
                )
                for key, label in [("1d", "1일"), ("1w", "1주"), ("1m", "1개월"), ("all", "전체")]
            )
            market_range_summary = (
                f"{html.escape({'1d':'1일','1w':'1주','1m':'1개월','all':'전체'}.get(market_range, '1주'))} 변화 | "
                f"지수 {history_delta_index:+,.2f} | "
                f"상승비율 {history_delta_breadth:+.1f}%p | "
                f"심리점수 {history_delta_sentiment:+.1f}"
            )
            vol_window = idx_series[-12:] if len(idx_series) >= 3 else idx_series
            intraday_vol = statistics.pstdev(vol_window) if len(vol_window) >= 2 else 0.0
            quote_map = {str(q.get("label")): q for q in glb.get("quotes", [])}
            ordered_labels = ["KOSDAQ", "S&P 500", "NASDAQ", "USD/KRW", "GOLD", "COPPER", "WTI"]
            global_chips = ""
            for label in ordered_labels:
                q = quote_map.get(label)
                if not q:
                    continue
                pct = _to_float(q.get("change_pct"))
                color = "#16c47f" if pct >= 0 else "#ff6b6b"
                global_chips += (
                    "<div class='chip'>"
                    f"{html.escape(label)} "
                    f"<strong>{q.get('value')}</strong> "
                    f"<span style='color:{color}'>{pct:+.2f}%</span>"
                    "</div>"
                )
            api_health = "OK"
            api_chip_class = "ok"
            if st.get("last_error"):
                api_health = "ERROR"
                api_chip_class = "bad"
            elif mk.get("last_error") or glb.get("last_error"):
                api_health = "DEGRADED"
                api_chip_class = "warn"
            freshness_sec = _to_float(st.get("data_freshness_sec"))
            freshness_class = "bad" if freshness_sec > 120 else ("warn" if freshness_sec > 60 else "ok")
            risk_class = "bad" if st.get("risk_halt_active") else "ok"
            stale_class = "bad" if st.get("stale_data_active") else "ok"
            index_change_pct_display = _display_text(mk.get("index_change_pct"), "집계 전")
            trade_mode_display = _display_label(settings.trade_mode, kind="mode")
            live_armed_display = _display_label("ON" if settings.live_armed else "OFF", kind="bool")
            thread_alive_display = _display_label("UP" if st.get("thread_alive") else "DOWN", kind="bool")
            stale_display = _display_label("ON" if st.get("stale_data_active") else "OFF", kind="bool")
            risk_halt_display = _display_label("ON" if st.get("risk_halt_active") else "OFF", kind="bool")
            rising_display = _display_text(mk.get("rising"), "집계 중")
            falling_display = _display_text(mk.get("falling"), "집계 중")
            def _g(label: str) -> float:
                q = quote_map.get(label)
                return _to_float(q.get("change_pct")) if q else 0.0

            risk_on_score = (
                _g("S&P 500")
                + _g("NASDAQ")
                + _g("COPPER")
                + _g("WTI")
                - _g("USD/KRW")
                - _g("GOLD")
            ) / 6.0
            if risk_on_score >= 0.2:
                risk_on_label = "RISK-ON"
            elif risk_on_score <= -0.2:
                risk_on_label = "RISK-OFF"
            else:
                risk_on_label = "NEUTRAL"
            risk_on_label_display = _display_label(risk_on_label, kind="risk_on")
            topbar_items = (
                "<div class='chip'><strong>시장 데이터</strong></div>"
                f"<div class='chip'><strong>{html.escape(_display_text(mk.get('index_name'), '시장 지수'))}</strong> {_display_text(mk.get('index_value'), '수집 중')}</div>"
                f"<div class='chip'>등락 <strong>{index_change_pct_display}{'' if index_change_pct_display == '집계 전' else '%'}</strong></div>"
                f"{global_chips}"
                f"<div class='chip'>상승/하락 <strong>{rising_display}/{falling_display}</strong></div>"
                f"<div class='chip'>변동성 <strong>{intraday_vol:.2f}%</strong></div>"
                f"<div class='chip'>리스크온 <strong>{risk_on_label_display} ({risk_on_score:+.2f})</strong></div>"
                "<div class='chip'><strong>시스템 상태</strong></div>"
                f"<div class='chip {'bad' if settings.trade_mode == 'LIVE' else 'ok'}'>거래 모드 <strong>{trade_mode_display}</strong></div>"
                f"<div class='chip {'warn' if settings.trade_mode == 'LIVE' and not settings.live_armed else 'ok'}'>실거래 승인 <strong>{live_armed_display}</strong></div>"
                f"<div class='chip {api_chip_class}'>API 상태 <strong>{api_health}</strong></div>"
                f"<div class='chip {'ok' if st.get('thread_alive') else 'bad'}'>실행 스레드 <strong>{thread_alive_display}</strong></div>"
                f"<div class='chip {stale_class}'>시세 지연 <strong>{stale_display}</strong></div>"
                f"<div class='chip {risk_class}'>리스크 홀트 <strong>{risk_halt_display}</strong></div>"
                f"<div class='chip {freshness_class}'>데이터 지연 <strong>{freshness_sec:.1f}s</strong></div>"
                "<div class='chip'><strong>세션</strong></div>"
                "<div class='chip' id='sessionModeChip'>운영 모드 <strong>-</strong></div>"
                "<div class='chip' id='sessionProfileChip'>전략 <strong>-</strong></div>"
                "<div class='chip' id='sessionDiagChip'>최근 진단 <strong>-</strong></div>"
                "<div class='chip' id='sessionChecklistChip'>체크리스트 남음 <strong>-</strong></div>"
                f"<div class='chip'>업데이트 <strong>{html.escape(str(mk.get('updated_at')))}</strong></div>"
            )
            topbar_items_clone = (
                topbar_items
                .replace(" id='sessionModeChip'", "")
                .replace(" id='sessionProfileChip'", "")
                .replace(" id='sessionDiagChip'", "")
                .replace(" id='sessionChecklistChip'", "")
            )
            # Risk-on score: SPX/NASDAQ/COPPER/WTI up, USDKRW/GOLD down is risk-on.
            recent_rows = "".join(
                f"<tr><td>{html.escape(str(x.get('updated_at')))}</td>"
                f"<td>{_to_float(x.get('index_change_pct')):+.2f}%</td>"
                f"<td>{_to_float(x.get('breadth_ratio')):.1f}%</td>"
                f"<td>{_to_float(x.get('sentiment_score')):.1f}</td>"
                f"<td>{int(_to_float(x.get('rising')))}</td>"
                f"<td>{int(_to_float(x.get('falling')))}</td></tr>"
                for x in hist[-8:]
            )
            current_regime_raw = str(st.get("market_regime") or mk.get("analysis", {}).get("regime", "UNKNOWN"))
            current_regime = html.escape(_display_label(current_regime_raw, kind="regime"))
            trade_mode_display = _display_label(settings.trade_mode, kind="mode")
            trade_mode_state_display = _display_label(st.get("trade_mode") or settings.trade_mode, kind="mode")
            live_armed_display = _display_label("ON" if settings.live_armed else "OFF", kind="bool")
            live_armed_state_display = _display_label("ON" if st.get("live_armed") else "OFF", kind="bool")
            thread_alive_display = _display_label("UP" if st.get("thread_alive") else "DOWN", kind="bool")
            stale_display = _display_label("ON" if st.get("stale_data_active") else "OFF", kind="bool")
            risk_halt_display = _display_label("ON" if st.get("risk_halt_active") else "OFF", kind="bool")
            running_display = _display_label("UP" if st.get("running") else "DOWN", kind="bool")
            breadth_ratio_display = _display_text(mk.get("breadth_ratio"), "집계 중")
            rising_display = _display_text(mk.get("rising"), "집계 중")
            falling_display = _display_text(mk.get("falling"), "집계 중")
            sentiment_display = _display_text(ana.get("sentiment_score"), "집계 중")
            cash_equity_display = _display_money_pair(st.get("cash_balance"), st.get("equity"))
            total_pnl_num = _to_float(st.get("total_pnl"))
            total_return_num = _to_float(st.get("total_return_pct"))
            total_pnl_display = (
                "손익 집계 전"
                if abs(total_pnl_num) < 1e-12 and abs(total_return_num) < 1e-12
                else f"{total_pnl_num:,.1f} ({total_return_num:+.2f}%)"
            )
            portfolio_heat_display = (
                "집계 전"
                if abs(_to_float(st.get("portfolio_heat_pct"))) < 1e-12 and abs(_to_float(st.get("max_portfolio_heat_pct"))) < 1e-12
                else f"{_to_float(st.get('portfolio_heat_pct')):.2f}% / {_to_float(st.get('max_portfolio_heat_pct')):.2f}%"
            )
            max_symbol_loss_display = _display_number(st.get("max_symbol_loss_pct"), "집계 전", 2)
            uptime_display = "가동 직후" if _to_int(st.get("uptime_sec")) <= 0 else f"{_to_int(st.get('uptime_sec'))}s"
            order_uptime_display = (
                "주문 없음 / 가동 직후"
                if _to_int(st.get("order_count")) <= 0 and _to_int(st.get("uptime_sec")) <= 0
                else f"{_to_int(st.get('order_count'))} / {uptime_display}"
            )
            token_expires_display = _display_text(st.get("token_expires"), "토큰 정보 수집 중")
            no_trade_summary_text = html.escape(
                _display_text(st.get("no_trade_summary") or no_trade_event, "무거래 요약 없음")
            )
            selected_symbol_code = str(st.get("selected_symbol") or settings.symbol)
            selected_symbol = html.escape(_display_text(_symbol_label(selected_symbol_code, name_map), "미선정"))
            monitored_symbols_raw = str(st.get("monitored_symbols") or "")
            restored_monitored_symbols = _restore_monitored_symbols(
                monitored_symbols_raw,
                selection_detail=st.get("selection_detail"),
                fallback_rows=st.get("factor_snapshot"),
                target_count=settings.trend_select_count,
            )
            if restored_monitored_symbols:
                monitored_symbols_raw = ",".join(restored_monitored_symbols)
                st["monitored_symbols"] = monitored_symbols_raw
            monitored_symbol_labels = [_symbol_label(x, name_map) for x in restored_monitored_symbols]
            monitored_symbol_set = set(restored_monitored_symbols)
            monitored_symbols_display = html.escape(", ".join(monitored_symbol_labels))
            monitored_symbols_chips = "".join(
                f"<span class='chip' style='font-size:13px'>{html.escape(x)}</span>"
                for x in monitored_symbol_labels
            )
            monitored_symbol_count = len(restored_monitored_symbols)
            positions_summary_display = html.escape(
                _format_positions_summary(str(st.get("positions_summary") or ""), name_map)
            )
            selection_ref = html.escape(_display_text(st.get("strategy_reference"), "기준 산출 전"))
            selection_score = _to_float(st.get("selection_score"))
            selection_reason_text = html.escape(_display_text(st.get("selection_reason"), "선정 사유 집계 전"))
            selection_detail = st.get("selection_detail") if isinstance(st.get("selection_detail"), dict) else {}
            today_top_candidate_cards = ""
            selection_history_stats = st.get("selection_history_stats") if isinstance(st.get("selection_history_stats"), list) else []
            selection_turnover_pct = _to_float(st.get("selection_turnover_pct"))
            selection_turnover_note = html.escape(_display_text(st.get("selection_turnover_note"), "교체율 집계 전"))
            if not selection_history_stats:
                file_stats, file_turnover_pct, file_turnover_note = _selection_history_stats_from_file(
                    getattr(settings, "selection_history_path", "data/selection_history.json"),
                    restored_monitored_symbols,
                )
                if file_stats:
                    selection_history_stats = file_stats
                    selection_turnover_pct = file_turnover_pct
                    selection_turnover_note = html.escape(file_turnover_note)
            if not selection_history_stats and restored_monitored_symbols:
                synthetic_day = str(st.get("daily_selection_day") or time.strftime("%Y-%m-%d"))
                selection_history_stats = [
                    {
                        "symbol": sym,
                        "selected_count": 1,
                        "current_streak_days": 1,
                        "last_selected_day": synthetic_day,
                    }
                    for sym in restored_monitored_symbols[: max(1, int(settings.trend_select_count))]
                ]
                if selection_turnover_pct <= 0:
                    selection_turnover_note = html.escape("선정 이력 수집을 시작했습니다. 전일 비교는 다음 거래일부터 가능합니다.")
            if (not str(st.get("no_trade_summary") or "").strip()) and isinstance(st.get("reason_histogram"), dict):
                blocker_items = list((st.get("reason_histogram") or {}).items())[:3]
                if blocker_items:
                    no_trade_summary_text = html.escape(
                        "현재까지 무체결 | 상위 차단 "
                        + ", ".join(f"{k}:{v}" for k, v in blocker_items)
                    )
            selected_factor = (
                selection_detail.get("selected_factor")
                if isinstance(selection_detail.get("selected_factor"), dict)
                else {}
            )
            selected_sector_summary = ", ".join(
                f"{_symbol_label(str(x.get('symbol') or ''), name_map)}:{str(x.get('sector') or 'UNMAPPED')}"
                for x in list(selection_detail.get("selected_sectors", []))[:5]
                if isinstance(x, dict)
            )
            selection_exact_summary = html.escape(
                (
                    f"rank {int(_to_float(selection_detail.get('selected_rank')))}"
                    f"/{max(1, int(_to_float(selection_detail.get('ranked_size'))))} | "
                    f"score {float(selection_detail.get('selected_score', 0.0)):+.2f} | "
                    f"M {float(selected_factor.get('momentum_pct', 0.0)):+.2f}% | "
                    f"5D {float(selected_factor.get('ret5_pct', 0.0)):+.2f}% | "
                    f"상대강도 {float(selected_factor.get('relative_pct', 0.0)):+.2f}% | "
                    f"추세 {float(selected_factor.get('trend_pct', 0.0)):+.2f}% | "
                    f"변동성 {float(selected_factor.get('volatility_pct', 0.0)):.2f}% | "
                    f"ATR {float(selected_factor.get('atr14_pct', 0.0)):.2f}% | "
                    f"RAM {float(selected_factor.get('risk_adjusted_momentum', 0.0)):.2f} | "
                    f"TEF {float(selected_factor.get('trend_efficiency', 0.0)):.2f} | "
                    f"RSI {float(selected_factor.get('daily_rsi', 0.0)):.1f} | "
                    f"ATTN {float(selected_factor.get('attention_ratio', 0.0)):.2f} | "
                    f"SPIKE {float(selected_factor.get('value_spike_ratio', 0.0)):.2f}"
                )
            )
            focus_has_selection = bool(selection_detail) or bool(monitored_symbol_set)
            selected_focus_metrics = (
                "".join(
                    [
                        f"<div class='rank-metric'>선정 점수 <strong>{selection_score:+.2f}</strong></div>",
                        f"<div class='rank-metric'>20일 수익률 <strong>{float(selected_factor.get('momentum_pct', 0.0)):+.2f}%</strong></div>",
                        f"<div class='rank-metric'>5일 수익률 <strong>{float(selected_factor.get('ret5_pct', 0.0)):+.2f}%</strong></div>",
                        f"<div class='rank-metric'>상대강도 <strong>{float(selected_factor.get('relative_pct', 0.0)):+.2f}%</strong></div>",
                        f"<div class='rank-metric'>추세 <strong>{float(selected_factor.get('trend_pct', 0.0)):+.2f}%</strong></div>",
                        f"<div class='rank-metric'>RAM <strong>{float(selected_factor.get('risk_adjusted_momentum', 0.0)):.2f}</strong></div>",
                        f"<div class='rank-metric'>TEF <strong>{float(selected_factor.get('trend_efficiency', 0.0)):.2f}</strong></div>",
                        f"<div class='rank-metric'>RSI <strong>{float(selected_factor.get('daily_rsi', 0.0)):.1f}</strong></div>",
                        f"<div class='rank-metric'>관심도 <strong>{float(selected_factor.get('attention_ratio', 0.0)):.2f}</strong></div>",
                        f"<div class='rank-metric'>스파이크 <strong>{float(selected_factor.get('value_spike_ratio', 0.0)):.2f}</strong></div>",
                    ]
                )
                if focus_has_selection
                else "<div class='k'>핵심 지표 집계 중입니다.</div>"
            )
            selected_focus_badges = (
                _selection_reason_badges_html(
                    {
                        "symbol": selected_symbol_code,
                        "rank": selection_detail.get("selected_rank"),
                        "momentum_pct": selected_factor.get("momentum_pct"),
                        "ret5_pct": selected_factor.get("ret5_pct"),
                        "attention_ratio": selected_factor.get("attention_ratio"),
                        "value_spike_ratio": selected_factor.get("value_spike_ratio"),
                        "daily_rsi": selected_factor.get("daily_rsi"),
                    },
                    selected_symbols=monitored_symbol_set,
                    selected_limit=settings.trend_select_count,
                )
                if focus_has_selection
                else ""
            )
            selection_rank_rows = "".join(
                "<tr>"
                f"<td>{int(_to_float(x.get('rank')))}</td>"
                f"<td>{html.escape(_symbol_label(str(x.get('symbol') or ''), name_map))}</td>"
                f"<td>{_to_float(x.get('score')):+.2f}</td>"
                f"<td>{_to_float(x.get('momentum_pct')):+.2f}%</td>"
                f"<td>{_to_float(x.get('ret5_pct')):+.2f}%</td>"
                f"<td>{_to_float(x.get('relative_pct')):+.2f}%</td>"
                f"<td>{_to_float(x.get('trend_pct')):+.2f}%</td>"
                f"<td>{_to_float(x.get('volatility_pct')):.2f}%</td>"
                f"<td>{_to_float(x.get('risk_adjusted_momentum')):.2f}</td>"
                f"<td>{_to_float(x.get('trend_efficiency')):.2f}</td>"
                f"<td>{_to_float(x.get('attention_ratio')):.2f}</td>"
                f"<td>{_to_float(x.get('value_spike_ratio')):.2f}</td>"
                f"<td>{_to_float(x.get('daily_rsi')):.1f}</td>"
                "</tr>"
                for x in list(selection_detail.get("top_ranked", []))[:5]
                if isinstance(x, dict)
            )
            selection_rank_cards = "".join(
                (
                    "<div class='rank-card'>"
                    "<div class='rank-card-top'>"
                    "<div>"
                    f"<div class='rank-badge'>상위 후보 #{int(_to_float(x.get('rank')))}</div>"
                    f"<div class='v' style='font-size:18px;margin-top:8px'>{html.escape(_symbol_label(str(x.get('symbol') or ''), name_map))}</div>"
                    f"<div class='k' style='margin-top:4px'>RSI {_to_float(x.get('daily_rsi')):.1f} · 관심도 {_to_float(x.get('attention_ratio')):.2f} · 스파이크 {_to_float(x.get('value_spike_ratio')):.2f}</div>"
                    "</div>"
                    f"<div class='rank-score'>{_to_float(x.get('score')):+.2f}</div>"
                    "</div>"
                    f"<div class='reason-badges'>{_selection_reason_badges_html(x, selected_symbols=monitored_symbol_set, selected_limit=settings.trend_select_count)}</div>"
                    "<div class='rank-meta'>"
                    f"<div class='rank-metric'>20일 수익률 <strong>{_to_float(x.get('momentum_pct')):+.2f}%</strong></div>"
                    f"<div class='rank-metric'>5일 수익률 <strong>{_to_float(x.get('ret5_pct')):+.2f}%</strong></div>"
                    f"<div class='rank-metric'>상대강도 <strong>{_to_float(x.get('relative_pct')):+.2f}%</strong></div>"
                    f"<div class='rank-metric'>추세 <strong>{_to_float(x.get('trend_pct')):+.2f}%</strong></div>"
                    f"<div class='rank-metric'>변동성 <strong>{_to_float(x.get('volatility_pct')):.2f}%</strong></div>"
                    f"<div class='rank-metric'>RAM <strong>{_to_float(x.get('risk_adjusted_momentum')):.2f}</strong></div>"
                    f"<div class='rank-metric'>TEF <strong>{_to_float(x.get('trend_efficiency')):.2f}</strong></div>"
                    "</div>"
                    "</div>"
                )
                for x in list(selection_detail.get("top_ranked", []))[:5]
                if isinstance(x, dict)
            )
            fallback_reason_text = html.escape(_display_text(selection_detail.get("fallback_reason"), ""))
            universe_seed_symbols = selection_universe_symbols(settings)
            universe_seed_count = len(universe_seed_symbols)
            universe_source_label = "KIND 자동 유니버스" if settings.auto_universe_enabled else "수동 유니버스"
            universe_scope_text = (
                f"{universe_source_label} 기준으로 전 종목을 스캔하고, "
                f"하루 한 번 후보 풀을 갱신한 뒤 상위 {max(3, int(settings.candidate_refresh_top_n))}개 후보를 운영 준비 대상으로 반영합니다."
            )
            universe_seed_text = (
                f"현재 수동 seed {universe_seed_count}개"
                if universe_seed_count > 1
                else "수동 seed 없이 자동 유니버스만 사용"
            )
            config_state = (qs.get("config", [""])[0] or "").strip().lower()
            cfg_base_url = html.escape(settings.base_url)
            cfg_account_no = html.escape(settings.account_no)
            cfg_price_path = html.escape(settings.price_path)
            cfg_order_path = html.escape(settings.order_path)
            cfg_slack_webhook = html.escape(settings.slack_webhook_url)
            cfg_slack_keywords = html.escape(settings.slack_event_keywords)
            cfg_trade_cooldown = str(settings.trade_cooldown_sec)
            cfg_stale_age = str(settings.stale_data_max_age_sec)
            cfg_target_risk = str(settings.target_risk_per_trade_pct)
            cfg_daily_loss_limit = str(settings.daily_loss_limit_pct)
            cfg_signal_confirm_cycles = str(settings.signal_confirm_cycles)
            cfg_atr_exit_lookback_days = str(settings.atr_exit_lookback_days)
            cfg_atr_stop_mult = str(settings.atr_stop_mult)
            cfg_atr_take_mult = str(settings.atr_take_mult)
            cfg_atr_trailing_mult = str(settings.atr_trailing_mult)
            cfg_trade_mode = settings.trade_mode
            cfg_live_armed_checked = "checked" if settings.live_armed else ""
            cfg_max_symbol_loss = str(settings.max_symbol_loss_pct)
            cfg_max_portfolio_heat = str(settings.max_portfolio_heat_pct)
            cfg_bar_interval_minutes = str(settings.bar_interval_minutes)
            cfg_decision_on_bar_close_only_checked = "checked" if settings.decision_on_bar_close_only else ""
            cfg_market_status_filter_enabled_checked = "checked" if settings.market_status_filter_enabled else ""
            cfg_enable_bearish_exception_checked = "checked" if settings.enable_bearish_exception else ""
            cfg_max_active_positions = str(settings.max_active_positions)
            cfg_candidate_refresh_top_n = str(settings.candidate_refresh_top_n)
            cfg_candidate_refresh_minutes = str(settings.candidate_refresh_minutes)
            cfg_intraday_reselect_enabled_checked = "checked" if settings.intraday_reselect_enabled else ""
            cfg_intraday_reselect_minutes = str(settings.intraday_reselect_minutes)
            cfg_trend_select_count = str(settings.trend_select_count)
            cfg_trend_min_avg_turnover20_krw = str(int(settings.trend_min_avg_turnover20_krw))
            cfg_trend_turnover_ratio = str(settings.trend_min_turnover_ratio_5_to_20)
            cfg_trend_value_spike_ratio = str(settings.trend_min_value_spike_ratio)
            cfg_trend_breakout_buffer = str(settings.trend_breakout_buffer_pct)
            cfg_trend_min_atr14 = str(settings.trend_min_atr14_pct)
            cfg_trend_max_atr14 = str(settings.trend_max_atr14_pct)
            cfg_trend_overheat_day = str(settings.trend_overheat_day_pct)
            cfg_trend_overheat_2day = str(settings.trend_overheat_2day_pct)
            cfg_trend_daily_rsi_min = str(settings.trend_daily_rsi_min)
            cfg_trend_daily_rsi_max = str(settings.trend_daily_rsi_max)
            cfg_trend_gap_skip_up = str(settings.trend_gap_skip_up_pct)
            cfg_trend_gap_skip_down = str(settings.trend_gap_skip_down_pct)
            cfg_trend_max_chase_from_open = str(settings.trend_max_chase_from_open_pct)
            cfg_trend_max_sector_names = str(settings.trend_max_sector_names)
            cfg_symbol_sector_map = html.escape(settings.symbol_sector_map)
            cfg_sector_auto_map_checked = "checked" if settings.sector_auto_map_enabled else ""
            slack_enabled_checked = "checked" if settings.slack_enabled else ""
            cfg_hourly_market_report_enabled_checked = "checked" if settings.hourly_market_report_enabled else ""
            cfg_compare_warn_win_rate_gap = str(settings.compare_warn_win_rate_gap_pct)
            cfg_compare_warn_pnl_gap = str(settings.compare_warn_pnl_gap_krw)
            cfg_compare_warn_expectancy_gap = str(settings.compare_warn_expectancy_gap_krw)
            cfg_compare_warn_hold_gap = str(settings.compare_warn_hold_gap_days)
            cfg_ios_testflight_url = html.escape(settings.ios_testflight_url)
            cfg_ios_app_store_url = html.escape(settings.ios_app_store_url)
            cfg_ios_manifest_url = html.escape(settings.ios_manifest_url)
            cfg_mobile_server_url = html.escape(settings.mobile_server_url)
            cfg_mobile_server_label = html.escape(settings.mobile_server_label)
            cfg_mobile_app_scheme = html.escape(settings.mobile_app_scheme)
            cfg_web_access_enabled_checked = "checked" if settings.web_access_enabled else ""
            cfg_web_access_key = html.escape(settings.web_access_key)
            cfg_web_trusted_device_days = str(settings.web_trusted_device_days)
            cfg_web_max_trusted_devices = str(settings.web_max_trusted_devices)
            trusted_devices = _cleanup_trusted_web_devices(
                _load_trusted_web_devices(),
                ttl_days=settings.web_trusted_device_days,
                max_devices=settings.web_max_trusted_devices,
            )
            trusted_device_rows = _trusted_device_rows_html(trusted_devices)
            install_testflight_url_js = json.dumps(settings.ios_testflight_url)
            install_app_store_url_js = json.dumps(settings.ios_app_store_url)
            install_manifest_url_js = json.dumps(settings.ios_manifest_url)
            install_mobile_server_url_js = json.dumps(settings.mobile_server_url)
            install_mobile_server_label_js = json.dumps(settings.mobile_server_label or "AITRADER Server")
            install_mobile_app_scheme_js = json.dumps(settings.mobile_app_scheme or "aitrader")
            diag_state = (qs.get("diag", [""])[0] or "").strip().lower()
            mode_state = (qs.get("mode", [""])[0] or "").strip().lower()
            simulation_state = (qs.get("simulation", [""])[0] or "").strip().lower()
            simulation_reason = (qs.get("reason", [""])[0] or "").strip()
            diag = diagnostics.get()
            if not _has_meaningful_payload(diag):
                diag = persisted_ui.get("diagnostics", {}) if isinstance(persisted_ui.get("diagnostics"), dict) else {}
            sim_catalog = _simulation_report_catalog()
            sim_prefs = _load_simulation_run_config()
            selected_sim_profile = str(
                (qs.get("sim_profile", [""])[0] or sim_prefs.get("profile") or "daily_selection")
            ).strip().lower()
            if selected_sim_profile not in sim_catalog:
                selected_sim_profile = "daily_selection"
            result_sim_profile = _latest_simulation_result_profile(sim_catalog, sim_prefs)
            sim_profile_meta = dict(sim_catalog.get(result_sim_profile) or {})
            sim_profile_prefs = _simulation_profile_prefs(sim_prefs, selected_sim_profile)
            sim_result_profile_prefs = _simulation_profile_prefs(sim_prefs, result_sim_profile)
            sim_report = _load_json_report(str(sim_profile_meta.get("report_path") or "data/short_term_trade_report_top100.json"))
            selected_intraday_summary = _load_selected_intraday_price_summary()
            selected_intraday_chart = {"available": False, "chart": "", "summary": ""}
            selected_intraday_day_options = _load_selected_intraday_day_options()
            sim_form_window_days = str(int(_to_float(sim_profile_prefs.get("window_days") or 20) or 20))
            sim_form_seed_n = str(int(_to_float(sim_profile_prefs.get("seed_n") or 2000) or 2000))
            sim_form_top_n = str(int(_to_float(sim_profile_prefs.get("top_n") or 800) or 800))
            sim_form_data_fetch_limit = str(int(_to_float(sim_profile_prefs.get("data_fetch_limit") or 260) or 260))
            sim_form_rank_weights = str(sim_profile_prefs.get("rank_weights") or "0.5,0.3,0.2")
            sim_form_max_hold_days = str(int(_to_float(sim_profile_prefs.get("max_hold_days") or 2) or 2))
            sim_form_target_day = str(sim_profile_prefs.get("target_day") or "")
            sim_form_relaxed_checked = "checked" if bool(sim_profile_prefs.get("relaxed_selected_entry")) else ""
            sim_form_probe_checked = "checked" if bool(sim_profile_prefs.get("selected_continuation_probe")) else ""
            sim_form_warn_win_gap = str(
                _to_float(sim_profile_prefs.get("compare_warn_win_rate_gap_pct") or settings.compare_warn_win_rate_gap_pct)
            )
            sim_form_warn_pnl_gap = str(
                int(
                    _to_float(sim_profile_prefs.get("compare_warn_pnl_gap_krw") or settings.compare_warn_pnl_gap_krw)
                    or settings.compare_warn_pnl_gap_krw
                )
            )
            sim_form_warn_expectancy_gap = str(
                int(
                    _to_float(
                        sim_profile_prefs.get("compare_warn_expectancy_gap_krw")
                        or settings.compare_warn_expectancy_gap_krw
                    )
                    or settings.compare_warn_expectancy_gap_krw
                )
            )
            sim_form_warn_hold_gap = str(
                _to_float(sim_profile_prefs.get("compare_warn_hold_gap_days") or settings.compare_warn_hold_gap_days)
            )
            sim_profile_pref_map: dict[str, dict[str, object]] = {}
            sim_profile_meta_preview_map: dict[str, dict[str, str]] = {}
            for profile_key in sim_catalog.keys():
                scoped = _simulation_profile_prefs(sim_prefs, profile_key)
                scoped_history = list(scoped.get("history") or []) if isinstance(scoped.get("history"), list) else []
                if not scoped_history and scoped.get("requested_at"):
                    scoped_history = [
                        {
                            "requested_at": str(scoped.get("requested_at") or ""),
                            "window_days": int(_to_float(scoped.get("window_days") or 20) or 20),
                            "data_fetch_limit": int(_to_float(scoped.get("data_fetch_limit") or 260) or 260),
                            "top_n": int(_to_float(scoped.get("top_n") or 800) or 800),
                            "relaxed_selected_entry": bool(scoped.get("relaxed_selected_entry")),
                            "selected_continuation_probe": bool(scoped.get("selected_continuation_probe")),
                        }
                    ]
                sim_profile_pref_map[profile_key] = {
                    "window_days": int(_to_float(scoped.get("window_days") or 20) or 20),
                    "seed_n": int(_to_float(scoped.get("seed_n") or 2000) or 2000),
                    "top_n": int(_to_float(scoped.get("top_n") or 800) or 800),
                    "data_fetch_limit": int(_to_float(scoped.get("data_fetch_limit") or 260) or 260),
                    "max_hold_days": int(_to_float(scoped.get("max_hold_days") or 2) or 2),
                    "target_day": str(scoped.get("target_day") or ""),
                    "rank_weights": str(scoped.get("rank_weights") or "0.5,0.3,0.2"),
                    "compare_warn_win_rate_gap_pct": _to_float(scoped.get("compare_warn_win_rate_gap_pct") or settings.compare_warn_win_rate_gap_pct),
                    "compare_warn_pnl_gap_krw": int(_to_float(scoped.get("compare_warn_pnl_gap_krw") or settings.compare_warn_pnl_gap_krw) or settings.compare_warn_pnl_gap_krw),
                    "compare_warn_expectancy_gap_krw": int(_to_float(scoped.get("compare_warn_expectancy_gap_krw") or settings.compare_warn_expectancy_gap_krw) or settings.compare_warn_expectancy_gap_krw),
                    "compare_warn_hold_gap_days": _to_float(scoped.get("compare_warn_hold_gap_days") or settings.compare_warn_hold_gap_days),
                    "relaxed_selected_entry": bool(scoped.get("relaxed_selected_entry")),
                    "selected_continuation_probe": bool(scoped.get("selected_continuation_probe")),
                    "requested_at": str(scoped.get("requested_at") or ""),
                    "history": scoped_history,
                }
                catalog_row = dict(sim_catalog.get(profile_key) or {})
                strategy_text = str(catalog_row.get("strategy_label") or "")
                sim_profile_meta_preview_map[profile_key] = {
                    "label": str(catalog_row.get("label") or "시뮬레이션"),
                    "description": str(catalog_row.get("description") or ""),
                    "strategy": strategy_text,
                    "strategy_human": _simulation_strategy_human_label(strategy_text),
                    "why": _simulation_profile_why_text(profile_key),
                    "scope": _simulation_profile_scope_text(profile_key),
                    "form_title": _simulation_profile_form_title(profile_key),
                    "form_hint": _simulation_profile_form_hint(profile_key),
                    "report_path": str(catalog_row.get("report_path") or ""),
                    "requested_at": str(scoped.get("requested_at") or ""),
                    "strategy_card_title": (
                        "선별/진입 구조"
                        if profile_key in {"short_term", "daily_selection"}
                        else "검증 구조"
                    ),
                    "experiment_card_title": (
                        "실험 포인트"
                        if profile_key in {"rolling_rank", "short_horizon", "rank_weighted"}
                        else "핵심 결과 포인트"
                    ),
                }
            sim_profile_pref_json = json.dumps(sim_profile_pref_map, ensure_ascii=False)
            sim_profile_meta_preview_json = json.dumps(sim_profile_meta_preview_map, ensure_ascii=False)
            intraday_day_options_html = "".join(
                f"<option value='{html.escape(day)}' {'selected' if sim_form_target_day == day else ''}>{html.escape(day)}</option>"
                for day in selected_intraday_day_options
            )
            ledger_report = _load_ledger_snapshot(str(settings.ledger_path))
            selected_intraday_chart = _load_selected_intraday_replay_chart(ledger_report=ledger_report)
            selected_intraday_symbol_chart_map = _load_selected_intraday_symbol_chart_map(
                ledger_report=ledger_report,
                max_points=max(2, int((8 * 60) / max(1, int(settings.bar_interval_minutes)))),
                bar_interval_minutes=max(1, int(settings.bar_interval_minutes)),
                market_open_hhmm=str(getattr(settings, "regular_session_start", "09:00")),
                market_close_hhmm=str(getattr(settings, "regular_session_end", "15:30")),
            )
            live_trades = _live_transaction_rows(ledger_report)
            today_key = time.strftime("%Y-%m-%d")
            today_trade_summary = _today_trade_summary_from_ledger(ledger_report, today_key)
            today_symbol_labels = [_symbol_label(sym, name_map) for sym in list(today_trade_summary.get("symbols") or [])[:6]]
            today_trade_detail_text = (
                f"체결 {int(today_trade_summary.get('trade_count') or 0)}건 "
                f"(BUY {int(today_trade_summary.get('buy_count') or 0)} / SELL {int(today_trade_summary.get('sell_count') or 0)})"
            )
            today_trade_detail_subtext = (
                f"실현손익 {_to_float(today_trade_summary.get('realized_pnl')):+,.0f} | "
                f"승 {int(today_trade_summary.get('win_count') or 0)} / 패 {int(today_trade_summary.get('loss_count') or 0)}"
            )
            today_trade_symbol_text = ", ".join(today_symbol_labels) if today_symbol_labels else "오늘 체결 종목 없음"
            live_trade_count = len(live_trades)
            live_win_count = sum(1 for row in live_trades if _to_float((row or {}).get("realized_pnl")) > 0)
            live_win_rate = (live_win_count / float(live_trade_count) * 100.0) if live_trade_count > 0 else 0.0
            live_total_pnl = sum(_to_float((row or {}).get("realized_pnl")) for row in live_trades)
            live_hold_days_vals = [
                _to_float(_hold_days_label((row or {}).get("buy_ts"), (row or {}).get("sell_ts")).replace("일", ""))
                for row in live_trades
                if isinstance(row, dict) and _hold_days_label((row or {}).get("buy_ts"), (row or {}).get("sell_ts")) not in {"-", "미상"}
            ]
            live_avg_hold = (sum(live_hold_days_vals) / float(len(live_hold_days_vals))) if live_hold_days_vals else 0.0
            live_stats = _trade_stats(live_trades, pnl_key="realized_pnl", return_key="return_pct")
            a_grade_live_trades = [row for row in live_trades if isinstance(row, dict) and bool(row.get("a_grade_opening"))]
            regular_live_trades = [row for row in live_trades if isinstance(row, dict) and not bool(row.get("a_grade_opening"))]
            a_grade_trade_count = len(a_grade_live_trades)
            a_grade_win_count = sum(1 for row in a_grade_live_trades if _to_float((row or {}).get("realized_pnl")) > 0)
            a_grade_win_rate = (a_grade_win_count / float(a_grade_trade_count) * 100.0) if a_grade_trade_count > 0 else 0.0
            a_grade_total_pnl = sum(_to_float((row or {}).get("realized_pnl")) for row in a_grade_live_trades)
            a_grade_avg_return = (
                sum(_to_float((row or {}).get("return_pct")) for row in a_grade_live_trades) / float(a_grade_trade_count)
                if a_grade_trade_count > 0
                else 0.0
            )
            regular_trade_count = len(regular_live_trades)
            regular_win_count = sum(1 for row in regular_live_trades if _to_float((row or {}).get("realized_pnl")) > 0)
            regular_win_rate = (regular_win_count / float(regular_trade_count) * 100.0) if regular_trade_count > 0 else 0.0
            regular_total_pnl = sum(_to_float((row or {}).get("realized_pnl")) for row in regular_live_trades)
            regular_avg_return = (
                sum(_to_float((row or {}).get("return_pct")) for row in regular_live_trades) / float(regular_trade_count)
                if regular_trade_count > 0
                else 0.0
            )
            opening_review_rows = _load_opening_review_history_rows()
            opening_review_table_rows = "".join(
                "<tr>"
                f"<td>{html.escape(str(row.get('day') or '-'))}</td>"
                f"<td>{html.escape(_display_text(row.get('opening_review'), '-'))}</td>"
                f"<td>{int(_to_float(row.get('trades')))}</td>"
                f"<td>{_to_float(row.get('realized_pnl')):+,.0f}</td>"
                f"<td>{_to_float(row.get('sell_win_rate_pct')):.1f}%</td>"
                "</tr>"
                for row in list(opening_review_rows)[-12:]
                if isinstance(row, dict)
            )
            opening_review_win_series = [_to_float(row.get("sell_win_rate_pct")) for row in opening_review_rows if isinstance(row, dict)]
            opening_review_pnl_series = [_to_float(row.get("realized_pnl")) for row in opening_review_rows if isinstance(row, dict)]
            opening_review_dates = [str(row.get("day") or "")[5:] for row in opening_review_rows if isinstance(row, dict)]
            opening_review_ticks: list[tuple[int, str]] = []
            if opening_review_dates:
                tick_positions = sorted({0, max(0, len(opening_review_dates) // 2), max(0, len(opening_review_dates) - 1)})
                for pos in tick_positions:
                    opening_review_ticks.append((pos, opening_review_dates[pos]))
            opening_review_win_chart = _line_overlay_svg(
                [("승률", "#22c55e", [float(v) for v in opening_review_win_series])],
                y_min=0.0,
                y_max=100.0,
                bands=[(50.0, "#64748b")],
                x_tick_labels=opening_review_ticks,
                summary=(
                    f"최근 오프닝 후보 승률 "
                    f"{_to_float(opening_review_win_series[-1] if opening_review_win_series else 0):.1f}%"
                ),
            )
            opening_review_pnl_chart = _sparkline_svg(
                opening_review_pnl_series,
                color="#60a5fa",
                unit="",
            )
            live_updated_at = html.escape(
                _display_text((live_trades[-1] or {}).get("sell_ts") if live_trades else "", "체결 이력 없음")
            )
            live_equity_history = list(ledger_report.get("equity_history") or []) if isinstance(ledger_report.get("equity_history"), list) else []
            live_equity_series = [
                _to_float(row.get("equity"))
                for row in live_equity_history[-240:]
                if isinstance(row, dict)
            ]
            live_drawdown = _drawdown_series(live_equity_series)
            live_trade_pnl_series = [_to_float((row or {}).get("realized_pnl")) for row in live_trades[-24:]]
            live_trade_return_series = [_to_float((row or {}).get("return_pct")) for row in live_trades[-24:]]
            live_weekday_map: dict[str, list[float]] = {}
            live_top_map: dict[str, float] = {}
            live_hold_map: dict[str, list[float]] = {}
            for row in live_trades:
                symbol = str((row or {}).get("symbol") or "").strip()
                if not symbol:
                    continue
                live_top_map[symbol] = live_top_map.get(symbol, 0.0) + _to_float((row or {}).get("realized_pnl"))
                hold_days = _hold_days_label(row.get("buy_ts"), row.get("sell_ts"))
                hold_key = hold_days if hold_days != "-" else "미상"
                live_hold_map.setdefault(hold_key, []).append(_to_float((row or {}).get("return_pct")))
                live_weekday_map.setdefault(_weekday_label(row.get("sell_ts")), []).append(_to_float((row or {}).get("return_pct")))
            live_top_cards = "".join(
                "<div class='rank-metric'>"
                f"{html.escape(_symbol_label(symbol, name_map))} "
                f"<strong>{pnl:,.0f}</strong>"
                "</div>"
                for symbol, pnl in sorted(live_top_map.items(), key=lambda item: item[1], reverse=True)[:5]
            )
            live_trade_rows = "".join(
                (
                    "<tr class='"
                    + ("sim-win" if _to_float(row.get('realized_pnl')) > 0 else "sim-loss")
                    + "'"
                    + f" data-result=\"{'win' if _to_float(row.get('realized_pnl')) > 0 else 'loss'}\""
                    + f" data-return=\"{_to_float(row.get('return_pct')):.4f}\""
                    + f" data-pnl=\"{_to_float(row.get('realized_pnl')):.4f}\""
                    + f" data-sell-date=\"{html.escape(str(row.get('sell_ts') or ''))}\""
                    + f" data-symbol=\"{html.escape(str(row.get('symbol') or ''))}\""
                    + ">"
                    f"<td><button type='button' class='trade-symbol-btn' data-trade-symbol=\"{html.escape(str(row.get('symbol') or ''))}\">{html.escape(_symbol_label(str(row.get('symbol') or ''), name_map))}</button></td>"
                    f"<td>{html.escape(str(row.get('buy_ts') or '-'))}</td>"
                    f"<td>{html.escape(str(row.get('sell_ts') or '-'))}</td>"
                    f"<td>{_to_float(row.get('buy_price')):,.0f}</td>"
                    f"<td>{_to_float(row.get('sell_price')):,.0f}</td>"
                    f"<td>{int(_to_float(row.get('qty')))}</td>"
                    f"<td>{_to_float(row.get('return_pct')):+.2f}%</td>"
                    f"<td>{_to_float(row.get('realized_pnl')):,.0f}</td>"
                    f"<td>{html.escape(_hold_days_label(row.get('buy_ts'), row.get('sell_ts')))}</td>"
                    f"<td>{html.escape(str(row.get('entry_mode') or '-'))}</td>"
                    "</tr>"
                )
                for row in live_trades[-20:]
                if isinstance(row, dict)
            )
            sim_trades = list(sim_report.get("trades") or []) if isinstance(sim_report.get("trades"), list) else []
            sim_summary_rows = (
                list(sim_report.get("summary_by_symbol") or [])
                if isinstance(sim_report.get("summary_by_symbol"), list)
                else list(sim_report.get("summary_rows") or [])
                if isinstance(sim_report.get("summary_rows"), list)
                else []
            )
            sim_trade_count = len(sim_trades)
            sim_win_count = sum(1 for row in sim_trades if _to_float((row or {}).get("realized_pnl")) > 0)
            sim_win_rate = (sim_win_count / float(sim_trade_count) * 100.0) if sim_trade_count > 0 else 0.0
            sim_total_pnl = sum(_to_float((row or {}).get("realized_pnl")) for row in sim_trades)
            sim_stats = _trade_stats(sim_trades, pnl_key="realized_pnl", return_key="return_pct")
            sim_avg_hold = (
                sum(_to_float((row or {}).get("hold_bars")) for row in sim_trades) / float(sim_trade_count)
                if sim_trade_count > 0
                else 0.0
            )
            sim_trade_pnl_series = [_to_float((row or {}).get("realized_pnl")) for row in sim_trades[-24:]]
            sim_trade_return_series = [_to_float((row or {}).get("return_pct")) for row in sim_trades[-24:]]
            sim_cumulative_pnl = _cumulative_series([_to_float((row or {}).get("realized_pnl")) for row in sim_trades])
            sim_drawdown = _drawdown_series([10000000.0 + x for x in sim_cumulative_pnl]) if sim_cumulative_pnl else []
            sim_updated_at_raw = _ts_text(sim_report.get("updated_at"))
            sim_requested_at_raw = _ts_text(sim_result_profile_prefs.get("requested_at"))
            sim_updated_at = html.escape(_display_text(sim_updated_at_raw, "실행 전"))
            sim_run_status = (
                "방금 실행 완료" if simulation_state == "done"
                else "실행 실패" if simulation_state == "failed"
                else "최근 실행 반영됨" if (sim_updated_at_raw and sim_requested_at_raw and sim_updated_at_raw >= sim_requested_at_raw)
                else "실행 대기 / 이전 결과 표시 중" if sim_requested_at_raw
                else "대기 중"
            )
            sim_status_note = (
                f"요청 {sim_requested_at_raw} → 결과 {sim_updated_at_raw}"
                if sim_requested_at_raw and sim_updated_at_raw
                else f"요청 {sim_requested_at_raw}"
                if sim_requested_at_raw
                else f"결과 {sim_updated_at_raw}"
                if sim_updated_at_raw
                else "아직 실행 기록이 없습니다."
            )
            sim_config = dict(sim_report.get("config") or {}) if isinstance(sim_report.get("config"), dict) else {}
            sim_strategy = dict(sim_config.get("strategy") or {}) if isinstance(sim_config.get("strategy"), dict) else {}
            sim_report_type_label = html.escape(str(sim_profile_meta.get("label") or "시뮬레이션"))
            sim_report_desc = html.escape(str(sim_profile_meta.get("description") or ""))
            sim_strategy_label = html.escape(str(sim_profile_meta.get("strategy_label") or ""))
            sim_intraday_data_text = (
                f"rows {int(selected_intraday_summary.get('rows', 0))} | "
                f"symbols {int(selected_intraday_summary.get('symbols', 0))} | "
                f"days {int(selected_intraday_summary.get('days', 0))} | "
                f"last {str(selected_intraday_summary.get('last_bar_ts') or '-')}"
                if bool(selected_intraday_summary.get("available"))
                else "아직 저장된 2분 선택종목 데이터가 없습니다."
            )
            intraday_selected_data_ready = bool(selected_intraday_summary.get("available"))
            intraday_selected_status_badge = (
                "<span class='delta-badge good'>2분 데이터 준비됨</span>"
                if intraday_selected_data_ready
                else "<span class='delta-badge warn'>2분 데이터 없음</span>"
            )
            intraday_selected_status_note = (
                "실전에서 저장된 선정 종목 2분 데이터가 있어 리플레이 실행과 차트 확인이 가능합니다."
                if intraday_selected_data_ready
                else "아직 장중 저장 전입니다. 장중에 선정 종목이 2분 바 기준으로 누적되면 리플레이 결과와 차트가 채워집니다."
            )
            sim_data_fetch_text = (
                f"히스토리 {int(_to_float(sim_config.get('data_fetch_limit') or sim_prefs.get('data_fetch_limit') or 0))}일 확보"
                if _to_float(sim_config.get("data_fetch_limit") or sim_prefs.get("data_fetch_limit")) > 0
                else "필요한 히스토리 자동 확보"
            )
            sim_target_day_text = str(sim_report.get("target_day") or sim_config.get("target_day") or "").strip()
            sim_period_text = (
                f"특정일 {sim_target_day_text}"
                if sim_target_day_text
                else f"최근 {int(_to_float(sim_report.get('window_days') or sim_config.get('window_days') or 0))}거래일"
                if _to_float(sim_report.get("window_days") or sim_config.get("window_days")) > 0
                else "저장된 2분 선택 종목 리플레이"
                if result_sim_profile == "intraday_replay"
                else f"상위 {int(_to_float(sim_report.get('top_count') or sim_config.get('top_n') or 0))}종목 종목별 시뮬레이션"
                if _to_float(sim_report.get("top_count") or sim_config.get("top_n")) > 0
                else "기본 기간"
            )
            sim_universe_text = (
                f"seed {int(_to_float(sim_report.get('seed_count') or sim_config.get('seed_n') or 0))} | "
                f"top {int(_to_float(sim_report.get('top_count') or sim_config.get('top_n') or 0))}"
                if _to_float(sim_report.get("top_count") or sim_config.get("top_n")) > 0
                else f"seed {int(_to_float(sim_report.get('seed_count') or sim_config.get('seed_n') or 0))}"
            )
            sim_strategy_detail = (
                f"{int(_to_float(sim_strategy.get('bar_interval_minutes') or settings.bar_interval_minutes))}분봉 | "
                f"{'봉마감 판단' if bool(sim_strategy.get('decision_on_bar_close_only', settings.decision_on_bar_close_only)) else '실시간 판단'} | "
                f"{html.escape(str(sim_strategy.get('selection_style') or 'strategy'))}"
            )
            sim_ranking_text = ", ".join(
                str(x) for x in list(sim_strategy.get("ranking_factors") or [])[:5]
            ) or "RAM, TEF, TQP"
            sim_guard_text = ", ".join(
                _simulation_guard_human_label(x) for x in list(sim_strategy.get("risk_guards") or [])[:4]
            ) or "risk guards"
            sim_selection_style_raw = str(sim_strategy.get("selection_style") or "top1_priority_with_watchlist")
            sim_entry_style_raw = str(sim_strategy.get("entry_style") or "short_horizon_trend_follow")
            sim_selection_style_text = html.escape(_simulation_strategy_human_label(sim_selection_style_raw))
            sim_entry_style_text = html.escape(_simulation_strategy_human_label(sim_entry_style_raw))
            sim_selection_style_detail = html.escape(_simulation_strategy_human_detail(sim_selection_style_raw))
            sim_entry_style_detail = html.escape(_simulation_strategy_human_detail(sim_entry_style_raw))
            live_selection_style_text = html.escape("전종목 스캔 + 상위 후보 압축")
            live_selection_style_detail = html.escape(
                f"KIND 자동 유니버스 전종목을 갱신하고, 준비 후보는 상위 {max(3, int(settings.candidate_refresh_top_n))}개로 압축한 뒤 최종 운용 바스켓은 최대 {max(1, int(settings.trend_select_count))}개까지 유지합니다."
                + (
                    f" 현재는 후보와 선정 종목을 약 {max(1, int(getattr(settings, 'intraday_reselect_minutes', settings.candidate_refresh_minutes)))}분마다 다시 평가합니다."
                    if bool(getattr(settings, "intraday_reselect_enabled", False))
                    else " 현재는 선정 종목을 하루 1회 중심으로 확정합니다."
                )
            )
            live_entry_style_text = html.escape("2분봉 종가 기준 추세 진입")
            live_entry_style_detail = html.escape(
                f"현재 실전 진입은 {max(1, int(settings.bar_interval_minutes))}분봉 기준이며, {'봉마감 확인 후' if settings.decision_on_bar_close_only else '장중 실시간으로'} 구조·돌파·추세 품질을 확인한 뒤 진입합니다."
            )
            live_cadence_style_text = html.escape("가격 확인 / 판단 주기")
            live_cadence_style_detail = html.escape(
                f"운용 종목 가격은 장중 계속 갱신하지만, 실제 매수·매도 판단은 기본적으로 {max(1, int(settings.bar_interval_minutes))}분봉이 마감될 때마다 다시 계산합니다. 손실 후 재진입 제한도 최소 1개 bar 이상 유지합니다."
            )
            live_exit_style_text = html.escape("조건형 청산")
            live_exit_style_detail = html.escape(
                "고정 최대 5일 보유가 아니라 손절, 동적 익절, 빠른 실패, 추세 훼손, trailing stop, hold_exit 조건으로 청산합니다. 일반적으로 수익 구간은 2~3거래일 이내에 먼저 정리되는 편입니다."
            )
            live_exec_style_text = html.escape("하루 1회 선정 + 장중 관리")
            live_exec_style_detail = html.escape(
                (
                    f"장중에는 약 {max(1, int(getattr(settings, 'intraday_reselect_minutes', settings.bar_interval_minutes)))}분마다 후보를 다시 평가하고, 선별된 종목과 보유 종목을 함께 관리합니다."
                    if bool(getattr(settings, "intraday_reselect_enabled", False))
                    else "장 시작 전 오늘 바스켓을 잠그고 장중에는 보유 종목 관리와 신규 진입 조건만 점검합니다. 즉, 실전은 무한 재선정보다 선별된 소수 종목을 집중 관리하는 구조입니다."
                )
            )
            live_strategy_compare_text = html.escape(
                "실거래는 실제 체결 제약과 리스크 관리가 반영된 결과이고, 시뮬레이션은 같은 전략 가정이 기대값으로 이어지는지 비교하기 위한 기준선입니다."
            )
            live_decision_activity_detail = html.escape(_display_text(st.get("decision_activity_summary"), "집계 중"))
            live_capacity_style_text = html.escape("현재 운용 폭")
            live_capacity_style_detail = html.escape(
                f"준비 후보 최대 {max(3, int(settings.candidate_refresh_top_n))}개, 실전 감시 바스켓 최대 {max(1, int(settings.trend_select_count))}개, 실제 동시 포지션 최대 {max(1, int(settings.max_active_positions))}개로 운용합니다."
            )
            strategy_compare_selection = html.escape(
                f"실거래는 전종목 스캔 후 최종 바스켓을 최대 {max(1, int(settings.trend_select_count))}개까지 유지하고, 시뮬레이션은 선택 프로필에 따라 top-N 리포트 또는 일일 재선정 포트폴리오로 검증합니다."
                + (
                    f" 현재 실전은 후보군을 약 {max(1, int(getattr(settings, 'intraday_reselect_minutes', settings.candidate_refresh_minutes)))}분마다 다시 평가합니다."
                    if bool(getattr(settings, "intraday_reselect_enabled", False))
                    else ""
                )
            )
            strategy_compare_entry = html.escape(
                f"실거래 진입은 {max(1, int(settings.bar_interval_minutes))}분봉과 봉마감 확인을 중심으로 하고, 시뮬레이션은 같은 추세/품질 가정을 프로필별 룰로 단순화해 기대값을 측정합니다."
            )
            strategy_compare_cadence = html.escape(
                f"실거래는 장중 가격을 계속 갱신하면서도 실제 매매 판단은 기본적으로 {max(1, int(settings.bar_interval_minutes))}분봉 마감마다 다시 계산하고, 시뮬레이션은 선택한 기간/프로필 단위로 묶어서 결과를 계산합니다."
                + (
                    f" 후보 재선정도 약 {max(1, int(getattr(settings, 'intraday_reselect_minutes', settings.candidate_refresh_minutes)))}분마다 다시 시도합니다."
                    if bool(getattr(settings, "intraday_reselect_enabled", False))
                    else ""
                )
            )
            strategy_compare_exit = html.escape(
                "실거래 청산은 손절·동적 익절·fast fail·trailing·hold_exit가 즉시 반영되고, 시뮬레이션은 선택한 프로필의 보유일·회전 규칙으로 결과를 계산합니다."
            )
            strategy_compare_exec = html.escape(
                "즉, 실거래는 장중 리스크 관리가 더 강하고, 시뮬레이션은 전략 가정이 유지됐을 때의 기준 성과를 비교하는 용도입니다."
            )
            sim_why_text = (
                "실전에서는 Top1 품질에 집중하고, 시뮬레이션에서는 같은 전략이 실제 기대값으로 이어지는지 검증합니다."
                if result_sim_profile in {"short_term", "daily_selection"}
                else "후보를 매일 다시 평가해 추세가 이어지는지, 아니면 과열 추격인지 빠르게 가려내기 위한 실험입니다."
            )
            sim_hold_style_text = (
                f"최대 {int(_to_float(sim_config.get('max_hold_days') or sim_result_profile_prefs.get('max_hold_days') or 2))}일 보유"
                if result_sim_profile in {"daily_selection", "short_horizon"}
                else "2분 바 종가 리플레이"
                if result_sim_profile == "intraday_replay"
                else "forward horizon 기준 평가"
                if result_sim_profile in {"rolling_rank", "rank_weighted"}
                else "종목별 시그널 청산"
            )
            if result_sim_profile == "short_term":
                sim_param_text = (
                    f"top {int(_to_float(sim_config.get('top_n') or sim_result_profile_prefs.get('top_n') or 0))} | "
                    f"seed {int(_to_float(sim_config.get('seed_n') or sim_result_profile_prefs.get('seed_n') or 0))} | "
                    f"data {int(_to_float(sim_config.get('data_fetch_limit') or sim_result_profile_prefs.get('data_fetch_limit') or 0))}일"
                )
            else:
                sim_param_text = (
                    f"window {int(_to_float(sim_config.get('window_days') or sim_result_profile_prefs.get('window_days') or 0))}일 | "
                    f"data {int(_to_float(sim_config.get('data_fetch_limit') or sim_result_profile_prefs.get('data_fetch_limit') or 0))}일 | "
                    f"seed {int(_to_float(sim_config.get('seed_n') or sim_result_profile_prefs.get('seed_n') or 0))}"
                )
                if result_sim_profile == "intraday_replay":
                    sim_param_text = (
                        f"{('day ' + sim_target_day_text + ' | ') if sim_target_day_text else ''}"
                        f"window {int(_to_float(sim_config.get('window_days') or sim_result_profile_prefs.get('window_days') or 0))}일 | "
                        f"source data/selected_intraday_prices.json"
                    )
                if _to_float(sim_config.get("top_n") or sim_result_profile_prefs.get("top_n")) > 0:
                    sim_param_text += f" | top {int(_to_float(sim_config.get('top_n') or sim_result_profile_prefs.get('top_n')))}"
                if result_sim_profile == "rank_weighted":
                    sim_param_text += f" | weights {html.escape(str(sim_result_profile_prefs.get('rank_weights') or sim_config.get('rank_weights') or '0.5,0.3,0.2'))}"
            sim_strategy_cards = (
                f"<div class='ops-card'><div class='section-title'>선별 전략</div><div class='v' style='font-size:14px'>{sim_selection_style_text}</div><div class='k'>{sim_selection_style_detail}</div></div>"
                f"<div class='ops-card'><div class='section-title'>진입 전략</div><div class='v' style='font-size:14px'>{sim_entry_style_text}</div><div class='k'>{sim_entry_style_detail}</div></div>"
                f"<div class='ops-card'><div class='section-title'>보유/청산</div><div class='v' style='font-size:14px'>{html.escape(sim_hold_style_text)}</div><div class='k'>시뮬레이션 보유 규칙</div></div>"
                f"<div class='ops-card'><div class='section-title'>{'실행 입력' if result_sim_profile == 'short_term' else '실제 파라미터'}</div><div class='v' style='font-size:13px'>{html.escape(sim_param_text)}</div><div class='k'>{'top / seed / data 기준' if result_sim_profile == 'short_term' else '기간 / 데이터 / 유니버스 설정'}</div></div>"
                f"<div class='ops-card'><div class='section-title'>리스크 가드</div><div class='v' style='font-size:13px'>{html.escape(sim_guard_text)}</div><div class='k'>과열/충격/추격 방지</div></div>"
            )
            win_left_style, win_right_style = _comparison_tone_style(live_win_rate, sim_win_rate, prefer_higher=True)
            pnl_left_style, pnl_right_style = _comparison_tone_style(live_total_pnl, sim_total_pnl, prefer_higher=True)
            exp_left_style, exp_right_style = _comparison_tone_style(live_stats["expectancy"], sim_stats["expectancy"], prefer_higher=True)
            hold_left_style, hold_right_style = _comparison_tone_style(live_avg_hold, sim_avg_hold, prefer_higher=False)
            win_delta = live_win_rate - sim_win_rate
            pnl_delta = live_total_pnl - sim_total_pnl
            exp_delta = live_stats["expectancy"] - sim_stats["expectancy"]
            hold_delta = live_avg_hold - sim_avg_hold
            warn_win_gap = _to_float(
                sim_profile_prefs.get("compare_warn_win_rate_gap_pct")
                or getattr(settings, "compare_warn_win_rate_gap_pct", 20.0)
            )
            warn_pnl_gap = _to_float(
                sim_profile_prefs.get("compare_warn_pnl_gap_krw")
                or getattr(settings, "compare_warn_pnl_gap_krw", 100000.0)
            )
            warn_expectancy_gap = _to_float(
                sim_profile_prefs.get("compare_warn_expectancy_gap_krw")
                or getattr(settings, "compare_warn_expectancy_gap_krw", 10000.0)
            )
            warn_hold_gap = _to_float(
                sim_profile_prefs.get("compare_warn_hold_gap_days")
                or getattr(settings, "compare_warn_hold_gap_days", 1.0)
            )
            win_badge = "<span class='delta-badge warn'>차이 큼</span>" if abs(win_delta) >= warn_win_gap else "<span class='delta-badge good'>유사</span>"
            pnl_badge = "<span class='delta-badge warn'>차이 큼</span>" if abs(pnl_delta) >= warn_pnl_gap else "<span class='delta-badge good'>유사</span>"
            exp_badge = "<span class='delta-badge warn'>차이 큼</span>" if abs(exp_delta) >= warn_expectancy_gap else "<span class='delta-badge good'>유사</span>"
            hold_badge = "<span class='delta-badge warn'>차이 큼</span>" if abs(hold_delta) >= warn_hold_gap else "<span class='delta-badge good'>유사</span>"
            delta_alerts: list[str] = []
            if abs(win_delta) >= warn_win_gap:
                delta_alerts.append(f"승률 차이 {win_delta:+.1f}%p")
            if abs(pnl_delta) >= warn_pnl_gap:
                delta_alerts.append(f"누적손익 차이 {pnl_delta:+,.0f}")
            if abs(exp_delta) >= warn_expectancy_gap:
                delta_alerts.append(f"Expectancy 차이 {exp_delta:+,.0f}")
            if abs(hold_delta) >= warn_hold_gap:
                delta_alerts.append(f"평균 보유 차이 {hold_delta:+.1f}일")
            if live_trade_count <= 0 and sim_trade_count <= 0:
                live_vs_sim_comment = "실거래와 시뮬레이션 모두 거래가 거의 없어 아직 비교 해석이 어렵습니다."
                live_vs_sim_action = "추천 액션: 데이터 기간을 늘리고, 시뮬레이션 거래가 실제로 발생하는 조건인지 먼저 점검하세요."
                primary_action_label = "시뮬레이션 설정 보기"
                primary_action_target = "performance-section"
                primary_action_tab = "performance"
                secondary_action_label = "시장 컨텍스트 보기"
                secondary_action_target = "market-section"
                secondary_action_tab = "stocks"
                secondary_action_stocks = "market"
            elif live_trade_count > 0 and sim_trade_count <= 0:
                live_vs_sim_comment = "실거래 체결은 있는데 현재 시뮬레이션 리포트에는 거래가 없어, 시뮬레이션 조건이 너무 보수적이거나 최근 리포트가 비어 있을 가능성이 큽니다."
                live_vs_sim_action = "추천 액션: 시뮬레이션 profile/기간/데이터 기간을 확인하고, daily selection 또는 short horizon 결과가 비어 있는지 먼저 점검하세요."
                primary_action_label = "시뮬레이션 설정 보기"
                primary_action_target = "performance-section"
                primary_action_tab = "performance"
                secondary_action_label = "차단 사유 보기"
                secondary_action_target = "block-section"
                secondary_action_tab = "stocks"
                secondary_action_stocks = "blocks"
            elif live_trade_count <= 0 and sim_trade_count > 0:
                live_vs_sim_comment = "시뮬레이션은 거래가 나오지만 실거래는 비어 있어, 실전 진입 게이트나 장중 조건이 더 엄격할 가능성이 큽니다."
                live_vs_sim_action = "추천 액션: 장중 차단 사유와 실전 entry gate를 확인하고, 시뮬레이션보다 실거래가 어디서 더 막히는지 비교하세요."
                primary_action_label = "차단 사유 보기"
                primary_action_target = "block-section"
                primary_action_tab = "stocks"
                primary_action_stocks = "blocks"
                secondary_action_label = "실행 보드 보기"
                secondary_action_target = "board-section"
                secondary_action_tab = "stocks"
                secondary_action_stocks = "board"
            elif live_total_pnl > sim_total_pnl and live_win_rate >= sim_win_rate:
                live_vs_sim_comment = "현재는 실거래가 시뮬레이션보다 더 강합니다. 실전 필터가 오히려 잡음을 더 잘 걸러내고 있을 가능성이 큽니다."
                live_vs_sim_action = "추천 액션: 지금 실전 필터를 유지하고, 시뮬레이션 쪽 데이터 기간 또는 보유 규칙을 실전 기준에 더 가깝게 맞춰보세요."
                primary_action_label = "시뮬레이션 설정 보기"
                primary_action_target = "performance-section"
                primary_action_tab = "performance"
                secondary_action_label = "실행 보드 보기"
                secondary_action_target = "board-section"
                secondary_action_tab = "stocks"
                secondary_action_stocks = "board"
            elif sim_total_pnl > live_total_pnl and sim_win_rate >= live_win_rate:
                live_vs_sim_comment = "현재는 시뮬레이션이 더 강합니다. 실전에서는 진입 타이밍, 체결, 장중 리스크 가드 때문에 성과가 덜 반영될 수 있습니다."
                live_vs_sim_action = "추천 액션: 실거래 진입 시점, 체결 지연, 장중 리스크 가드가 수익을 얼마나 깎는지 로그 기준으로 점검하세요."
                primary_action_label = "실행 보드 보기"
                primary_action_target = "board-section"
                primary_action_tab = "stocks"
                primary_action_stocks = "board"
                secondary_action_label = "차단 사유 보기"
                secondary_action_target = "block-section"
                secondary_action_tab = "stocks"
                secondary_action_stocks = "blocks"
            else:
                live_vs_sim_comment = "실거래와 시뮬레이션이 서로 다른 모습을 보입니다. 보유 기간, 진입 빈도, 장중 차단 사유를 같이 봐야 정확한 해석이 됩니다."
                live_vs_sim_action = "추천 액션: 보유 기간 차이와 차단 사유 상위 항목을 함께 보고, entry/exit 조건 중 어느 쪽이 더 큰 차이를 만드는지 분리해서 점검하세요."
                primary_action_label = "실행 보드 보기"
                primary_action_target = "board-section"
                primary_action_tab = "stocks"
                primary_action_stocks = "board"
                secondary_action_label = "차단 사유 보기"
                secondary_action_target = "block-section"
                secondary_action_tab = "stocks"
                secondary_action_stocks = "blocks"
            primary_action_html = (
                f"<a class='action-link primary' href='#{primary_action_target}' data-jump-target='{primary_action_target}' data-jump-tab='{primary_action_tab}'"
                + (f" data-jump-stocks='{primary_action_stocks}'" if 'primary_action_stocks' in locals() else "")
                + f">{html.escape(primary_action_label)}</a>"
            )
            secondary_action_html = (
                f"<a class='action-link secondary' href='#{secondary_action_target}' data-jump-target='{secondary_action_target}' data-jump-tab='{secondary_action_tab}'"
                + (f" data-jump-stocks='{secondary_action_stocks}'" if 'secondary_action_stocks' in locals() else "")
                + f">{html.escape(secondary_action_label)}</a>"
            )
            delta_alert_html = (
                f"<div class='alert-strip clickable' data-jump-target='compare-summary-section' data-jump-tab='performance'><div><div class='alert-strip-title'>우선 점검 필요</div><div class='alert-strip-text'>{html.escape(' | '.join(delta_alerts[:3]))}</div></div><div class='delta-badge warn'>차이 큼</div></div>"
                if delta_alerts
                else ""
            )
            comparison_topbar_notice = ""
            if len(delta_alerts) >= 2:
                topbar_alert_message = " | ".join(delta_alerts[:2])
                topbar_alert_key = f"{result_sim_profile}|{topbar_alert_message}"
                comparison_topbar_notice = (
                    f"<div class='topbar-alert' data-alert-key='{html.escape(topbar_alert_key)}'>"
                    f"<div class='inner'>"
                    f"<div class='msg'>실거래/시뮬레이션 차이 경고: {html.escape(topbar_alert_message)}</div>"
                    f"<div class='action-row'>"
                    f"<a class='action-link primary' href='#compare-summary-section' data-jump-target='compare-summary-section' data-jump-tab='performance'>비교 바로 보기</a>"
                    f"<button type='button' class='action-link secondary topbar-alert-close'>닫기</button>"
                    f"</div>"
                    f"</div>"
                    f"</div>"
                )
            live_card_badge = "<span class='delta-badge good'>실거래 우위</span>"
            sim_card_badge = "<span class='delta-badge good'>시뮬레이션 우위</span>"
            if live_total_pnl > sim_total_pnl and live_win_rate >= sim_win_rate:
                live_card_badge = "<span class='delta-badge good'>실거래 우위</span>"
                sim_card_badge = "<span class='delta-badge'>비교 대상</span>"
            elif sim_total_pnl > live_total_pnl and sim_win_rate >= live_win_rate:
                live_card_badge = "<span class='delta-badge'>비교 대상</span>"
                sim_card_badge = "<span class='delta-badge good'>시뮬레이션 우위</span>"
            else:
                live_card_badge = "<span class='delta-badge warn'>혼합 우위</span>"
                sim_card_badge = "<span class='delta-badge warn'>혼합 우위</span>"
            live_vs_sim_cards = (
                f"<div class='card'>"
                f"<div class='section-title'>실거래 비교 카드 {live_card_badge}</div>"
                f"<div class='v' style='font-size:15px'>실제 체결 성과 기준</div>"
                f"<div class='k' style='margin-top:8px'>승률 <strong style='{win_left_style}'>{live_win_rate:.1f}%</strong> <span class='delta-badge {'warn' if abs(win_delta) >= warn_win_gap else 'good'}'>Δ {win_delta:+.1f}%p</span></div>"
                f"<div class='k'>누적손익 <strong style='{pnl_left_style}'>{live_total_pnl:+,.0f}</strong> <span class='delta-badge {'warn' if abs(pnl_delta) >= warn_pnl_gap else 'good'}'>Δ {pnl_delta:+,.0f}</span></div>"
                f"<div class='k'>Expectancy <strong style='{exp_left_style}'>{live_stats['expectancy']:+,.0f}</strong> <span class='delta-badge {'warn' if abs(exp_delta) >= warn_expectancy_gap else 'good'}'>Δ {exp_delta:+,.0f}</span></div>"
                f"<div class='k'>평균 보유 <strong style='{hold_left_style}'>{live_avg_hold:.1f}일</strong> <span class='delta-badge {'warn' if abs(hold_delta) >= warn_hold_gap else 'good'}'>Δ {hold_delta:+.1f}일</span></div>"
                f"</div>"
                f"<div class='card'>"
                f"<div class='section-title'>시뮬레이션 비교 카드 {sim_card_badge}</div>"
                f"<div class='v' style='font-size:15px'>{sim_report_type_label}</div>"
                f"<div class='k' style='margin-top:8px'>승률 <strong style='{win_right_style}'>{sim_win_rate:.1f}%</strong> <span class='delta-badge {'warn' if abs(win_delta) >= warn_win_gap else 'good'}'>Δ {win_delta:+.1f}%p</span></div>"
                f"<div class='k'>누적손익 <strong style='{pnl_right_style}'>{sim_total_pnl:+,.0f}</strong> <span class='delta-badge {'warn' if abs(pnl_delta) >= warn_pnl_gap else 'good'}'>Δ {pnl_delta:+,.0f}</span></div>"
                f"<div class='k'>Expectancy <strong style='{exp_right_style}'>{sim_stats['expectancy']:+,.0f}</strong> <span class='delta-badge {'warn' if abs(exp_delta) >= warn_expectancy_gap else 'good'}'>Δ {exp_delta:+,.0f}</span></div>"
                f"<div class='k'>평균 보유 <strong style='{hold_right_style}'>{sim_avg_hold:.1f}일</strong> <span class='delta-badge {'warn' if abs(hold_delta) >= warn_hold_gap else 'good'}'>Δ {hold_delta:+.1f}일</span></div>"
                f"<div class='k' style='margin-top:8px'>현재 리포트: {sim_report_type_label}</div>"
                f"</div>"
            )
            sim_summary = dict(sim_report.get("summary") or {}) if isinstance(sim_report.get("summary"), dict) else {}
            sim_experiment_cards = ""
            if result_sim_profile == "rolling_rank":
                sim_experiment_cards = (
                    f"<div class='ops-card'><div class='section-title'>Avg Fwd 1D</div><div class='v'>{_to_float(sim_summary.get('avg_forward_1d_pct')):+.3f}%</div><div class='k'>평균 익일 성과</div></div>"
                    f"<div class='ops-card'><div class='section-title'>Avg Fwd 3D</div><div class='v'>{_to_float(sim_summary.get('avg_forward_3d_pct')):+.3f}%</div><div class='k'>평균 3일 성과</div></div>"
                    f"<div class='ops-card'><div class='section-title'>Top1 Fwd 1D</div><div class='v'>{_to_float(sim_summary.get('top1_avg_forward_1d_pct')):+.3f}%</div><div class='k'>최상위 후보 품질</div></div>"
                    f"<div class='ops-card'><div class='section-title'>Top1 Hit</div><div class='v'>{_to_float(sim_summary.get('top1_hit_rate_pct')):.1f}%</div><div class='k'>익일 양봉 비율</div></div>"
                )
            elif result_sim_profile == "short_horizon":
                sim_experiment_cards = (
                    f"<div class='ops-card'><div class='section-title'>Avg Fwd 1D</div><div class='v'>{_to_float(sim_summary.get('avg_forward_1d_pct')):+.3f}%</div><div class='k'>평균 1일 보유</div></div>"
                    f"<div class='ops-card'><div class='section-title'>Avg Fwd 2D</div><div class='v'>{_to_float(sim_summary.get('avg_forward_2d_pct')):+.3f}%</div><div class='k'>평균 2일 보유</div></div>"
                    f"<div class='ops-card'><div class='section-title'>Top1 1D</div><div class='v'>{_to_float(sim_summary.get('top1_avg_forward_1d_pct')):+.3f}%</div><div class='k'>최상위 1일 품질</div></div>"
                    f"<div class='ops-card'><div class='section-title'>Top1 2D</div><div class='v'>{_to_float(sim_summary.get('top1_avg_forward_2d_pct')):+.3f}%</div><div class='k'>최상위 2일 품질</div></div>"
                )
            elif result_sim_profile == "daily_selection":
                blocker_text = ", ".join(
                    f"{html.escape(str(row.get('name') or '-'))} {int(_to_float(row.get('count')))}"
                    for row in list(sim_summary.get("top_entry_blockers") or [])[:4]
                    if isinstance(row, dict)
                ) or "집계 전"
                sim_experiment_cards = (
                    f"<div class='ops-card'><div class='section-title'>포트폴리오 수익률</div><div class='v'>{_to_float(sim_summary.get('return_pct')):+.3f}%</div><div class='k'>일일 재선정 누적</div></div>"
                    f"<div class='ops-card'><div class='section-title'>실현손익</div><div class='v'>{_to_float(sim_summary.get('realized_pnl')):+,.0f}</div><div class='k'>SELL 기준 합산</div></div>"
                    f"<div class='ops-card'><div class='section-title'>매수/매도</div><div class='v'>{int(_to_float(sim_summary.get('buy_count')))} / {int(_to_float(sim_summary.get('sell_count')))}</div><div class='k'>포트폴리오 체결 수</div></div>"
                    f"<div class='ops-card'><div class='section-title'>상위 진입 차단</div><div class='v' style='font-size:13px'>{blocker_text}</div><div class='k'>entry gate 병목</div></div>"
                )
            elif result_sim_profile == "rank_weighted":
                weights_text = ", ".join(str(x) for x in list(sim_summary.get("rank_weights") or sim_config.get("rank_weights") or [])) or "0.5,0.3,0.2"
                sim_experiment_cards = (
                    f"<div class='ops-card'><div class='section-title'>Weighted 1D</div><div class='v'>{_to_float(sim_summary.get('weighted_avg_forward_1d_pct')):+.3f}%</div><div class='k'>가중 평균 1일 성과</div></div>"
                    f"<div class='ops-card'><div class='section-title'>Weighted 3D</div><div class='v'>{_to_float(sim_summary.get('weighted_avg_forward_3d_pct')):+.3f}%</div><div class='k'>가중 평균 3일 성과</div></div>"
                    f"<div class='ops-card'><div class='section-title'>Weighted 5D</div><div class='v'>{_to_float(sim_summary.get('weighted_avg_forward_5d_pct')):+.3f}%</div><div class='k'>가중 평균 5일 성과</div></div>"
                    f"<div class='ops-card'><div class='section-title'>Rank Weights</div><div class='v' style='font-size:13px'>{html.escape(weights_text)}</div><div class='k'>top1/top2/top3 배분</div></div>"
                )
            elif result_sim_profile == "intraday_replay":
                sim_experiment_cards = (
                    f"<div class='ops-card'><div class='section-title'>리플레이 거래 수</div><div class='v'>{int(_to_float(sim_summary.get('trade_count')))}</div><div class='k'>저장된 BUY 신호 기준</div></div>"
                    f"<div class='ops-card'><div class='section-title'>평균 리턴</div><div class='v'>{_to_float(sim_summary.get('avg_return_pct')):+.3f}%</div><div class='k'>거래당 평균</div></div>"
                    f"<div class='ops-card'><div class='section-title'>Bar 수</div><div class='v'>{int(_to_float(sim_summary.get('bar_count')))}</div><div class='k'>저장된 2분 바</div></div>"
                    f"<div class='ops-card'><div class='section-title'>평균 Bar 변화</div><div class='v'>{_to_float(sim_summary.get('avg_bar_return_pct')):+.4f}%</div><div class='k'>2분 단위 평균 변동</div></div>"
                )
            else:
                sim_experiment_cards = (
                    f"<div class='ops-card'><div class='section-title'>거래 수</div><div class='v'>{sim_trade_count}</div><div class='k'>닫힌 거래 기준</div></div>"
                    f"<div class='ops-card'><div class='section-title'>승률</div><div class='v'>{sim_win_rate:.1f}%</div><div class='k'>승리 {sim_win_count}건</div></div>"
                    f"<div class='ops-card'><div class='section-title'>누적 손익</div><div class='v'>{sim_total_pnl:,.0f}</div><div class='k'>리포트 합산 손익</div></div>"
                    f"<div class='ops-card'><div class='section-title'>평균 보유</div><div class='v'>{sim_avg_hold:.1f}일</div><div class='k'>종목별 평균 보유</div></div>"
                )
            sim_top_map: dict[str, float] = {}
            sim_hold_map: dict[str, list[float]] = {}
            sim_weekday_map: dict[str, list[float]] = {}
            for row in sim_trades:
                symbol = str((row or {}).get("symbol") or "").strip()
                if symbol:
                    sim_top_map[symbol] = sim_top_map.get(symbol, 0.0) + _to_float((row or {}).get("realized_pnl"))
                hold_key = f"{int(_to_float((row or {}).get('hold_bars')))}일"
                sim_hold_map.setdefault(hold_key, []).append(_to_float((row or {}).get("return_pct")))
                sim_weekday_map.setdefault(_weekday_label(row.get("sell_date")), []).append(_to_float((row or {}).get("return_pct")))
            daily_selection_status = html.escape(_display_text(st.get("daily_selection_status"), "당일 선정 상태 집계 전"))
            daily_selection_day = html.escape(_display_text(st.get("daily_selection_day"), "미확정"))
            sim_top_cards = "".join(
                "<div class='rank-metric'>"
                f"{html.escape(_symbol_label(str(row.get('symbol') or ''), name_map))} "
                f"<strong>{_to_float(row.get('realized_pnl')):,.0f}</strong>"
                "</div>"
                for row in sim_summary_rows[:5]
                if isinstance(row, dict)
            )
            sim_intraday_symbol_rows = "".join(
                (
                    "<tr>"
                    f"<td>{html.escape(_symbol_label(str(row.get('symbol') or ''), name_map))}</td>"
                    f"<td>{int(_to_float(row.get('days')))}</td>"
                    f"<td>{_to_float(row.get('avg_return_pct')):+.3f}%</td>"
                    f"<td>{_to_float(row.get('realized_pnl')):+,.0f}</td>"
                    "</tr>"
                )
                for row in sim_summary_rows[:20]
                if isinstance(row, dict)
            )
            sim_result_visibility_note = (
                f"최근 실행 결과가 반영되었습니다. 거래표 기준 체결 {sim_trade_count}건, 종목 요약 {len(sim_summary_rows)}건입니다."
                if (sim_updated_at_raw and sim_requested_at_raw and sim_updated_at_raw >= sim_requested_at_raw)
                else "현재 화면에는 마지막으로 저장된 시뮬레이션 리포트를 표시합니다."
            )
            if sim_trade_count == 0:
                if sim_summary_rows:
                    sim_result_visibility_note += " 이번 실행은 닫힌 거래가 없어 거래표는 비어 있지만, 종목 요약과 차트는 아래에 계속 표시됩니다."
                else:
                    sim_result_visibility_note += " 이번 실행 결과에서 체결도 종목 요약도 아직 없어 아래 표가 비어 있을 수 있습니다."
            sim_trade_rows = "".join(
                (
                "<tr class='"
                + ("sim-win" if _to_float(row.get('realized_pnl')) > 0 else "sim-loss")
                + "'"
                + f" data-result=\"{'win' if _to_float(row.get('realized_pnl')) > 0 else 'loss'}\""
                + f" data-return=\"{_to_float(row.get('return_pct')):.4f}\""
                + f" data-pnl=\"{_to_float(row.get('realized_pnl')):.4f}\""
                + f" data-buy-date=\"{html.escape(_trade_bar_date(row.get('symbol'), row.get('buy_bar')))}\""
                + f" data-sell-date=\"{html.escape(_trade_bar_date(row.get('symbol'), row.get('sell_bar')))}\""
                + f" data-symbol=\"{html.escape(str(row.get('symbol') or ''))}\""
                + ">"
                f"<td><button type='button' class='trade-symbol-btn' data-trade-symbol=\"{html.escape(str(row.get('symbol') or ''))}\">{html.escape(_symbol_label(str(row.get('symbol') or ''), name_map))}</button></td>"
                f"<td>{html.escape(_trade_bar_date(row.get('symbol'), row.get('buy_bar')))}</td>"
                f"<td>{html.escape(_trade_bar_date(row.get('symbol'), row.get('sell_bar')))}</td>"
                f"<td>{_to_float(row.get('buy_price')):,.0f}</td>"
                f"<td>{_to_float(row.get('sell_price')):,.0f}</td>"
                f"<td>{(str(int(_to_float(row.get('qty')))) if _to_float(row.get('qty')) > 0 else '-')}</td>"
                f"<td>{_to_float(row.get('return_pct')):+.2f}%</td>"
                f"<td>{_to_float(row.get('realized_pnl')):+,.0f}</td>"
                f"<td>{int(_to_float(row.get('hold_bars')))}일</td>"
                f"<td>{html.escape(_simulation_trade_type_label(row.get('type')))}</td>"
                "</tr>"
                )
                for row in list(sim_trades)[-10:]
                if isinstance(row, dict)
            )
            live_equity_chart = _line_overlay_svg(
                [("Equity", "#67e8f9", [float(v) for v in live_equity_series])],
                summary=(
                    f"최근 평가자산 {_to_float(live_equity_series[-1] if live_equity_series else 0):,.0f}원 | "
                    f"고점 {_to_float(max(live_equity_series) if live_equity_series else 0):,.0f}원"
                ),
            )
            live_drawdown_chart = _sparkline_svg(
                live_drawdown,
                color="#ff8a80",
                unit="%",
            )
            live_trade_pnl_chart = _bar_series_svg(
                live_trade_pnl_series,
                unit="원",
                summary=(
                    f"최근 {len(live_trade_pnl_series)}건 실현손익 | "
                    f"평균 {_to_float(sum(live_trade_pnl_series) / len(live_trade_pnl_series) if live_trade_pnl_series else 0):+.0f}원"
                ),
            )
            live_trade_return_chart = _bar_series_svg(
                live_trade_return_series,
                unit="%",
                summary=(
                    f"최근 {len(live_trade_return_series)}건 거래 수익률 | "
                    f"평균 {_to_float(sum(live_trade_return_series) / len(live_trade_return_series) if live_trade_return_series else 0):+.2f}%"
                ),
            )
            live_symbol_contribution_chart = _category_bar_svg(
                [
                    (_symbol_label(symbol, name_map), pnl / 10000.0)
                    for symbol, pnl in sorted(live_top_map.items(), key=lambda item: abs(item[1]), reverse=True)[:8]
                ],
                unit="만",
                summary="종목별 누적 기여도 | 단위: 만원",
            )
            live_hold_profile_chart = _category_bar_svg(
                [
                    (
                        label,
                        (sum(vals) / len(vals)) if vals else 0.0,
                    )
                    for label, vals in sorted(
                        live_hold_map.items(),
                        key=lambda item: (_to_int(str(item[0]).replace("일", "")) if str(item[0]).endswith("일") else 9999),
                    )[:8]
                ],
                unit="%",
                summary="보유기간별 평균 수익률",
            )
            live_weekday_heatmap = _weekday_heatmap_html(
                [
                    (label, (sum(vals) / len(vals)) if vals else 0.0)
                    for label, vals in live_weekday_map.items()
                ]
            )
            sim_cumulative_chart = _line_overlay_svg(
                [("Cumulative PnL", "#22c55e", [float(v) for v in sim_cumulative_pnl])],
                summary=(
                    f"누적 손익 {_to_float(sim_cumulative_pnl[-1] if sim_cumulative_pnl else 0):+.0f}원 | "
                    f"최고 {_to_float(max(sim_cumulative_pnl) if sim_cumulative_pnl else 0):+.0f}원"
                ),
            )
            sim_drawdown_chart = _sparkline_svg(
                sim_drawdown,
                color="#f97316",
                unit="%",
            )
            sim_trade_pnl_chart = _bar_series_svg(
                sim_trade_pnl_series,
                unit="원",
                summary=(
                    f"최근 {len(sim_trade_pnl_series)}건 실현손익 | "
                    f"평균 {_to_float(sum(sim_trade_pnl_series) / len(sim_trade_pnl_series) if sim_trade_pnl_series else 0):+.0f}원"
                ),
            )
            sim_trade_return_chart = _bar_series_svg(
                sim_trade_return_series,
                unit="%",
                summary=(
                    f"최근 {len(sim_trade_return_series)}건 거래 수익률 | "
                    f"평균 {_to_float(sum(sim_trade_return_series) / len(sim_trade_return_series) if sim_trade_return_series else 0):+.2f}%"
                ),
            )
            sim_symbol_contribution_chart = _category_bar_svg(
                [
                    (_symbol_label(symbol, name_map), pnl / 10000.0)
                    for symbol, pnl in sorted(sim_top_map.items(), key=lambda item: abs(item[1]), reverse=True)[:8]
                ],
                unit="만",
                summary="종목별 누적 기여도 | 단위: 만원",
            )
            sim_hold_profile_chart = _category_bar_svg(
                [
                    (
                        label,
                        (sum(vals) / len(vals)) if vals else 0.0,
                    )
                    for label, vals in sorted(
                        sim_hold_map.items(),
                        key=lambda item: (_to_int(str(item[0]).replace("일", "")) if str(item[0]).endswith("일") else 9999),
                    )[:8]
                ],
                unit="%",
                summary="보유기간별 평균 수익률",
            )
            sim_weekday_heatmap = _weekday_heatmap_html(
                [
                    (label, (sum(vals) / len(vals)) if vals else 0.0)
                    for label, vals in sim_weekday_map.items()
                ]
            )
            runtime_live = bool(st.get("running")) and bool(st.get("thread_alive"))
            runtime_live_fresh = runtime_live and freshness_sec <= 120
            offhours = offhours_snapshot.get()
            if not _has_meaningful_payload(offhours):
                offhours = persisted_ui.get("offhours_snapshot", {}) if isinstance(persisted_ui.get("offhours_snapshot"), dict) else {}
            persisted_selection_detail = persisted_ui.get("selection_detail", {}) if isinstance(persisted_ui.get("selection_detail"), dict) else {}
            persisted_reason_hist = persisted_ui.get("reason_histogram", {}) if isinstance(persisted_ui.get("reason_histogram"), dict) else {}
            persisted_factor_snapshot = persisted_ui.get("factor_snapshot", []) if isinstance(persisted_ui.get("factor_snapshot"), list) else []
            persisted_stock_statuses = persisted_ui.get("stock_statuses", []) if isinstance(persisted_ui.get("stock_statuses"), list) else []
            persisted_daily_fields = {
                key: persisted_ui.get(key)
                for key in (
                    "daily_selection_status",
                    "daily_selection_day",
                    "market_flow_summary",
                    "vi_summary",
                    "opening_focus_summary",
                    "opening_priority_summary",
                    "opening_a_grade_summary",
                    "opening_review_summary",
                    "no_trade_summary",
                    "strategy_reference",
                    "selection_history_stats",
                    "selection_turnover_pct",
                    "selection_turnover_note",
                    "session_phase",
                    "session_profile",
                    "session_diag",
                    "session_shock_active",
                    "session_shock_reason",
                )
            }
            if persisted_stock_statuses:
                persisted_stock_statuses = [
                    {
                        **row,
                        "snapshot_source_label": str(row.get("snapshot_source_label") or "장외 스냅샷"),
                        "snapshot_updated_at": str(row.get("snapshot_updated_at") or offhours.get("updated_at") or ""),
                    }
                    for row in persisted_stock_statuses
                    if isinstance(row, dict)
                ]
            persisted_selected_symbol = str(persisted_ui.get("selected_symbol") or "").strip()
            persisted_selection_reason = str(persisted_ui.get("selection_reason") or "").strip()
            persisted_monitored_symbols = str(persisted_ui.get("monitored_symbols") or "").strip()
            if (not runtime_live_fresh) and (not st.get("selection_detail")) and persisted_selection_detail:
                st["selection_detail"] = persisted_selection_detail
            if (not runtime_live_fresh) and (not st.get("reason_histogram")) and persisted_reason_hist:
                st["reason_histogram"] = persisted_reason_hist
            if (not runtime_live_fresh) and (not st.get("factor_snapshot")) and persisted_factor_snapshot:
                st["factor_snapshot"] = persisted_factor_snapshot
            if (not runtime_live_fresh) and (not st.get("stock_statuses")) and persisted_stock_statuses:
                st["stock_statuses"] = persisted_stock_statuses
            if (not runtime_live_fresh) and (not str(st.get("selected_symbol") or "").strip()) and persisted_selected_symbol:
                st["selected_symbol"] = persisted_selected_symbol
            if (not runtime_live_fresh) and (not str(st.get("selection_reason") or "").strip()) and persisted_selection_reason:
                st["selection_reason"] = persisted_selection_reason
            if (not runtime_live_fresh) and (not str(st.get("monitored_symbols") or "").strip()) and persisted_monitored_symbols:
                st["monitored_symbols"] = persisted_monitored_symbols
            for key, value in persisted_daily_fields.items():
                if (not runtime_live_fresh) and (key not in st or _is_blankish(st.get(key))):
                    if not _is_blankish(value):
                        st[key] = value
            fallback_snapshot_rows = (
                list(st.get("factor_snapshot") or [])
                if runtime_live_fresh
                else _prefer_richer_rows(st.get("factor_snapshot"), offhours.get("rows"))
            )
            restored_monitored_symbols = _restore_monitored_symbols(
                str(st.get("monitored_symbols") or ""),
                selection_detail=st.get("selection_detail"),
                fallback_rows=fallback_snapshot_rows,
                target_count=settings.trend_select_count,
            )
            if restored_monitored_symbols:
                st["monitored_symbols"] = ",".join(restored_monitored_symbols)
            monitored_symbol_set = set(restored_monitored_symbols)
            live_stock_rows = list(st.get("stock_statuses") or [])
            use_fallback_rows = (
                (not runtime_live_fresh)
                and (
                    not live_stock_rows
                    or (
                    isinstance(fallback_snapshot_rows, list)
                    and len(fallback_snapshot_rows) > len(live_stock_rows)
                    and len(live_stock_rows) <= 1
                    )
                )
            )
            stock_rows_for_display = (live_stock_rows if not use_fallback_rows else []) or _fallback_stock_rows_from_snapshot(
                fallback_snapshot_rows,
                selected_symbols=monitored_symbol_set,
                source_label="장외 스냅샷",
                updated_at=str(offhours.get("updated_at") or ""),
            )
            fallback_snapshot_updated_at = html.escape(_display_text(offhours.get("updated_at"), "최근 유효 시각 없음"))
            fallback_snapshot_count = len(fallback_snapshot_rows) if isinstance(fallback_snapshot_rows, list) else 0
            snapshot_mode = bool(use_fallback_rows) or not runtime_live
            if snapshot_mode:
                dashboard_data_state = "장외 스냅샷"
                dashboard_data_note = "실시간 봇이 실행 중이 아니어서 마지막 유효 스냅샷을 표시합니다."
            elif freshness_sec > 120:
                dashboard_data_state = "마지막 유효값"
                dashboard_data_note = "실시간 런타임은 살아 있지만 데이터가 지연되어 마지막 유효값을 표시합니다."
            else:
                dashboard_data_state = "실시간"
                dashboard_data_note = "실시간 런타임과 최신 상태를 기준으로 표시합니다."
            if restored_monitored_symbols:
                monitored_symbol_labels = [_symbol_label(x, name_map) for x in restored_monitored_symbols]
                monitored_symbols_display = html.escape(", ".join(monitored_symbol_labels))
                monitored_symbols_chips = "".join(
                    f"<span class='chip' style='font-size:13px'>{html.escape(x)}</span>"
                    for x in monitored_symbol_labels
                )
                monitored_symbol_count = len(restored_monitored_symbols)
            should_rebuild_selection_detail = (
                not selection_detail
                or (
                    isinstance(selection_detail, dict)
                    and len(list(selection_detail.get("top_ranked") or [])) <= 1
                    and isinstance(fallback_snapshot_rows, list)
                    and len(fallback_snapshot_rows) > 1
                )
            )
            if should_rebuild_selection_detail and isinstance(fallback_snapshot_rows, list) and fallback_snapshot_rows:
                top_ranked = []
                for idx, row in enumerate(fallback_snapshot_rows[:5], start=1):
                    if not isinstance(row, dict):
                        continue
                    top_ranked.append({"rank": idx, **row})
                primary = top_ranked[0] if top_ranked else {}
                selection_detail = {
                    "selected_rank": 1,
                    "ranked_size": len(fallback_snapshot_rows),
                    "selected_score": primary.get("score", 0.0),
                    "selected_factor": primary,
                    "top_ranked": top_ranked,
                    "fallback_reason": "장외 시간에는 최근 유효 스냅샷 기준으로 표시합니다.",
                }
                st["selection_detail"] = selection_detail
                selected_factor = primary
                selected_symbol_code = str(primary.get("symbol") or selected_symbol_code).strip()
                st["selected_symbol"] = selected_symbol_code
                selected_symbol = html.escape(_display_text(_symbol_label(selected_symbol_code, name_map), "미선정"))
                selection_score = _to_float(primary.get("score"))
                if not str(st.get("selection_reason") or "").strip():
                    st["selection_reason"] = "장외 스냅샷 기준 최근 유효 데이터"
                selection_reason_text = html.escape(_display_text(st.get("selection_reason"), "선정 사유 집계 전"))
                selection_ref = html.escape(_display_text(st.get("strategy_reference"), "장외 스냅샷"))
                selected_sector_summary = ", ".join(
                    f"{_symbol_label(str(x.get('symbol') or ''), name_map)}:{str(x.get('sector') or 'UNMAPPED')}"
                    for x in top_ranked[:5]
                    if isinstance(x, dict)
                )
                selection_exact_summary = html.escape(
                    (
                        f"rank 1/{max(1, len(fallback_snapshot_rows))} | "
                        f"score {float(primary.get('score', 0.0)):+.2f} | "
                        f"M {float(primary.get('momentum_pct', 0.0)):+.2f}% | "
                        f"5D {float(primary.get('ret5_pct', 0.0)):+.2f}% | "
                        f"상대강도 {float(primary.get('relative_pct', 0.0)):+.2f}% | "
                        f"추세 {float(primary.get('trend_pct', 0.0)):+.2f}% | "
                        f"변동성 {float(primary.get('volatility_pct', 0.0)):.2f}% | "
                        f"ATR {float(primary.get('atr14_pct', 0.0)):.2f}% | "
                        f"RAM {float(primary.get('risk_adjusted_momentum', 0.0)):.2f} | "
                        f"TEF {float(primary.get('trend_efficiency', 0.0)):.2f} | "
                        f"RSI {float(primary.get('daily_rsi', 0.0)):.1f} | "
                        f"ATTN {float(primary.get('attention_ratio', 0.0)):.2f} | "
                        f"SPIKE {float(primary.get('value_spike_ratio', 0.0)):.2f}"
                    )
                )
                focus_has_selection = True
                selected_focus_badges = _selection_reason_badges_html(
                    {
                        "symbol": selected_symbol_code,
                        "rank": 1,
                        "momentum_pct": primary.get("momentum_pct"),
                        "ret5_pct": primary.get("ret5_pct"),
                        "attention_ratio": primary.get("attention_ratio"),
                        "value_spike_ratio": primary.get("value_spike_ratio"),
                        "daily_rsi": primary.get("daily_rsi"),
                    },
                    selected_symbols=monitored_symbol_set,
                    selected_limit=settings.trend_select_count,
                )
                selected_focus_metrics = "".join(
                    [
                        f"<div class='rank-metric'>선정 점수 <strong>{selection_score:+.2f}</strong></div>",
                        f"<div class='rank-metric'>20일 수익률 <strong>{float(primary.get('momentum_pct', 0.0)):+.2f}%</strong></div>",
                        f"<div class='rank-metric'>5일 수익률 <strong>{float(primary.get('ret5_pct', 0.0)):+.2f}%</strong></div>",
                        f"<div class='rank-metric'>상대강도 <strong>{float(primary.get('relative_pct', 0.0)):+.2f}%</strong></div>",
                        f"<div class='rank-metric'>추세 <strong>{float(primary.get('trend_pct', 0.0)):+.2f}%</strong></div>",
                        f"<div class='rank-metric'>RAM <strong>{float(primary.get('risk_adjusted_momentum', 0.0)):.2f}</strong></div>",
                        f"<div class='rank-metric'>TEF <strong>{float(primary.get('trend_efficiency', 0.0)):.2f}</strong></div>",
                        f"<div class='rank-metric'>TQP <strong>{float(primary.get('top_rank_quality_penalty', 0.0)):.2f}</strong></div>",
                        f"<div class='rank-metric'>RSI <strong>{float(primary.get('daily_rsi', 0.0)):.1f}</strong></div>",
                        f"<div class='rank-metric'>관심도 <strong>{float(primary.get('attention_ratio', 0.0)):.2f}</strong></div>",
                        f"<div class='rank-metric'>스파이크 <strong>{float(primary.get('value_spike_ratio', 0.0)):.2f}</strong></div>",
                    ]
                )
                selection_rank_rows = "".join(
                    "<tr>"
                    f"<td>{int(_to_float(x.get('rank')))}</td>"
                    f"<td>{html.escape(_symbol_label(str(x.get('symbol') or ''), name_map))}</td>"
                    f"<td>{_to_float(x.get('score')):+.2f}</td>"
                    f"<td>{_to_float(x.get('momentum_pct')):+.2f}%</td>"
                    f"<td>{_to_float(x.get('ret5_pct')):+.2f}%</td>"
                    f"<td>{_to_float(x.get('relative_pct')):+.2f}%</td>"
                    f"<td>{_to_float(x.get('trend_pct')):+.2f}%</td>"
                    f"<td>{_to_float(x.get('volatility_pct')):.2f}%</td>"
                    f"<td>{_to_float(x.get('risk_adjusted_momentum')):.2f}</td>"
                    f"<td>{_to_float(x.get('trend_efficiency')):.2f}</td>"
                    f"<td>{_to_float(x.get('top_rank_quality_penalty')):.2f}</td>"
                    f"<td>{_to_float(x.get('attention_ratio')):.2f}</td>"
                    f"<td>{_to_float(x.get('value_spike_ratio')):.2f}</td>"
                    f"<td>{_to_float(x.get('daily_rsi')):.1f}</td>"
                    "</tr>"
                    for x in top_ranked
                )
                selection_rank_cards = "".join(
                    (
                        "<div class='rank-card'>"
                        "<div class='rank-card-top'>"
                        "<div>"
                        f"<div class='rank-badge'>상위 후보 #{int(_to_float(x.get('rank')))}</div>"
                        f"<div class='v' style='font-size:18px;margin-top:8px'>{html.escape(_symbol_label(str(x.get('symbol') or ''), name_map))}</div>"
                        f"<div class='k' style='margin-top:4px'>RSI {_to_float(x.get('daily_rsi')):.1f} · 관심도 {_to_float(x.get('attention_ratio')):.2f} · 스파이크 {_to_float(x.get('value_spike_ratio')):.2f} · TQP {_to_float(x.get('top_rank_quality_penalty')):.2f}</div>"
                        "</div>"
                        f"<div class='rank-score'>{_to_float(x.get('score')):+.2f}</div>"
                        "</div>"
                        f"<div class='reason-badges'>{_selection_reason_badges_html(x, selected_symbols=monitored_symbol_set, selected_limit=settings.trend_select_count)}</div>"
                        "<div class='rank-meta'>"
                        f"<div class='rank-metric'>20일 수익률 <strong>{_to_float(x.get('momentum_pct')):+.2f}%</strong></div>"
                        f"<div class='rank-metric'>5일 수익률 <strong>{_to_float(x.get('ret5_pct')):+.2f}%</strong></div>"
                        f"<div class='rank-metric'>상대강도 <strong>{_to_float(x.get('relative_pct')):+.2f}%</strong></div>"
                        f"<div class='rank-metric'>추세 <strong>{_to_float(x.get('trend_pct')):+.2f}%</strong></div>"
                        f"<div class='rank-metric'>변동성 <strong>{_to_float(x.get('volatility_pct')):.2f}%</strong></div>"
                        f"<div class='rank-metric'>RAM <strong>{_to_float(x.get('risk_adjusted_momentum')):.2f}</strong></div>"
                        f"<div class='rank-metric'>TEF <strong>{_to_float(x.get('trend_efficiency')):.2f}</strong></div>"
                        f"<div class='rank-metric'>TQP <strong>{_to_float(x.get('top_rank_quality_penalty')):.2f}</strong></div>"
                        "</div>"
                        "</div>"
                    )
                    for x in top_ranked
                )
                fallback_reason_text = html.escape(_display_text(selection_detail.get("fallback_reason"), ""))
            stock_status_map = {
                str(r.get("symbol") or "").strip(): r
                for r in stock_rows_for_display
                if isinstance(r, dict) and str(r.get("symbol") or "").strip()
            }
            top_ranked_today = (
                selection_detail.get("top_ranked")
                if isinstance(selection_detail.get("top_ranked"), list)
                else []
            )
            today_top_candidate_cards = "".join(
                (
                    "<div class='rank-card'>"
                    "<div class='rank-card-top'>"
                    "<div>"
                    f"<div class='rank-badge'>오늘 후보 #{int(_to_float(row.get('rank')))}</div>"
                    f"<div class='v' style='font-size:17px;margin-top:8px'>{html.escape(_symbol_label(str(row.get('symbol') or ''), name_map))}</div>"
                    f"<div class='k' style='margin-top:4px'>점수 {_to_float(row.get('score')):+.2f} | RSI {_to_float(row.get('daily_rsi')):.1f} | {'실전 우선' if int(_to_float(row.get('rank'))) == 1 else '보조 후보'}</div>"
                    "</div>"
                    "</div>"
                    "<div class='rank-meta'>"
                    f"<div class='rank-metric'>20D <strong>{_to_float(row.get('momentum_pct')):+.2f}%</strong></div>"
                    f"<div class='rank-metric'>추세 <strong>{_to_float(row.get('trend_pct')):+.2f}%</strong></div>"
                    f"<div class='rank-metric'>관심도 <strong>{_to_float(row.get('attention_ratio')):.2f}</strong></div>"
                    f"<div class='rank-metric'>스파이크 <strong>{_to_float(row.get('value_spike_ratio')):.2f}</strong></div>"
                    "</div>"
                    f"{_candidate_key_risk_badge(row, stock_status_map.get(str(row.get('symbol') or '').strip()))}"
                    f"{_today_candidate_timeline_html(str(row.get('symbol') or ''), stock_status_map.get(str(row.get('symbol') or '').strip()), list(today_trade_summary.get('trade_rows') or []))}"
                    f"<div class='reason-badges'>{_selection_reason_badges_html(row, selected_symbols=monitored_symbol_set, selected_limit=settings.trend_select_count)}</div>"
                    "</div>"
                )
                for row in top_ranked_today[:3]
                if isinstance(row, dict)
            )
            last_known_snapshot = {
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "account_snapshot": {
                    "cash_balance": st.get("cash_balance"),
                    "equity": st.get("equity"),
                    "unrealized_pnl": st.get("unrealized_pnl"),
                    "realized_pnl": st.get("realized_pnl"),
                    "total_pnl": st.get("total_pnl"),
                    "total_return_pct": st.get("total_return_pct"),
                    "active_positions": st.get("active_positions"),
                    "position_qty": st.get("position_qty"),
                    "position_symbol": st.get("position_symbol"),
                    "positions_summary": st.get("positions_summary"),
                    "source": ((st.get("broker_account_snapshot") or {}) if isinstance(st.get("broker_account_snapshot"), dict) else {}).get("source"),
                    "updated_at": ((st.get("broker_account_snapshot") or {}) if isinstance(st.get("broker_account_snapshot"), dict) else {}).get("updated_at"),
                },
                "selected_symbol": st.get("selected_symbol"),
                "selection_reason": st.get("selection_reason"),
                "monitored_symbols": st.get("monitored_symbols"),
                "daily_selection_status": st.get("daily_selection_status"),
                "daily_selection_day": st.get("daily_selection_day"),
                "market_flow_summary": st.get("market_flow_summary"),
                "vi_summary": st.get("vi_summary"),
                "opening_focus_summary": st.get("opening_focus_summary"),
                "opening_priority_summary": st.get("opening_priority_summary"),
                "opening_a_grade_summary": st.get("opening_a_grade_summary"),
                "opening_review_summary": st.get("opening_review_summary"),
                "no_trade_summary": st.get("no_trade_summary"),
                "strategy_reference": st.get("strategy_reference"),
                "selection_history_stats": st.get("selection_history_stats") or [],
                "selection_turnover_pct": st.get("selection_turnover_pct"),
                "selection_turnover_note": st.get("selection_turnover_note"),
                "session_phase": st.get("session_phase"),
                "session_profile": st.get("session_profile"),
                "session_diag": st.get("session_diag"),
                "session_shock_active": st.get("session_shock_active"),
                "session_shock_reason": st.get("session_shock_reason"),
                "selection_detail": st.get("selection_detail") or {},
                "reason_histogram": st.get("reason_histogram") or {},
                "factor_snapshot": fallback_snapshot_rows or st.get("factor_snapshot") or [],
                "stock_statuses": stock_rows_for_display or [],
                "diagnostics": diag or {},
                "market_vibe": mk or {},
                "global_market": glb or {},
                "offhours_snapshot": offhours or {},
            }
            if any(
                _has_meaningful_payload(last_known_snapshot.get(key))
                for key in (
                    "selection_detail",
                    "reason_histogram",
                    "factor_snapshot",
                    "stock_statuses",
                    "diagnostics",
                    "market_vibe",
                    "global_market",
                    "offhours_snapshot",
                )
            ):
                _save_last_known_ui(last_known_snapshot)
            if not monitored_symbols_chips and str(selected_symbol_code or "").strip():
                monitored_symbols_chips = (
                    f"<span class='chip' style='font-size:13px'>{html.escape(_symbol_label(str(selected_symbol_code), name_map))}</span>"
                )
                if not monitored_symbols_display or monitored_symbols_display == "-":
                    monitored_symbols_display = html.escape(_symbol_label(str(selected_symbol_code), name_map))
            selection_detail_for_board = st.get("selection_detail") if isinstance(st.get("selection_detail"), dict) else {}
            watchlist_only_mode = (
                isinstance(selection_detail_for_board, dict)
                and not list(selection_detail_for_board.get("selected_symbols") or [])
                and bool(list(selection_detail_for_board.get("analysis_watch_symbols") or []))
            )
            regime_idx_pct = _to_float(mk.get("index_change_pct"))
            regime_breadth_pct = _to_float(mk.get("breadth_ratio"))
            regime_calc, regime_basis = _regime_reason(regime_idx_pct, regime_breadth_pct)
            stock_candidate_cards = "".join(
                _stock_status_card_html(r, name_map, selected_intraday_symbol_chart_map)
                for r in stock_rows_for_display
                if _stock_board_bucket(r) == "candidate"
            )
            stock_candidate_count = sum(1 for r in stock_rows_for_display if _stock_board_bucket(r) == "candidate")
            watchlist_board_rows = [
                r for r in stock_rows_for_display
                if isinstance(r, dict) and str(r.get("symbol") or "").strip() in monitored_symbol_set
            ]
            watchlist_board_cards = "".join(
                _stock_status_card_html(r, name_map, selected_intraday_symbol_chart_map)
                for r in watchlist_board_rows
            )
            watchlist_board_count = len(watchlist_board_rows)
            stock_holding_cards = "".join(
                _stock_status_card_html(r, name_map, selected_intraday_symbol_chart_map)
                for r in stock_rows_for_display
                if _stock_board_bucket(r) == "holding"
            )
            stock_holding_count = sum(1 for r in stock_rows_for_display if _stock_board_bucket(r) == "holding")
            stock_blocked_cards = "".join(
                _stock_status_card_html(r, name_map, selected_intraday_symbol_chart_map)
                for r in stock_rows_for_display
                if _stock_board_bucket(r) == "blocked"
            )
            stock_blocked_count = sum(1 for r in stock_rows_for_display if _stock_board_bucket(r) == "blocked")
            board_primary_cards = stock_candidate_cards
            board_primary_count = stock_candidate_count
            board_primary_title = "매수 후보"
            board_primary_empty = "매수 후보를 집계 중입니다."
            if watchlist_only_mode and not stock_candidate_cards and watchlist_board_cards:
                board_primary_cards = watchlist_board_cards
                board_primary_count = watchlist_board_count
                board_primary_title = "감시 후보"
                board_primary_empty = "감시 후보를 집계 중입니다."
            reason_hist_rows = "".join(
                f"<tr><td>{html.escape(str(k))}</td><td>{int(_to_float(v))}</td></tr>"
                for k, v in list((st.get("reason_histogram") or {}).items())[:12]
            )
            reason_hist_cards = _block_reason_cards_html(st.get("reason_histogram"))
            factor_rows = "".join(
                "<tr>"
                f"<td>{html.escape(_symbol_label(str(x.get('symbol') or ''), name_map))}</td>"
                f"<td>{html.escape(str(x.get('sector') or 'UNMAPPED'))}</td>"
                f"<td>{_to_float(x.get('score')):+.2f}</td>"
                f"<td>{_to_float(x.get('momentum_pct')):+.2f}%</td>"
                f"<td>{_to_float(x.get('ret5_pct')):+.2f}%</td>"
                f"<td>{_to_float(x.get('relative_pct')):+.2f}%</td>"
                f"<td>{_to_float(x.get('trend_pct')):+.2f}%</td>"
                f"<td>{_to_float(x.get('volatility_pct')):.2f}%</td>"
                f"<td>{_to_float(x.get('attention_ratio')):.2f}</td>"
                f"<td>{_to_float(x.get('value_spike_ratio')):.2f}</td>"
                f"<td>{_to_float(x.get('daily_rsi')):.1f}</td>"
                "</tr>"
                for x in st.get("factor_snapshot", [])
            )
            reconcile = st.get("reconcile_stats") or {}
            event_summary_cards = _event_summary_cards_html(event_list)
            order_rows = "".join(
                "<tr>"
                f"<td>{html.escape(str(o.get('ts') or '-'))}</td>"
                f"<td>{html.escape(_symbol_label(str(o.get('symbol') or ''), name_map))}</td>"
                f"<td>{html.escape(str(o.get('side') or '-'))}</td>"
                f"<td>{int(_to_float(o.get('qty')))}</td>"
                f"<td>{_to_float(o.get('price')):,.0f}</td>"
                f"<td>{html.escape(str(o.get('status') or '-'))}</td>"
                f"<td>{html.escape(str(o.get('detail') or '-'))}</td>"
                "</tr>"
                for o in list(st.get('order_journal', []))[-20:]
            )
            order_summary_cards = _order_summary_cards_html(reconcile, st.get("order_journal"))
            alerts: list[dict[str, str]] = []
            if st.get("last_error"):
                alerts.append(
                    {
                        "key": "last_error",
                        "level": "ERR",
                        "msg": f"봇 오류: {st.get('last_error')}",
                    }
                )
            if st.get("risk_halt_active"):
                alerts.append(
                    {
                        "key": "risk_halt",
                        "level": "WARN",
                        "msg": f"리스크 홀트 ON: {st.get('risk_halt_reason') or '-'}",
                    }
                )
            if st.get("stale_data_active"):
                alerts.append(
                    {
                        "key": "stale_data",
                        "level": "WARN",
                        "msg": f"시세 지연: {st.get('stale_data_reason') or '-'}",
                    }
                )
            if mk.get("last_error") or glb.get("last_error"):
                alerts.append(
                    {
                        "key": "data_source",
                        "level": "WARN",
                        "msg": "외부 데이터 소스 오류 발생(시장/글로벌 데이터 중 1개 이상).",
                    }
                )
            if _to_int(reconcile.get("timeout_this_loop")) > 0:
                alerts.append(
                    {
                        "key": "reconcile_timeout",
                        "level": "WARN",
                        "msg": f"리컨실 timeout 발생: {int(_to_float(reconcile.get('timeout_this_loop')))}건",
                    }
                )
            if _to_int(reconcile.get("pending")) > 0:
                alerts.append(
                    {
                        "key": "reconcile_pending",
                        "level": "INFO",
                        "msg": f"리컨실 pending: {int(_to_float(reconcile.get('pending')))}건",
                    }
                )
            if not alerts:
                alerts.append({"key": "all_ok", "level": "OK", "msg": "현재 핵심 알림 없음"})
            if no_trade_event:
                alerts.append(
                    {
                        "key": "no_trade_summary",
                        "level": "INFO",
                        "msg": f"오늘 무거래 요약: {no_trade_event}",
                    }
                )
            alerts_html = "".join(
                (
                    "<div class='card alert-row alert-level-"
                    + html.escape(a["level"].lower())
                    + "' data-alert-key='"
                    + html.escape(a["key"])
                    + "'>"
                    + f"<div class='k'>[{html.escape(a['level'])}]</div>"
                    + f"<div class='v' style='font-size:13px'>{html.escape(a['msg'])}</div>"
                    + f"<div style='margin-top:6px;text-align:right'><button type='button' class='refresh alert-ack-btn' data-alert-key='{html.escape(a['key'])}'>확인</button></div>"
                    + "</div>"
                )
                for a in alerts
            )
            menu_guide_rows = "".join(
                [
                    "<tr><td>봇 시작 / 봇 중지</td><td>전략 루프 실행을 시작하거나 멈춥니다.</td><td>실거래 전에는 먼저 모의투자로 상태를 점검합니다.</td></tr>",
                    "<tr><td>모의투자 전환 / 실거래 전환</td><td>주문 모드를 전환합니다.</td><td>실거래 전환은 반드시 실거래 승인 체크 후 사용합니다.</td></tr>",
                    "<tr><td>화면 가이드</td><td>대시보드 영역, 버튼, 새로고침 방식, 체크리스트를 설명합니다.</td><td>처음 접속했을 때 가장 먼저 확인합니다.</td></tr>",
                    "<tr><td>전략 가이드</td><td>자동 유니버스 스캔, 선정 기준, 진입, 청산, 예외 규칙을 설명합니다.</td><td>설정 변경 전후 기준을 다시 확인할 때 사용합니다.</td></tr>",
                    "<tr><td>설정</td><td>연결, 리스크, 추세 전략 파라미터를 저장합니다.</td><td>저장 후 봇이 자동 재시작되므로 반영 여부를 운영 상태에서 확인합니다.</td></tr>",
                    "<tr><td>빠른 진단</td><td>API, 경로, 계정, 알림 연결 상태를 점검합니다.</td><td>오류가 있거나 장 시작 전 점검이 필요할 때 실행합니다.</td></tr>",
                    "<tr><td>자동 새로고침</td><td>대시보드 갱신 주기를 한 곳에서만 설정합니다.</td><td>운영 중에는 30~60초, 점검 중에는 더 짧게 사용합니다.</td></tr>",
                ]
            )
            panel_guide_rows = "".join(
                [
                    "<tr><td>헤드라인/시장 개요</td><td>현재 시장 국면, 시장 강도, 상승 비율, 변동성, 관심 방향을 먼저 확인하는 영역입니다.</td></tr>",
                    "<tr><td>운영 상태</td><td>봇 실행 여부, 계좌 상태, 포트폴리오 히트, 손실 캡, 최근 오류를 한 번에 확인합니다.</td></tr>",
                    "<tr><td>오늘의 포커스</td><td>신규 롱이 가능하면 top1 선정 종목을, 약세장/충격장에서는 방어형 감시 후보 목록과 상위 후보 비교를 보여줍니다.</td></tr>",
                    "<tr><td>종목 실행 보드</td><td>종목별 액션과 추세, 구조, 돌파, 과열 여부를 함께 읽는 실시간 실행 화면입니다. 장외에는 장외 스냅샷 진단 카드로 대체됩니다.</td></tr>",
                    "<tr><td>운영 점검</td><td>중요 알림과 최근 진단 결과를 한 카드에서 빠르게 확인하는 영역입니다.</td></tr>",
                    "<tr><td>차단 사유</td><td>최근 루프에서 진입을 막은 주요 이유와 팩터 스냅샷을 함께 보여줍니다.</td></tr>",
                    "<tr><td>이벤트 스트림 / 주문 저널</td><td>최근 이벤트 요약, 주문 상태, 정합 결과를 운영 보조 정보로 확인합니다.</td></tr>",
                ]
            )
            factor_glossary_rows = "".join(
                [
                    "<tr><td>점수</td><td>멀티팩터 가중합입니다. 높을수록 우선순위가 높습니다.</td></tr>",
                    "<tr><td>20일 수익률</td><td>최근 20거래일 누적 수익률입니다.</td></tr>",
                    "<tr><td>5일 수익률</td><td>최근 5거래일 누적 수익률입니다.</td></tr>",
                    "<tr><td>상대강도</td><td>종목 모멘텀에서 시장 지수 변화를 뺀 값입니다.</td></tr>",
                    "<tr><td>추세</td><td>현재가와 이동평균선 위치 관계를 점수화한 값입니다.</td></tr>",
                    "<tr><td>관심도</td><td>최근 5일 평균 거래대금이 20일 평균 대비 얼마나 늘었는지 보여줍니다.</td></tr>",
                    "<tr><td>스파이크</td><td>최근 거래대금이 평균 대비 얼마나 급증했는지 보여줍니다.</td></tr>",
                    "<tr><td>ATR14</td><td>최근 14일 평균 변동폭 비율입니다. 손절과 익절 계산에 사용합니다.</td></tr>",
                    "<tr><td>연장패널티</td><td>너무 멀리 올라간 추세 추격을 감점하는 값입니다.</td></tr>",
                    "<tr><td>추격 과열</td><td>점수는 높아도 이미 과도하게 연장된 종목으로 판단된 상태입니다.</td></tr>",
                    "<tr><td>약세 예외 후보</td><td>약세장에서도 상대강도와 거래대금이 매우 강해 제한적으로 진입 검토하는 상태입니다.</td></tr>",
                    "<tr><td>시그널 확인</td><td>BUY 또는 SELL 신호가 연속 확인될 때만 주문을 실행합니다.</td></tr>",
                ]
            )
            checklist_beginner_rows = "".join(
                [
                    "<tr><td>1</td><td>원클릭 진단 실행 후 FAIL 항목 0개 확인</td><td style='text-align:center'><input type='checkbox' class='check-item' data-check-id='beginner-1'/></td></tr>",
                    "<tr><td>2</td><td>모의투자 유지, 실거래 승인 비활성 확인</td><td style='text-align:center'><input type='checkbox' class='check-item' data-check-id='beginner-2'/></td></tr>",
                    "<tr><td>3</td><td>전략 가이드에서 현재 시장 국면과 오늘의 포커스/상위 후보 비교 확인</td><td style='text-align:center'><input type='checkbox' class='check-item' data-check-id='beginner-3'/></td></tr>",
                    "<tr><td>4</td><td>최근 이벤트에서 ERROR/RISK 이벤트 먼저 점검</td><td style='text-align:center'><input type='checkbox' class='check-item' data-check-id='beginner-4'/></td></tr>",
                ]
            )
            checklist_intermediate_rows = "".join(
                [
                    "<tr><td>1</td><td>차단 사유 패널에서 blocked 사유 상위 3개 점검</td><td style='text-align:center'><input type='checkbox' class='check-item' data-check-id='intermediate-1'/></td></tr>",
                    "<tr><td>2</td><td>20일/5일 수익률, 상대강도, 추세, 변동성과 최종 점수 정합성 확인</td><td style='text-align:center'><input type='checkbox' class='check-item' data-check-id='intermediate-2'/></td></tr>",
                    "<tr><td>3</td><td>ATR/손절/익절 배수와 현재 시장 국면별 리스크 강도 매칭 확인</td><td style='text-align:center'><input type='checkbox' class='check-item' data-check-id='intermediate-3'/></td></tr>",
                    "<tr><td>4</td><td>주문 리컨실 대기/시간초과 증가 여부 감시</td><td style='text-align:center'><input type='checkbox' class='check-item' data-check-id='intermediate-4'/></td></tr>",
                ]
            )
            checklist_live_rows = "".join(
                [
                    "<tr><td>1</td><td>실거래 전환 전 모의투자에서 1일 이상 로그 검증</td><td style='text-align:center'><input type='checkbox' class='check-item' data-check-id='live-1'/></td></tr>",
                    "<tr><td>2</td><td>실거래 승인 체크 + 계정/경로/슬랙 재진단</td><td style='text-align:center'><input type='checkbox' class='check-item' data-check-id='live-2'/></td></tr>",
                    "<tr><td>3</td><td>초기 30분은 소수량(보수 프로필)로 모니터링</td><td style='text-align:center'><input type='checkbox' class='check-item' data-check-id='live-3'/></td></tr>",
                    "<tr><td>4</td><td>리스크 홀트/데이터 지연 발생 시 즉시 중지</td><td style='text-align:center'><input type='checkbox' class='check-item' data-check-id='live-4'/></td></tr>",
                ]
            )

            page = f"""<!doctype html>
<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<meta name='theme-color' content='#0b1524'>
<meta name='apple-mobile-web-app-capable' content='yes'>
<meta name='apple-mobile-web-app-status-bar-style' content='black-translucent'>
<meta name='apple-mobile-web-app-title' content='AITRADER'>
<link rel='manifest' href='/manifest.webmanifest'>
<link rel='icon' href='/app-icon.svg' type='image/svg+xml'>
<link rel='apple-touch-icon' href='/app-icon.svg'>
<title>자동매매 대시보드</title>
<style>
:root{{--bg:#07111d;--panel:#0f1a2a;--panel2:#132238;--panel3:#0c1624;--ink:#eef4ff;--muted:#9db0c9;--accent:#1ca46c;--warn:#d5964c;--line:#243754;--line-soft:#1b2b44;--up:#1fd38a;--down:#ff7e7e;--gap:8px;--pad:10px;}}
body{{margin:0;font-family:"SF Pro Display","Segoe UI",system-ui,-apple-system,sans-serif;background:radial-gradient(circle at 15% 0%,#143154 0,#0b1524 36%,#07111d 100%);color:var(--ink);}}
.topbar{{position:sticky;top:0;z-index:30;background:#08111ce8;backdrop-filter:blur(10px);border-bottom:1px solid var(--line);padding:8px 0;overflow:hidden;}}
.topbar-ticker{{display:flex;align-items:center;gap:18px;width:max-content;min-width:100%;animation:topbar-scroll 75s linear infinite;will-change:transform;padding:0 12px;}}
.topbar:hover .topbar-ticker{{animation-play-state:paused;}}
@keyframes topbar-scroll{{0%{{transform:translateX(0);}}100%{{transform:translateX(-50%);}}}}
.chip{{padding:5px 8px;border:1px solid var(--line);border-radius:999px;background:#0e1828;font-size:12px;color:var(--muted);white-space:nowrap;}}
.chip strong{{color:var(--ink);}}
.chip.ok{{border-color:#1f7a4f;color:#a9e9c9;background:#0f2a1f;}}
.chip.warn{{border-color:#8a6a1a;color:#ffe2a3;background:#2a2310;}}
.chip.bad{{border-color:#8c2f2f;color:#ffb7b7;background:#301515;}}
.wrap{{width:100%;max-width:1840px;margin:8px auto;padding:0 10px;min-width:0;box-sizing:border-box;}}
.pro-grid{{display:grid;gap:var(--gap);align-items:start;}}
.pro-grid > .card{{box-sizing:border-box;min-width:0;overflow:hidden;}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px;}}
.card{{background:linear-gradient(165deg,var(--panel),var(--panel2));border:1px solid var(--line);border-radius:16px;padding:var(--pad);box-shadow:0 10px 28px rgba(0,0,0,.24);min-width:0;}}
.hero{{background:linear-gradient(155deg,#11263a,#173149);}}
.market-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px;align-items:stretch;}}
.analysis-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px;}}
.chart-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:8px;align-items:stretch;}}
.chart-card{{background:linear-gradient(180deg,#0d1727,#0a1321);border-color:#1f3148;}}
.chart-card{{min-height:250px;display:flex;flex-direction:column;justify-content:flex-start;}}
.chart-label{{font-size:12px;font-weight:800;letter-spacing:.04em;text-transform:uppercase;color:#9fb6d7;margin-bottom:8px;}}
.range-tabs{{display:inline-flex;align-items:center;gap:6px;flex-wrap:wrap;}}
.range-tab{{display:inline-flex;align-items:center;justify-content:center;min-width:58px;padding:6px 10px;border-radius:999px;border:1px solid #26415f;background:#0e1a2b;color:#9fb6d7;text-decoration:none;font-size:11px;font-weight:700;line-height:1;}}
.range-tab.active{{background:#123657;color:#eef6ff;border-color:#3c78b9;box-shadow:0 0 0 1px rgba(60,120,185,.18) inset;}}
.range-tab:hover{{border-color:#4d84bf;color:#eef6ff;}}
.delta-badge{{display:inline-flex;align-items:center;gap:4px;padding:3px 8px;border-radius:999px;border:1px solid #355277;background:#122238;color:#d7e6ff;font-size:11px;font-weight:800;}}
.delta-badge.warn{{border-color:#8b4747;background:#2b1618;color:#ffc4c4;}}
.delta-badge.good{{border-color:#2a8b62;background:#0e271d;color:#c7f2dc;}}
.delta-inline-list{{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px;}}
.delta-inline-item{{display:inline-flex;align-items:center;gap:4px;padding:4px 8px;border-radius:999px;border:1px solid #355277;background:#122238;color:#d7e6ff;font-size:11px;font-weight:700;}}
.delta-inline-item.changed{{border-color:#8b4747;background:#2b1618;color:#ffc4c4;}}
.delta-inline-item.same{{border-color:#2a8b62;background:#0e271d;color:#c7f2dc;}}
.delta-inline-item.important{{box-shadow:0 0 0 1px rgba(244,114,114,.28) inset;}}
.delta-inline-item.major{{font-weight:900;border-width:2px;box-shadow:0 0 0 1px rgba(255,183,77,.22) inset;}}
.delta-inline-item .mini-flag{{display:inline-flex;align-items:center;justify-content:center;padding:1px 5px;border-radius:999px;background:rgba(255,255,255,.12);font-size:10px;font-weight:900;letter-spacing:.02em;}}
.action-row{{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;}}
.action-link{{display:inline-flex;align-items:center;justify-content:center;padding:8px 12px;border-radius:999px;border:1px solid #355277;background:#122238;color:#e6f0ff;text-decoration:none;font-size:12px;font-weight:800;cursor:pointer;}}
.action-link:hover{{border-color:#4d84bf;color:#fff;}}
.action-link.primary{{border-color:#2a8b62;background:#0e271d;color:#c7f2dc;box-shadow:0 0 0 1px rgba(42,139,98,.18) inset;}}
.action-link.secondary{{border-color:#355277;background:#122238;color:#d7e6ff;}}
.perf-jump-row{{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;}}
.perf-jump-btn{{display:inline-flex;align-items:center;justify-content:center;padding:8px 12px;border-radius:999px;border:1px solid #37547f;background:#122238;color:#dbe8ff;font-size:12px;font-weight:800;cursor:pointer;}}
.perf-jump-btn:hover{{border-color:#6da5e6;color:#fff;}}
.trade-symbol-btn{{display:inline-flex;align-items:center;justify-content:flex-start;padding:0;background:none;border:0;color:#9fd0ff;font-size:12px;font-weight:800;cursor:pointer;text-decoration:none;}}
.trade-symbol-btn:hover{{color:#ffffff;text-decoration:underline;}}
.trade-clear-btn{{display:inline-flex;align-items:center;justify-content:center;padding:7px 10px;border-radius:999px;border:1px solid #486381;background:#13253c;color:#dce8ff;font-size:11px;font-weight:800;cursor:pointer;}}
.trade-clear-btn:hover{{border-color:#7cb0ff;color:#fff;}}
.trade-row-focus td{{box-shadow:inset 0 0 0 9999px rgba(96,165,250,.10);border-top-color:#4d84bf;border-bottom-color:#4d84bf;}}
.focus-compare-card{{display:grid;gap:10px;border:1px solid #355277;background:linear-gradient(180deg,#121c2d,#0f1727);}}
.focus-compare-head{{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;}}
.focus-compare-badge{{display:inline-flex;align-items:center;justify-content:center;padding:5px 10px;border-radius:999px;border:1px solid #4d84bf;background:#13253c;color:#dbe8ff;font-size:12px;font-weight:900;letter-spacing:.04em;}}
.focus-compare-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;align-items:start;}}
.focus-compare-panel{{border:1px solid var(--line-soft);border-radius:14px;padding:10px;background:#0d1727;display:grid;gap:8px;min-width:0;}}
.focus-compare-panel.live{{border-color:rgba(42,139,98,.35);background:linear-gradient(180deg,#11211c,#0f1820);}}
.focus-compare-panel.sim{{border-color:rgba(157,100,219,.35);background:linear-gradient(180deg,#171328,#11182a);}}
.focus-compare-title{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:13px;font-weight:900;color:#eef4ff;letter-spacing:.04em;text-transform:uppercase;}}
.focus-compare-stats{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;}}
.focus-stat{{padding:8px 9px;border-radius:12px;border:1px solid var(--line-soft);background:#101c2e;display:grid;gap:3px;}}
.focus-stat .label{{font-size:11px;color:#98afd0;}}
.focus-stat .value{{font-size:16px;font-weight:900;color:#f2f7ff;line-height:1.2;}}
.focus-compare-chart{{padding:8px;border-radius:12px;border:1px solid var(--line-soft);background:#101927;}}
.focus-compare-chart svg{{width:100%;height:96px;display:block;}}
.focus-compare-chart .k{{margin-top:6px;}}
.alert-strip{{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;padding:10px 12px;border-radius:14px;border:1px solid #8b4747;background:linear-gradient(180deg,#2b1618,#221317);margin-top:10px;}}
.alert-strip.clickable{{cursor:pointer;}}
.alert-strip-title{{font-size:13px;font-weight:900;color:#ffd0d0;letter-spacing:.02em;}}
.alert-strip-text{{font-size:12px;color:#ffdede;line-height:1.45;}}
.topbar-alert{{padding:8px 12px;border-bottom:1px solid #51343a;background:linear-gradient(180deg,#241015,#1a0c11);}}
.topbar-alert .inner{{max-width:1600px;margin:0 auto;display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;}}
.topbar-alert .msg{{font-size:12px;font-weight:800;color:#ffd7d7;}}
.install-topbar{{padding:12px 14px;border-bottom:1px solid #214a39;background:linear-gradient(180deg,#0d2218,#0d1a17);box-shadow:0 10px 28px rgba(0,0,0,.18);}}
.install-topbar .inner{{max-width:1840px;margin:0 auto;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;}}
.install-topbar .msg{{font-size:14px;font-weight:900;color:#dff9ec;}}
.install-topbar .sub{{font-size:12px;color:#9dd7b9;}}
.install-hero-card{{display:none;margin:12px 0 16px;padding:18px 18px;border-radius:18px;border:1px solid #285640;background:linear-gradient(160deg,#10261b,#13231d);box-shadow:0 18px 38px rgba(0,0,0,.24);}}
.install-hero-card.visible{{display:block;}}
.install-hero-title{{font-size:22px;font-weight:900;color:#f1fff6;letter-spacing:.01em;}}
.install-hero-text{{margin-top:8px;font-size:14px;line-height:1.55;color:#cfe8da;max-width:920px;}}
.install-hero-actions{{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px;}}
.install-hero-actions .control-btn{{min-width:170px;min-height:44px;font-size:13px;border-radius:12px;}}
.install-hero-badges{{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;}}
.install-badge{{display:inline-flex;align-items:center;gap:6px;padding:5px 10px;border-radius:999px;border:1px solid #2d6246;background:#102519;color:#d8f5e4;font-size:11px;font-weight:800;}}
.install-server-card{{margin-top:12px;padding:12px 14px;border-radius:14px;border:1px solid #274b3b;background:#0d1d16;}}
.install-server-title{{font-size:12px;font-weight:900;letter-spacing:.12em;text-transform:uppercase;color:#9dd7b9;}}
.install-server-main{{margin-top:8px;font-size:15px;font-weight:800;color:#f1fff6;word-break:break-all;}}
.install-server-sub{{margin-top:5px;font-size:12px;line-height:1.45;color:#b9d6c6;}}
.install-server-actions{{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;}}
.install-server-btn{{border:1px solid #315948;background:#12251c;color:#dff7ea;border-radius:999px;padding:8px 12px;font-size:12px;font-weight:800;cursor:pointer;}}
.install-qr-wrap{{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-top:12px;}}
.install-qr-box{{display:none;align-items:center;justify-content:center;width:124px;height:124px;padding:8px;border-radius:14px;border:1px solid #315948;background:#f5fff9;}}
.install-qr-box.visible{{display:flex;}}
.install-qr-box img{{width:100%;height:100%;object-fit:contain;display:block;}}
.install-qr-note{{font-size:12px;line-height:1.45;color:#b9d6c6;max-width:420px;}}
.stats-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;}}
.common-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px;}}
.stock-grid{{display:grid;grid-template-columns:1fr;gap:8px;align-items:start;}}
.mv-primary{{font-weight:800;font-size:28px;line-height:1.15;letter-spacing:.2px;}}
.mv-secondary{{font-weight:700;font-size:14px;color:#d7e4ff;line-height:1.35;margin-top:6px;}}
.mv-tertiary{{font-size:12px;color:#9fb1d1;line-height:1.35;margin-top:6px;}}
.stock-card{{background:linear-gradient(180deg,#0e1726,#101d30);border:1px solid #27415f;border-radius:16px;padding:10px;}}
.stock-card.ready{{border-color:#2a8b62;box-shadow:0 0 0 1px rgba(42,139,98,.25),0 14px 30px rgba(0,0,0,.24);}}
.stock-card.watch{{border-color:#3b5f8d;}}
.stock-card.blocked{{border-color:#8b4747;background:linear-gradient(180deg,#16111a,#1c1522);}}
.stock-card.exit{{border-color:#9a6a2d;background:linear-gradient(180deg,#1b1620,#211a14);}}
.stock-head{{display:flex;justify-content:space-between;gap:10px;align-items:flex-start;margin-bottom:8px;}}
.stock-head-right{{display:flex;gap:6px;align-items:center;flex-wrap:wrap;justify-content:flex-end;}}
.stock-title{{font-weight:800;font-size:16px;line-height:1.2;}}
.stock-sub{{font-size:12px;color:var(--muted);margin-top:3px;}}
.stock-badge{{font-size:11px;padding:3px 8px;border-radius:999px;border:1px solid #37547f;color:#d8e7ff;background:#1b2d4a;}}
.stock-badge.selected{{border-color:#2b8e67;background:#10291f;color:#baf0d5;}}
.stock-badge.watch{{border-color:#486381;background:#13253c;color:#d8e7ff;}}
.stock-badge.live{{border-color:#2a8b62;background:#0e271d;color:#c7f2dc;}}
.stock-badge.stale{{border-color:#75612d;background:#261f12;color:#ffe3a8;}}
.stock-badge.pinned{{border-color:#3b6fd8;background:#13214a;color:#d9e6ff;}}
.readiness-pill{{font-size:11px;font-weight:800;padding:4px 9px;border-radius:999px;border:1px solid #355277;background:#122238;color:#d7e6ff;}}
.readiness-pill.ready{{border-color:#2a8b62;background:#0e271d;color:#c7f2dc;}}
.readiness-pill.watch{{border-color:#3b5f8d;background:#13253c;color:#dbe8ff;}}
.readiness-pill.blocked{{border-color:#8b4747;background:#2b1618;color:#ffc4c4;}}
.readiness-pill.exit{{border-color:#9a6a2d;background:#2d2111;color:#ffe2b5;}}
.signal-pill{{font-size:11px;font-weight:800;padding:4px 9px;border-radius:999px;border:1px solid #37547f;background:#14243d;color:#d8e7ff;}}
.signal-pill.signal-buy{{border-color:#1f875b;background:#0f2a1f;color:#c6f6de;}}
.signal-pill.signal-sell{{border-color:#8f3d3d;background:#311717;color:#ffc6c6;}}
.signal-pill.signal-hold{{border-color:#64562a;background:#241f10;color:#fbe4a7;}}
.stock-price-row{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px;margin-bottom:8px;}}
.stock-mini-chart{{margin:-2px 0 8px;}}
.stock-mini-chart svg{{height:82px !important;}}
.stock-metrics{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px;}}
.metric-chip{{display:flex;align-items:center;gap:6px;padding:6px 8px;border-radius:10px;background:#0d1727;border:1px solid var(--line-soft);font-size:12px;color:var(--muted);}}
.metric-chip strong{{color:var(--ink);font-size:12px;}}
.gate-row{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px;}}
.gate{{font-size:11px;font-weight:700;padding:4px 8px;border-radius:999px;border:1px solid #355277;background:#122238;color:#d7e6ff;}}
.gate.pass{{border-color:#2a8b62;background:#0e271d;color:#c7f2dc;}}
.gate.fail{{border-color:#8b4747;background:#2b1618;color:#ffc4c4;}}
.stock-status-summary{{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px;padding:6px 8px;border-radius:12px;background:#0d1727;border:1px solid var(--line-soft);}}
.stock-status-summary .label{{font-size:12px;color:#cfe0ff;font-weight:700;}}
.stock-status-summary .desc{{font-size:12px;color:var(--muted);}}
.stock-factor-line{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px;font-size:12px;color:#c7d7ef;}}
.stock-reason{{font-size:12px;line-height:1.45;color:#e4ecfb;padding-top:10px;border-top:1px solid var(--line-soft);}}
.dashboard-hero{{display:grid;gap:var(--gap);align-items:start;margin-bottom:var(--gap);}}
.dashboard-hero > .card{{box-sizing:border-box;min-width:0;overflow:hidden;}}
.masonry-col{{display:flex;flex-direction:column;gap:var(--gap);min-width:0;}}
body.standalone-app{{padding-top:env(safe-area-inset-top);padding-bottom:env(safe-area-inset-bottom);}}
body.standalone-app .topbar{{top:0;padding-top:max(8px, env(safe-area-inset-top));}}
body.standalone-app .wrap{{padding-bottom:max(12px, env(safe-area-inset-bottom));}}
.hero-main{{grid-column:span 1;}}
.hero-main{{padding:18px;}}
.hero-eyebrow{{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:#9fc0ea;margin-bottom:10px;}}
.hero-title{{font-size:34px;font-weight:900;letter-spacing:-.02em;line-height:1.05;margin:0 0 10px;}}
.hero-text{{font-size:14px;line-height:1.6;color:#d4e3f8;max-width:70ch;}}
.hero-pills{{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px;}}
.hero-pill{{padding:8px 10px;border-radius:999px;border:1px solid #37547f;background:#11233a;color:#d7e7ff;font-size:12px;}}
.hero-compact-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px;margin-top:12px;}}
.summary-card{{background:linear-gradient(180deg,#0f1d30,#0c1727);}}
.summary-card .v{{font-size:20px;}}
.focus-list{{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;}}
.focus-chip{{padding:7px 10px;border-radius:999px;background:#0d1727;border:1px solid var(--line-soft);font-size:12px;color:#dce8fb;}}
.focus-summary{{display:grid;gap:8px;}}
.focus-reason{{font-size:13px;line-height:1.6;color:#d8e6fb;}}
.rank-card-list{{display:grid;gap:8px;}}
.rank-card{{background:#0d1727;border:1px solid var(--line-soft);border-radius:14px;padding:10px;display:flex;flex-direction:column;justify-content:flex-start;}}
.rank-card-top{{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:8px;}}
.rank-badge{{font-size:11px;font-weight:800;padding:4px 8px;border-radius:999px;background:#13253c;border:1px solid #3b5f8d;color:#dbe8ff;}}
.rank-score{{font-size:18px;font-weight:800;color:#eef4ff;}}
.reason-badges{{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0 6px;}}
.reason-badge{{padding:6px 9px;border-radius:999px;font-size:11px;font-weight:800;letter-spacing:.02em;border:1px solid var(--line-soft);background:#101c2e;color:#d8e5fb;}}
.reason-badge.good{{background:#13261d;border-color:#295f49;color:#d6ffe7;}}
.reason-badge.wait{{background:#172235;border-color:#38567e;color:#d6e7ff;}}
.reason-badge.hold{{background:#2a2114;border-color:#8d6940;color:#ffe5b3;}}
.block-card-list{{display:grid;gap:8px;margin-top:6px;}}
.block-card{{background:#0d1727;border:1px solid var(--line-soft);border-radius:14px;padding:10px;display:flex;flex-direction:column;justify-content:flex-start;}}
.block-card-top{{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:8px;}}
.block-card-top strong{{font-size:15px;color:#eef4ff;}}
.selection-history-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px;margin-top:8px;}}
.selection-history-item{{background:#0d1727;border:1px solid var(--line-soft);border-radius:12px;padding:9px 10px;display:grid;gap:6px;min-height:0;}}
.selection-history-title{{font-size:13px;font-weight:800;color:#eef4ff;line-height:1.25;}}
.selection-history-metrics{{display:flex;flex-wrap:wrap;gap:6px;}}
.selection-history-date{{font-size:11px;color:#9bb0cb;line-height:1.25;}}
.block-reason-name{{font-size:14px;font-weight:800;color:#e7f0ff;line-height:1.45;}}
.block-reason-desc{{margin-top:6px;font-size:12px;line-height:1.55;color:#c6d5ea;}}
.rank-meta{{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;}}
.rank-metric{{padding:6px 8px;border-radius:10px;background:#101c2e;border:1px solid var(--line-soft);font-size:12px;color:var(--muted);}}
.rank-metric strong{{color:var(--ink);}}
.section-title{{font-size:13px;color:#a8bfdc;margin-bottom:8px;text-transform:uppercase;letter-spacing:.08em;}}
.section-title-row{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px;}}
.section-head{{display:flex;justify-content:space-between;align-items:flex-end;gap:8px;margin-bottom:8px;}}
.section-head .v{{font-size:20px;}}
.section-badge{{display:inline-flex;align-items:center;justify-content:center;padding:4px 10px;border-radius:999px;border:1px solid #37547f;background:#13253c;color:#dbe8ff;font-size:11px;font-weight:900;letter-spacing:.08em;text-transform:uppercase;box-shadow:0 6px 14px rgba(0,0,0,.18);}}
.section-badge.live{{border-color:#2a8b62;background:#0e271d;color:#c7f2dc;}}
.section-badge.sim{{border-color:#9d64db;background:#241533;color:#eadcff;}}
.card.cat-selection{{border-top:3px solid #3d7fe0;}}
.card.cat-market{{border-top:3px solid #1ca46c;}}
.card.cat-performance{{border-top:3px solid #a96edc;}}
.card.cat-monitor{{border-top:3px solid #d5964c;}}
#performance-section{{border-top:3px solid #d6a24c;background:linear-gradient(180deg,#16131d,#120f17);box-shadow:0 14px 30px rgba(0,0,0,.20);}}
#performance-live-section{{border-top:3px solid #2a8b62;background:linear-gradient(180deg,#0f1b18,#0d1620);}}
#performance-sim-section{{border-top:3px solid #9d64db;background:linear-gradient(180deg,#161222,#101626);}}
#performance-section .section-head .v{{color:#ffe0a6;}}
#performance-live-section .section-head .v{{color:#c7f2dc;}}
#performance-sim-section .section-head .v{{color:#e3d1ff;}}
#performance-live-section .section-title{{color:#8fd9b7;}}
#performance-sim-section .section-title{{color:#c6a8f1;}}
#performance-section .section-title{{color:#f1c98a;}}
#performance-section .card{{background:linear-gradient(180deg,#1a1520,#14111a);}}
#performance-live-section .card{{background:linear-gradient(180deg,#12211d,#101924);}}
#performance-sim-section .card{{background:linear-gradient(180deg,#181428,#11182a);}}
#performance-section .metric-summary-card{{background:linear-gradient(180deg,#1a1520,#14111a);}}
#performance-live-section .metric-summary-card{{background:linear-gradient(180deg,#12211d,#101924);}}
#performance-sim-section .metric-summary-card{{background:linear-gradient(180deg,#181428,#11182a);}}
#performance-section .ops-card{{background:linear-gradient(180deg,#1a1520,#14111a);}}
#performance-live-section .ops-card{{background:linear-gradient(180deg,#12211d,#101924);}}
#performance-sim-section .ops-card{{background:linear-gradient(180deg,#181428,#11182a);}}
#performance-section .section-head{{padding-bottom:8px;border-bottom:1px solid rgba(214,162,76,.18);}}
#performance-live-section .section-head{{padding-bottom:8px;border-bottom:1px solid rgba(42,139,98,.22);}}
#performance-sim-section .section-head{{padding-bottom:8px;border-bottom:1px solid rgba(157,100,219,.22);}}
.performance-compare-card{{position:sticky;top:12px;z-index:2;box-shadow:0 18px 38px rgba(0,0,0,.24);min-width:0;}}
.card.span-1{{grid-column:auto;}}
.card.span-2{{grid-column:auto;}}
.card.span-3{{grid-column:auto;}}
.tight-table th,.tight-table td{{padding:7px 6px;font-size:12px;}}
.mini-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:8px;}}
.info-list{{display:grid;gap:4px;}}
.info-row{{display:flex;justify-content:space-between;gap:10px;padding:4px 0;border-bottom:1px solid var(--line-soft);font-size:12px;line-height:1.35;}}
.info-row:last-child{{border-bottom:0;}}
.info-row span{{color:var(--muted);}}
.ops-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px;}}
.ops-card{{background:#0d1727;border:1px solid var(--line-soft);border-radius:14px;padding:10px;display:flex;flex-direction:column;justify-content:space-between;}}
.ops-card .v{{font-size:18px;}}
.ops-card-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:8px;margin:6px 0 8px;align-items:stretch;}}
.sim-win td{{background:rgba(24,180,122,.08);}}
.sim-loss td{{background:rgba(180,60,60,.10);}}
.sim-toolbar{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px;}}
.sim-toolbar select,.sim-toolbar input{{background:#0f172a;color:#e5eefb;border:1px solid #2a3655;border-radius:10px;padding:8px 10px;font-size:12px;}}
.sim-toolbar .k{{margin-left:auto;}}
.perf-tabs{{display:flex;gap:6px;flex-wrap:wrap;margin:4px 0 8px;}}
.perf-tab{{border:1px solid #2a3655;background:#0f172a;color:#cfe0ff;border-radius:999px;padding:8px 14px;font-size:12px;font-weight:800;cursor:pointer;}}
.perf-tab.active{{background:linear-gradient(145deg,#33557f,#243a56);color:#fff;border-color:#406797;}}
.perf-panel{{display:none;}}
.perf-panel.active{{display:block;}}
.performance-detail-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;align-items:start;margin-top:12px;}}
.performance-detail-grid > *{{min-width:0;}}
.performance-board{{display:grid;grid-column:1 / -1;width:100%;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;align-items:start;}}
.performance-board > *{{min-width:0;}}
.performance-board > #performance-section{{grid-column:1;grid-row:1;}}
.performance-board > #performance-live-section{{grid-column:2;grid-row:1;}}
.performance-board > #performance-sim-section{{grid-column:3;grid-row:1;}}
.performance-split-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;align-items:start;}}
.performance-split-grid > *{{min-width:0;}}
.performance-scroll-panel{{scrollbar-gutter:stable;overscroll-behavior:contain;overflow:auto;max-height:calc(100vh - 220px);padding-right:6px;}}
.performance-scroll-panel::-webkit-scrollbar{{width:10px;height:10px;}}
.performance-scroll-panel::-webkit-scrollbar-thumb{{background:#2b425f;border-radius:999px;border:2px solid transparent;background-clip:padding-box;}}
.performance-scroll-panel::-webkit-scrollbar-track{{background:transparent;}}
.metric-summary-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:8px;margin-top:8px;align-items:stretch;}}
.chart-grid > *, .stock-grid > *, .mini-grid > *, .ops-card-grid > *, .metric-summary-grid > *{{height:max-content;min-height:0;}}
.metric-summary-card{{background:#0d1727;border:1px solid var(--line-soft);border-radius:14px;padding:10px;display:flex;flex-direction:column;justify-content:space-between;}}
.metric-summary-card .v{{font-size:18px;}}
.weekday-heatmap{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px;}}
.weekday-cell{{border:1px solid #22344d;border-radius:12px;padding:10px;text-align:center;}}
.weekday-name{{font-size:11px;color:#b7c8e0;letter-spacing:.06em;text-transform:uppercase;}}
.weekday-value{{font-size:16px;font-weight:800;color:#eef4ff;margin-top:6px;}}
.control-panel{{position:static;top:auto;align-self:start;}}
h1{{font-size:clamp(15px,2vw,18px);margin:0 0 6px;letter-spacing:.2px;}} .k{{color:var(--muted);font-size:clamp(10px,1.5vw,12px);}} .v{{font-weight:700;font-size:clamp(13px,1.8vw,16px);}}
button{{border:0;border-radius:9px;padding:8px 11px;font-weight:700;cursor:pointer;}}
.start,.stop,.refresh{{background:#243246;color:#e3ecfa;}}
.control-panel .controls{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin-top:10px;}}
.control-panel form{{margin:0;}}
.control-btn{{width:100%;min-width:110px;height:34px;border-radius:9px;font-size:11px;letter-spacing:.05px;font-weight:800;display:flex;align-items:center;justify-content:center;text-align:center;white-space:nowrap;box-shadow:0 4px 10px rgba(0,0,0,.18);transition:transform .12s ease, box-shadow .12s ease, filter .12s ease;}}
.control-btn:hover{{transform:translateY(-1px);box-shadow:0 10px 18px rgba(0,0,0,.28);filter:brightness(1.03);}}
.control-btn.start,.control-btn.stop,.control-btn.refresh{{background:linear-gradient(145deg,#324862,#223246);color:#e8f0fb;border:1px solid #334b69;}}
.control-btn.state-active{{color:#ffffff;box-shadow:0 0 0 1px rgba(255,255,255,.08),0 12px 24px rgba(0,0,0,.24);}}
.control-btn.state-running{{background:linear-gradient(145deg,#1b9c6b,#136b49);border:1px solid #42d59e;}}
.control-btn.state-stopped{{background:linear-gradient(145deg,#8a4850,#5c2a31);border:1px solid #d7848d;}}
.control-btn.state-dry{{background:linear-gradient(145deg,#446a96,#28486b);border:1px solid #80acd8;}}
.control-btn.state-live{{background:linear-gradient(145deg,#d08b28,#8d5410);border:1px solid #ffc56e;}}
.control-section{{margin-top:10px;display:grid;gap:8px;}}
.control-section-title{{font-size:12px;color:#9fb1d1;font-weight:800;letter-spacing:.08em;text-transform:uppercase;}}
.control-grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;}}
.control-grid.utility{{grid-template-columns:repeat(4,minmax(0,1fr));}}
.control-stack{{display:grid;grid-template-columns:1fr;gap:6px;}}
.control-item{{display:grid;gap:4px;align-content:start;}}
.control-item-note{{font-size:10px;line-height:1.35;color:#92a8c5;text-align:center;min-height:26px;}}
.auto-refresh{{margin-top:10px;display:flex;align-items:center;justify-content:space-between;gap:8px;padding:8px;border:1px solid #2b4266;border-radius:10px;background:#0f1a2d;}}
.auto-refresh .left{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;}}
.auto-refresh label{{font-size:12px;color:#cfe0ff;}}
.auto-refresh select{{background:#152640;border:1px solid #2d4368;color:#dbe9ff;border-radius:8px;padding:6px 8px;font-size:12px;}}
.auto-refresh .countdown{{font-size:11px;color:#9fb1d1;min-width:96px;text-align:right;}}
pre{{background:#0a1322;color:#dbe7ff;border-radius:8px;padding:8px;max-height:260px;overflow:auto;font-size:11px;}}
code{{background:#1a2a45;padding:2px 6px;border-radius:6px;}}
table{{width:100%;border-collapse:collapse;font-size:11px;}}
th,td{{border-bottom:1px solid #263a58;padding:5px;text-align:right;}}
th:first-child,td:first-child{{text-align:left;}}
.help-modal{{position:fixed;inset:0;z-index:120;display:none;}}
.help-modal.show{{display:block;}}
.config-modal{{position:fixed;inset:0;z-index:130;display:none;}}
.config-modal.show{{display:block;}}
.help-backdrop{{position:absolute;inset:0;background:rgba(5,10,18,.75);backdrop-filter:blur(2px);}}
.help-dialog{{position:relative;max-width:min(1100px,96vw);max-height:90vh;margin:4vh auto;background:#101a2c;border:1px solid #2a3f62;border-radius:14px;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 20px 50px rgba(0,0,0,.5);}}
.config-dialog{{position:relative;max-width:min(860px,96vw);max-height:90vh;margin:4vh auto;background:#101a2c;border:1px solid #2a3f62;border-radius:14px;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 20px 50px rgba(0,0,0,.5);}}
.help-head{{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:14px 16px;border-bottom:1px solid #2a3f62;background:#13233b;}}
.help-title{{font-size:20px;font-weight:800;line-height:1.35;}}
.help-close{{background:#2a3f62;color:#dbe7ff;border:0;border-radius:8px;padding:8px 11px;font-size:14px;cursor:pointer;}}
.cfg-grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px;}}
.cfg-field label{{display:block;color:#9fb1d1;font-size:12px;margin-bottom:4px;}}
.cfg-field input{{width:100%;box-sizing:border-box;background:#0d1728;border:1px solid #2c4266;color:#e7efff;border-radius:8px;padding:8px;}}
.cfg-actions{{display:flex;justify-content:flex-end;gap:8px;margin-top:10px;}}
.cfg-note{{padding:6px 8px;background:#12301f;border:1px solid #2f8f5a;color:#bdf7da;border-radius:8px;font-size:12px;margin-bottom:8px;}}
.evt-filter{{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0;}}
.evt-btn{{background:#1c2a44;color:#dce9ff;border:1px solid #2f4469;border-radius:8px;padding:3px 7px;font-size:10px;cursor:pointer;}}
.evt-btn.active{{background:#315684;color:#fff;border-color:#4b79b0;}}
.alert-level-err{{border-color:#8c2f2f;background:linear-gradient(160deg,#2f1515,#24131c);}}
.alert-level-warn{{border-color:#8a6a1a;background:linear-gradient(160deg,#2b210f,#211a12);}}
.alert-level-info{{border-color:#2f4f7f;background:linear-gradient(160deg,#15233a,#142033);}}
.alert-level-ok{{border-color:#1f7a4f;background:linear-gradient(160deg,#11281d,#13231d);}}
.help-body{{padding:16px;overflow:auto;line-height:1.75;font-size:15px;color:#e9f1ff;}}
.help-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-bottom:10px;}}
.help-card{{background:#111f34;border:1px solid #2a3f62;border-radius:10px;padding:12px;margin-bottom:10px;}}
.help-k{{color:#9fb1d1;font-size:13px;}}
.help-v{{font-size:18px;font-weight:800;}}
.help-body ul{{margin:6px 0 10px 20px;}}
.help-body li{{margin:3px 0;}}
.help-body table{{width:100%;border-collapse:collapse;font-size:13px;}}
.help-body th,.help-body td{{padding:7px;border-bottom:1px solid #2a3f62;text-align:right;}}
.help-body th:first-child,.help-body td:first-child{{text-align:left;}}
.help-body pre{{font-size:13px;line-height:1.5;max-height:180px;}}
@media(max-width:1450px){{.control-panel{{position:static;}}}}
@media(max-width:1200px){{.weekday-heatmap{{grid-template-columns:repeat(3,minmax(0,1fr));}}}}
@media(min-width:1280px){{.dashboard-hero,.pro-grid{{grid-template-columns:repeat(3,minmax(0,1fr));}}}}
@media(min-width:900px) and (max-width:1279px){{.dashboard-hero,.pro-grid{{grid-template-columns:repeat(2,minmax(0,1fr));}}}}
@media(max-width:899px){{.dashboard-hero,.pro-grid{{grid-template-columns:1fr;}}}}
@media(max-width:900px){{.market-grid{{grid-template-columns:1fr 1fr;}} .grid{{grid-template-columns:repeat(2,minmax(0,1fr));}} .control-grid{{grid-template-columns:repeat(2,minmax(0,1fr));}} .control-grid.utility{{grid-template-columns:repeat(2,minmax(0,1fr));}} .stock-price-row{{grid-template-columns:repeat(2,minmax(0,1fr));}} .hero-compact-grid{{grid-template-columns:repeat(2,minmax(0,1fr));}}}}
@media(max-width:980px){{.help-grid{{grid-template-columns:1fr;}} .help-title{{font-size:18px;}} .help-body{{font-size:14px;}}}}
@media(max-width:700px){{.wrap{{padding:0 8px 88px;}} .topbar{{padding:6px 0;}} .topbar-ticker{{animation:topbar-scroll-mobile 38s linear infinite;gap:8px;padding:0 8px;width:max-content;min-width:100%;}} .topbar:hover .topbar-ticker{{animation-play-state:paused;}} @keyframes topbar-scroll-mobile{{0%{{transform:translateX(0);}}100%{{transform:translateX(-50%);}}}} .grid{{grid-template-columns:1fr;}} .market-grid{{grid-template-columns:1fr;}} .analysis-grid{{grid-template-columns:1fr;}} .stats-grid{{grid-template-columns:1fr;}} .common-grid{{grid-template-columns:1fr;}} .stock-grid{{grid-template-columns:1fr;}} .hero-compact-grid{{grid-template-columns:1fr;}} .mini-grid{{grid-template-columns:1fr;}} .ops-grid{{grid-template-columns:1fr;}} .performance-detail-grid{{grid-template-columns:1fr;}} .performance-board{{grid-template-columns:1fr;}} .performance-board > #performance-section,.performance-board > #performance-live-section,.performance-board > #performance-sim-section{{grid-column:auto;grid-row:auto;}} .performance-split-grid{{grid-template-columns:1fr;}} .focus-compare-grid{{grid-template-columns:1fr;}} .focus-compare-stats{{grid-template-columns:1fr;}} .control-grid{{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;}} .control-grid.utility{{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;}} .control-item-note{{font-size:11px;line-height:1.35;}} .control-btn{{min-height:42px;font-size:12px;}} .chip{{font-size:11px;padding:4px 6px;}} button{{padding:8px 10px;font-size:12px;}} .help-dialog{{max-height:95vh;margin:2vh auto;}} .config-dialog{{max-height:95vh;margin:2vh auto;}} .cfg-grid{{grid-template-columns:1fr;}} .hero-title{{font-size:28px;}} .card{{padding:10px;border-radius:14px;}} .section-head{{gap:6px;}} .v{{line-height:1.2;}} .mobile-dock{{display:grid;}} .chart-card{{min-height:220px;}} .chart-card svg{{height:150px !important;}} .install-topbar{{padding:12px 10px;}} .install-topbar .msg{{font-size:15px;}} .install-topbar .sub{{font-size:12px;line-height:1.45;}} .install-hero-card{{padding:16px 14px;}} .install-hero-title{{font-size:24px;line-height:1.2;}} .install-hero-text{{font-size:14px;}} .install-hero-actions{{grid-template-columns:1fr;display:grid;}} .install-hero-actions .control-btn{{width:100%;min-width:0;min-height:48px;font-size:14px;}} .performance-scroll-panel{{overflow:auto;max-height:56vh;padding-right:4px;-webkit-overflow-scrolling:touch;overscroll-behavior:contain;touch-action:pan-y;}}}}
@media(max-width:700px){{.wrap{{padding:0 8px 88px;}} .topbar{{padding:6px 0;}} .topbar-ticker{{animation:topbar-scroll-mobile 38s linear infinite;gap:8px;padding:0 8px;width:max-content;min-width:100%;}} .topbar:hover .topbar-ticker{{animation-play-state:paused;}} @keyframes topbar-scroll-mobile{{0%{{transform:translateX(0);}}100%{{transform:translateX(-50%);}}}} .grid{{grid-template-columns:1fr;}} .market-grid{{grid-template-columns:1fr;}} .analysis-grid{{grid-template-columns:1fr;}} .stats-grid{{grid-template-columns:1fr;}} .common-grid{{grid-template-columns:1fr;}} .stock-grid{{grid-template-columns:1fr;}} .hero-compact-grid{{grid-template-columns:1fr;}} .mini-grid{{grid-template-columns:1fr;}} .ops-grid{{grid-template-columns:1fr;}} .performance-detail-grid{{grid-template-columns:1fr;}} .performance-board{{grid-template-columns:1fr;gap:10px;}} .performance-board > #performance-section,.performance-board > #performance-live-section,.performance-board > #performance-sim-section{{grid-column:auto;grid-row:auto;}} .performance-split-grid{{grid-template-columns:1fr;}} .focus-compare-grid{{grid-template-columns:1fr;}} .focus-compare-stats{{grid-template-columns:1fr;}} .control-grid{{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;}} .control-grid.utility{{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;}} .control-item-note{{font-size:11px;line-height:1.35;}} .control-btn{{min-height:42px;font-size:12px;}} .chip{{font-size:11px;padding:4px 6px;}} button{{padding:8px 10px;font-size:12px;}} .help-dialog{{max-height:95vh;margin:2vh auto;}} .config-dialog{{max-height:95vh;margin:2vh auto;}} .cfg-grid{{grid-template-columns:1fr;}} .hero-title{{font-size:28px;}} .card{{padding:10px;border-radius:14px;}} .section-head{{gap:6px;}} .v{{line-height:1.2;}} .mobile-dock{{display:grid;}} .chart-card{{min-height:220px;}} .chart-card svg{{height:150px !important;}} .install-topbar{{padding:12px 10px;}} .install-topbar .msg{{font-size:15px;}} .install-topbar .sub{{font-size:12px;line-height:1.45;}} .install-hero-card{{padding:16px 14px;}} .install-hero-title{{font-size:24px;line-height:1.2;}} .install-hero-text{{font-size:14px;}} .install-hero-actions{{grid-template-columns:1fr;display:grid;}} .install-hero-actions .control-btn{{width:100%;min-width:0;min-height:48px;font-size:14px;}} .performance-scroll-panel{{overflow:visible;max-height:none;padding-right:0;-webkit-overflow-scrolling:auto;overscroll-behavior:auto;touch-action:auto;}} .performance-compare-card{{position:static;top:auto;z-index:auto;box-shadow:0 10px 22px rgba(0,0,0,.18);}} #performance-section{{margin-bottom:2px;}} #performance-live-section,#performance-sim-section{{scroll-margin-top:10px;}}}}
@media(min-width:701px) and (max-width:959px){{.performance-board{{grid-template-columns:repeat(2,minmax(0,1fr));}} .performance-board > #performance-section{{grid-column:1 / -1;grid-row:1;}} .performance-board > #performance-live-section{{grid-column:1;grid-row:2;}} .performance-board > #performance-sim-section{{grid-column:2;grid-row:2;}}}}
.mobile-dock{{display:none;position:fixed;left:0;right:0;bottom:0;z-index:45;padding:8px max(10px, env(safe-area-inset-right)) calc(8px + env(safe-area-inset-bottom)) max(10px, env(safe-area-inset-left));background:linear-gradient(180deg,rgba(7,17,29,0),rgba(7,17,29,.92) 24%,rgba(7,17,29,.98));grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;backdrop-filter:blur(10px);}}
.mobile-dock a{{display:flex;align-items:center;justify-content:center;min-height:42px;border-radius:12px;border:1px solid #26415f;background:#0d1829;color:#dce9ff;text-decoration:none;font-size:12px;font-weight:800;}}
.mobile-dock a:hover{{border-color:#4d84bf;}}
.dashboard-tabbar{{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:0 0 12px;}}
.dashboard-main-tab{{display:inline-flex;align-items:center;gap:8px;border:1px solid #2d4368;background:#0f172a;color:#dbe7ff;border-radius:999px;padding:10px 14px;font-size:13px;font-weight:800;cursor:pointer;box-shadow:0 6px 14px rgba(0,0,0,.18);}}
.dashboard-main-tab.active{{background:linear-gradient(145deg,#33557f,#243a56);color:#fff;border-color:#4b79b0;}}
.dashboard-main-tab[data-dashboard-tab='status']{{border-color:#7b6333;background:#16140f;}}
.dashboard-main-tab[data-dashboard-tab='stocks']{{border-color:#355a8d;background:#0f1728;}}
.dashboard-main-tab[data-dashboard-tab='performance']{{border-color:#6e4a9e;background:#161222;}}
.dashboard-main-tab[data-dashboard-tab='status'].active{{background:linear-gradient(145deg,#9d7b32,#6d5419);border-color:#e2b861;}}
.dashboard-main-tab[data-dashboard-tab='stocks'].active{{background:linear-gradient(145deg,#3d7fe0,#274f90);border-color:#7cb0ff;}}
.dashboard-main-tab[data-dashboard-tab='performance'].active{{background:linear-gradient(145deg,#9d64db,#663f97);border-color:#d0a5ff;}}
.dashboard-main-tab .count{{display:inline-flex;align-items:center;justify-content:center;min-width:22px;height:22px;border-radius:999px;background:#122238;border:1px solid #37547f;color:#d9e9ff;font-size:11px;font-weight:900;padding:0 6px;}}
.dashboard-main-tab.active .count{{background:#163250;border-color:#5f8cca;color:#ffffff;}}
.dashboard-main-tab .hint{{display:inline-flex;align-items:center;padding:4px 8px;border-radius:999px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.09);font-size:10px;font-weight:900;letter-spacing:.04em;text-transform:uppercase;}}
.dashboard-tab-help{{font-size:12px;color:#9fb1d1;margin-bottom:12px;}}
.stocks-subtabbar{{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:0 0 12px;}}
.stocks-subtab{{display:inline-flex;align-items:center;gap:6px;border:1px solid #29466c;background:#0e1727;color:#d7e6ff;border-radius:999px;padding:8px 12px;font-size:12px;font-weight:800;cursor:pointer;}}
.stocks-subtab.active{{background:linear-gradient(145deg,#315684,#223e61);color:#fff;border-color:#6da5e6;}}
.stocks-subtab .count{{display:inline-flex;align-items:center;justify-content:center;min-width:18px;height:18px;padding:0 5px;border-radius:999px;background:#13253c;border:1px solid #37547f;color:#d9e9ff;font-size:10px;font-weight:900;}}
.stocks-subtab .hint{{display:inline-flex;align-items:center;padding:3px 6px;border-radius:999px;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.08);font-size:9px;font-weight:900;letter-spacing:.04em;text-transform:uppercase;}}
.stocks-subtab-help{{font-size:12px;color:#9bb0cb;margin:-2px 0 12px;}}
@media(max-width:700px){{
  .dashboard-tabbar{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));}}
  .dashboard-main-tab{{justify-content:center;padding:10px 8px;font-size:12px;}}
  .dashboard-main-tab .count,.dashboard-main-tab .hint,.stocks-subtab .count,.stocks-subtab .hint{{display:none;}}
  .wrap{{padding:0 8px;}}
  .dashboard-hero,
  .pro-grid,
  .performance-board,
  .performance-split-grid,
  .grid,
  .mini-grid,
  .market-grid,
  .analysis-grid,
  .chart-grid,
  .ops-grid,
  .ops-card-grid,
  .metric-summary-grid,
  .focus-compare-grid,
  .selection-history-grid{{
    display:grid;
    grid-template-columns:minmax(0,1fr) !important;
  }}
  .card,
  .hero,
  .hero-main,
  .control-panel,
  .summary-card,
  .ops-card,
  .metric-summary-card,
  .rank-card,
  .stock-card,
  .block-card,
  .help-card,
  .focus-compare-card,
  .focus-compare-panel,
  .install-hero-card,
  .install-server-card,
  .cat-selection,
  .cat-monitor,
  .cat-market,
  .cat-performance,
  #performance-section,
  #performance-live-section,
  #performance-sim-section,
  #focus-section,
  #board-section,
  #market-section,
  #block-section,
  #ops-status,
  #today-detail-section,
  #ops-monitor,
  #event-section,
  #journal-section{{
    width:100% !important;
    max-width:100% !important;
    min-width:0 !important;
    box-sizing:border-box;
    margin-left:0 !important;
    margin-right:0 !important;
  }}
  .rank-card-list,
  .block-card-list,
  .stock-grid,
  .focus-list,
  .hero-compact-grid,
  .install-hero-actions,
  .install-server-actions,
  .action-row,
  .perf-jump-row,
  .stocks-subtabbar{{
    width:100%;
    max-width:100%;
    min-width:0;
    box-sizing:border-box;
  }}
  .install-hero-actions,
  .install-server-actions,
  .action-row,
  .perf-jump-row{{
    display:grid;
    grid-template-columns:minmax(0,1fr);
  }}
  .control-grid,
  .sim-toolbar{{
    grid-template-columns:minmax(0,1fr) !important;
    display:grid;
  }}
  .control-grid > *,
  .sim-toolbar > *,
  .stocks-subtabbar > *,
  .install-hero-actions > *,
  .install-server-actions > *,
  .action-row > *,
  .perf-jump-row > *{{
    min-width:0 !important;
    width:100% !important;
    box-sizing:border-box;
  }}
  input, select, button, textarea{{
    max-width:100%;
    box-sizing:border-box;
  }}
  table{{
    width:100%;
    max-width:100%;
  }}
}}
</style></head>
<body>
<div class='topbar'>
  <div class='topbar-ticker'>
    {topbar_items}
    {topbar_items_clone}
  </div>
</div>
<div class='install-topbar' id='installTopbar' hidden>
  <div class='inner'>
    <div>
      <div class='msg' id='installTopbarMsg'>iPhone에서 AITRADER 앱 설치 가능</div>
      <div class='sub' id='installTopbarSub'>TestFlight 또는 홈 화면 설치를 바로 사용할 수 있습니다.</div>
    </div>
    <div class='install-hero-actions'>
      <button type='button' id='installTopbarPrimaryBtn' class='action-link'>지금 설치</button>
      <button type='button' id='installTopbarGuideBtn' class='action-link secondary'>설치 방법</button>
    </div>
  </div>
</div>
{comparison_topbar_notice}
<div class='wrap'>
<div class='dashboard-tabbar' aria-label='대시보드 카테고리'>
  <button type='button' class='dashboard-main-tab active' data-dashboard-tab='status'>상태/제어 <span class='hint'>운영</span> <span class='count'>7</span></button>
  <button type='button' class='dashboard-main-tab' data-dashboard-tab='stocks'>종목 분석 <span class='hint'>장중</span> <span class='count'>4</span></button>
  <button type='button' class='dashboard-main-tab' data-dashboard-tab='performance'>성과/복기 <span class='hint'>장후</span> <span class='count'>3</span></button>
</div>
<div class='dashboard-tab-help'>운영 판단은 `상태/제어`, 종목 해석은 `종목 분석`, 장마감 복기와 시뮬레이션은 `성과/복기` 탭에서 보시면 됩니다.</div>
<div class='install-hero-card' id='installHeroCard'>
  <div class='install-hero-title' id='installHeroTitle'>iPhone 앱으로 설치</div>
  <div class='install-hero-text' id='installHeroText'>이 기기에서는 native 설치 또는 홈 화면 설치를 사용할 수 있습니다.</div>
  <div class='install-hero-actions'>
    <button type='button' id='installHeroPrimaryBtn' class='control-btn start'>지금 설치</button>
    <button type='button' id='installHeroPwaBtn' class='control-btn refresh'>홈 화면 설치</button>
    <button type='button' id='installHeroGuideBtn' class='control-btn refresh'>설치 가이드</button>
  </div>
  <div class='install-hero-badges'>
    <span class='install-badge'>iPhone 전용 안내</span>
    <span class='install-badge' id='installHeroBadge'>설치 링크 대기</span>
  </div>
  <div class='install-server-card'>
    <div class='install-server-title'>서버 연결 설정</div>
    <div class='install-server-main' id='installServerValue'>서버 주소를 아직 설정하지 않았습니다.</div>
    <div class='install-server-sub' id='installServerText'>앱 설치 후 이 주소를 iPhone 앱의 연결 주소로 사용합니다.</div>
    <div class='install-server-actions'>
      <button type='button' id='installServerOpenAppBtn' class='install-server-btn'>설치된 앱 열기</button>
      <button type='button' id='installServerCopyBtn' class='install-server-btn'>서버 주소 복사</button>
      <button type='button' id='installServerGuideBtn' class='install-server-btn'>연결 방법</button>
    </div>
    <div class='install-qr-wrap'>
      <div class='install-qr-box' id='installServerQrBox'>
        <img id='installServerQrImg' alt='AITRADER server QR code' />
      </div>
      <div class='install-qr-note' id='installServerQrNote'>서버 주소를 설정하면 QR 코드가 표시됩니다. 다른 기기에서 빠르게 서버 주소를 열거나 확인할 때 사용할 수 있습니다.</div>
    </div>
  </div>
</div>
<div class='stocks-subtabbar' aria-label='종목 분석 세부 탭'>
  <button type='button' class='stocks-subtab active' data-stocks-tab='focus'>포커스 <span class='hint'>후보</span> <span class='count'>1</span></button>
  <button type='button' class='stocks-subtab' data-stocks-tab='board'>실행 보드 <span class='hint'>액션</span> <span class='count'>1</span></button>
  <button type='button' class='stocks-subtab' data-stocks-tab='market'>시장 <span class='hint'>컨텍스트</span> <span class='count'>1</span></button>
  <button type='button' class='stocks-subtab' data-stocks-tab='blocks'>차단사유 <span class='hint'>원인</span> <span class='count'>1</span></button>
</div>
<div class='stocks-subtab-help'>`종목 분석` 탭에서는 지금 필요한 관점만 열어서 보실 수 있습니다. 포커스는 후보 해석, 실행 보드는 액션, 시장은 컨텍스트, 차단사유는 무거래 원인 확인에 맞습니다.</div>
<div class='dashboard-hero'>
  <div class='card hero hero-main' id='hero-summary' data-dashboard-tab='status'>
    <div class='hero-eyebrow'>KRX 추세 전략</div>
    <div class='hero-title'>{current_regime} 시장 국면</div>
    <div class='hero-text'>
      현재 시스템은 <strong>{selection_ref}</strong> 기준으로
      상승 추세, 구조, 관심도, 과열 제외 조건을 통과한 후보만 선별하고 장중 자동 집행합니다.
    </div>
    {(
      f"<div class='cfg-note' style='margin-top:10px;background:#3a200f;border-color:#7a4727;color:#ffd2b5;'>"
      f"{html.escape(dashboard_data_note)}"
      f"</div>"
      if snapshot_mode else ""
    )}
    <div class='hero-pills'>
      <span class='hero-pill'>선택 종목 {monitored_symbol_count}개</span>
      <span class='hero-pill'>활성 포지션 {st.get('active_positions')}</span>
      <span class='hero-pill'>국면 신뢰도 {round(float(st.get('regime_confidence', 0.0)) * 100.0, 1)}%</span>
      <span class='hero-pill'>데이터 {(_to_float(st.get('data_freshness_sec'))):.1f}s</span>
      <span class='hero-pill'>리스크 홀트 {risk_halt_display}</span>
    </div>
    <div class='hero-compact-grid'>
      <div class='card summary-card'><div class='k'>시장 강도</div><div class='v'>{sentiment_display}</div><div class='k'>/ 100</div></div>
      <div class='card summary-card'><div class='k'>상승 비율</div><div class='v'>{breadth_ratio_display}{'' if breadth_ratio_display == '집계 중' else '%'}</div><div class='k'>상승/하락 {rising_display}/{falling_display}</div></div>
      <div class='card summary-card'><div class='k'>단기 변동성</div><div class='v'>{intraday_vol:.2f}%</div><div class='k'>최근 {len(vol_window)}개 표준편차</div></div>
      <div class='card summary-card'><div class='k'>글로벌 리스크온</div><div class='v'>{risk_on_label_display}</div><div class='k'>{risk_on_score:+.2f}</div></div>
    </div>
    <div class='mini-grid' style='margin-top:12px'>
      <div class='card'>
        <div class='section-title'>선정 요약</div>
        <div class='v' style='font-size:18px'>{selected_symbol}</div>
        <div class='k' style='margin-top:6px'>기준 {selection_ref} | 일일 선정 {daily_selection_status}</div>
      </div>
      <div class='card'>
        <div class='section-title'>오늘의 관심 섹터</div>
        <div class='k'>선정 섹터</div>
        <div class='v' style='font-size:14px'>{html.escape(_display_text(selected_sector_summary, '섹터 매핑 대기 중'))}</div>
        <div class='k' style='margin-top:8px'>상승 상위 업종: {_display_text(sectors_up, '집계 중')}</div>
        <div class='k'>하락 상위 업종: {_display_text(sectors_down, '집계 중')}</div>
      </div>
    </div>
  </div>
  <div class='card control-panel' id='control-panel-card' data-dashboard-tab='status'>
      <h1>자동매매 제어</h1>
      {("<div class='cfg-note'>설정이 저장되었고 봇이 자동 재시작되어 반영되었습니다.</div>" if config_state == "applied" else "")}
      {("<div class='cfg-note' style='background:#3a200f;border-color:#7a4727;color:#ffd2b5;'>설정은 저장됐지만 봇 자동 재시작에 실패했습니다. 수동으로 시작/중지 후 확인하세요.</div>" if config_state == "failed" else "")}
      {("<div class='cfg-note'>원클릭 진단을 완료했습니다.</div>" if diag_state == "done" else "")}
      {("<div class='cfg-note'>거래 모드가 반영되었습니다.</div>" if mode_state == "applied" else "")}
      {("<div class='cfg-note' style='background:#3a200f;border-color:#7a4727;color:#ffd2b5;'>거래 모드 반영 중 봇 재시작에 실패했습니다.</div>" if mode_state == "failed" else "")}
      <div class='control-section'>
        <div class='control-section-title'>운영 제어</div>
        <div class='control-grid'>
          <div class='control-item'>
            <form method='post' action='/start' id='startForm'><button class='control-btn start {'state-active state-running' if st.get('running') else ''}'>봇 시작</button></form>
            <div class='control-item-note'>자동매매 루프를 실행합니다.</div>
          </div>
          <div class='control-item'>
            <form method='post' action='/stop'><button class='control-btn stop {'state-active state-stopped' if not st.get('running') else ''}'>봇 중지</button></form>
            <div class='control-item-note'>자동매매 루프를 즉시 멈춥니다.</div>
          </div>
          <div class='control-item'>
            <form method='post' action='/mode-set'>
              <input type='hidden' name='mode' value='DRY'/>
              <button class='control-btn stop {'state-active state-dry' if settings.trade_mode != 'LIVE' else ''}'>모의투자 전환</button>
            </form>
            <div class='control-item-note'>가상 주문으로 안전하게 점검합니다.</div>
          </div>
          <div class='control-item'>
            <form method='post' action='/mode-set' id='liveModeForm'>
              <input type='hidden' name='mode' value='LIVE'/>
              <button class='control-btn start {'state-active state-live' if settings.trade_mode == 'LIVE' and settings.live_armed else ''}'>실거래 전환</button>
            </form>
            <div class='control-item-note'>실주문과 주문 허용을 함께 활성화합니다.</div>
          </div>
        </div>
      </div>
      <div class='control-section'>
        <div class='control-section-title'>화면 도구</div>
        <div class='control-grid utility'>
          <div class='control-item'>
            <button type='button' id='guideOpenBtn' class='control-btn refresh'>화면 가이드</button>
            <div class='control-item-note'>패널과 버튼 읽는 법을 봅니다.</div>
          </div>
          <div class='control-item'>
            <button type='button' id='helpOpenBtn' class='control-btn refresh'>전략 가이드</button>
            <div class='control-item-note'>선정, 진입, 청산 규칙을 확인합니다.</div>
          </div>
          <div class='control-item'>
            <button type='button' id='configOpenBtn' class='control-btn refresh'>설정</button>
            <div class='control-item-note'>연결과 리스크 설정을 수정합니다.</div>
          </div>
          <div class='control-item'>
            <form method='post' action='/diagnostics-run'>
              <button class='control-btn refresh'>빠른 진단</button>
            </form>
            <div class='control-item-note'>API와 환경 상태를 즉시 점검합니다.</div>
          </div>
          <div class='control-item'>
            <div class='control-stack'>
              <button type='button' id='nativeInstallBtn' class='control-btn start'>iPhone 앱 설치</button>
              <button type='button' id='installAppBtn' class='control-btn refresh'>홈 화면 설치</button>
              <button type='button' id='installGuideBtn' class='control-btn refresh'>설치 가이드</button>
            </div>
            <div class='control-item-note' id='installAppHint'>iPhone native 설치 링크가 있으면 바로 이동하고, 없으면 홈 화면 앱 설치를 안내합니다.</div>
          </div>
        </div>
      </div>
      <div class='control-section'>
        <div class='control-section-title'>자동 새로고침</div>
      <div class='auto-refresh'>
        <div class='left'>
          <label><input type='checkbox' id='autoRefreshEnabled' /> 자동 새로고침</label>
          <select id='autoRefreshSec'>
            <option value='10'>10초</option>
            <option value='30'>30초</option>
            <option value='60' selected>60초</option>
            <option value='120'>120초</option>
            <option value='300'>300초</option>
          </select>
        </div>
        <div class='countdown' id='autoRefreshCountdown'>꺼짐</div>
      </div>
      </div>
    </div>
  <div class='card cat-monitor span-1' id='ops-status' data-dashboard-tab='status'>
      <div class='section-head'><div><div class='section-title'>운영 상태</div><div class='v'>실행 및 리스크</div></div></div>
      <div class='ops-grid' style='margin-bottom:10px;'>
        <div class='ops-card'>
          <div class='section-title'>실시간 운영</div>
          <div class='v'>{trade_mode_state_display} / {live_armed_state_display}</div>
          <div class='k' style='margin-top:6px'>실행 {running_display} | 스레드 {thread_alive_display} | 데이터 {dashboard_data_state} / {freshness_sec:.1f}s</div>
        </div>
        <div class='ops-card'>
          <div class='section-title'>일일 선정 상태</div>
          <div class='v'>{daily_selection_status}</div>
          <div class='k' style='margin-top:6px'>선정 기준일 {daily_selection_day} | 장중 종목 교체 없음 | top1 우선 운용</div>
        </div>
        <div class='ops-card'>
          <div class='section-title'>리스크 한도</div>
          <div class='v'>{portfolio_heat_display}</div>
          <div class='k' style='margin-top:6px'>포트폴리오 히트 | 종목 손실 캡 {max_symbol_loss_display}{'' if max_symbol_loss_display == '집계 전' else '%'}</div>
        </div>
      </div>
      <div class='info-list'>
        <div class='info-row'><span>실행 / 스레드</span><strong>{running_display} / {thread_alive_display}</strong></div>
        <div class='info-row'><span>거래 모드 / 실거래 승인</span><strong>{trade_mode_state_display} / {live_armed_state_display}</strong></div>
        <div class='info-row'><span>현금 / 평가자산</span><strong>{cash_equity_display}</strong></div>
        <div class='info-row'><span>누적 손익</span><strong>{total_pnl_display}</strong></div>
        <div class='info-row'><span>포트폴리오 히트</span><strong>{portfolio_heat_display}</strong></div>
        <div class='info-row'><span>종목 손실 캡</span><strong>{max_symbol_loss_display}{'' if max_symbol_loss_display == '집계 전' else '%'}</strong></div>
        <div class='info-row'><span>주문 수 / 가동시간</span><strong>{order_uptime_display}</strong></div>
        <div class='info-row'><span>데이터 상태</span><strong>{dashboard_data_state} / {fallback_snapshot_updated_at} / {fallback_snapshot_count}건</strong></div>
        <div class='info-row'><span>데이터 해석</span><strong>{html.escape(dashboard_data_note)}</strong></div>
        <div class='info-row'><span>오늘 무거래 요약</span><strong>{no_trade_summary_text}</strong></div>
        <div class='info-row'><span>외인/기관 수급</span><strong>{html.escape(_display_text(st.get('market_flow_summary'), '집계 중'))}</strong></div>
        <div class='info-row'><span>VI 현황</span><strong>{html.escape(_display_text(st.get('vi_summary'), '집계 중'))}</strong></div>
        <div class='info-row'><span>오프닝 포커스</span><strong>{html.escape(_display_text(st.get('opening_focus_summary'), '집계 중'))}</strong></div>
        <div class='info-row'><span>장마감 오프닝 복기</span><strong>{html.escape(_display_text(st.get('opening_review_summary'), '집계 중'))}</strong></div>
        <div class='info-row'><span>최근 오류</span><strong>{html.escape(_display_text(st['last_error'], '이상 없음'))}</strong></div>
        <div class='info-row'><span>토큰 만료</span><strong>{token_expires_display}</strong></div>
      </div>
    </div>
  <div class='card cat-monitor span-1' id='today-detail-section' data-dashboard-tab='status'>
      <div class='section-head'><div><div class='section-title'>오늘 장 상세</div><div class='v'>{today_key} 기준</div></div></div>
      <div class='ops-grid' style='margin-bottom:10px;'>
        <div class='ops-card'>
          <div class='section-title'>오늘 체결 요약</div>
          <div class='v'>{today_trade_detail_text}</div>
          <div class='k' style='margin-top:6px'>{today_trade_detail_subtext}</div>
        </div>
        <div class='ops-card'>
          <div class='section-title'>오늘 체결 종목</div>
          <div class='v' style='font-size:14px'>{html.escape(today_trade_symbol_text)}</div>
          <div class='k' style='margin-top:6px'>무체결이어도 오늘 선정/차단/오프닝 판단은 아래에 유지됩니다.</div>
        </div>
        <div class='ops-card'>
          <div class='section-title'>일일 선정 상태</div>
          <div class='v'>{daily_selection_status}</div>
          <div class='k' style='margin-top:6px'>선정 기준일 {daily_selection_day} | top1 우선, top2~top3 보조 관찰</div>
        </div>
      </div>
      <div class='info-list'>
        <div class='info-row'><span>오늘 무거래 / 차단 요약</span><strong>{html.escape(_display_text(st.get('no_trade_summary'), '집계 중'))}</strong></div>
        <div class='info-row'><span>외인/기관 수급</span><strong>{html.escape(_display_text(st.get('market_flow_summary'), '집계 중'))}</strong></div>
        <div class='info-row'><span>VI 현황</span><strong>{html.escape(_display_text(st.get('vi_summary'), '집계 중'))}</strong></div>
        <div class='info-row'><span>오프닝 포커스</span><strong>{html.escape(_display_text(st.get('opening_focus_summary'), '집계 중'))}</strong></div>
        <div class='info-row'><span>시초 우선 관찰</span><strong>{html.escape(_display_text(st.get('opening_priority_summary'), '집계 중'))}</strong></div>
        <div class='info-row'><span>A급 오프닝 후보</span><strong>{html.escape(_display_text(st.get('opening_a_grade_summary'), '집계 중'))}</strong></div>
        <div class='info-row'><span>장마감 오프닝 복기</span><strong>{html.escape(_display_text(st.get('opening_review_summary'), '집계 중'))}</strong></div>
      </div>
      <div class='k' style='margin-top:10px'>오늘 상위 차단 사유</div>
      <div class='block-card-list' style='margin-top:6px'>
        {reason_hist_cards if reason_hist_cards else "<div class='k'>차단 사유를 집계 중입니다.</div>"}
      </div>
      <div class='k' style='margin-top:10px'>오늘 상위 후보 3선</div>
      <div class='rank-card-list' style='margin-top:6px'>
        {today_top_candidate_cards if today_top_candidate_cards else "<div class='k'>오늘 상위 후보를 집계 중입니다.</div>"}
      </div>
    </div>
</div>
<div class='pro-grid'>
    <div class='card cat-selection span-1' id='focus-section' data-dashboard-tab='stocks' data-stocks-tab='focus'>
      <div class='section-head'><div><div class='section-title'>오늘의 포커스</div><div class='v'>top1 선정 종목 또는 방어형 감시 후보</div></div></div>
      <div class='focus-list'>
        {monitored_symbols_chips if monitored_symbols_chips else "<span class='k'>선정 또는 감시 후보를 집계 중입니다.</span>"}
      </div>
      <div class='mini-grid' style='margin-top:12px'>
        <div class='card'>
          <div class='k'>선정 기준</div>
          <div class='v' style='font-size:16px'>{selection_ref}</div>
          <div class='k' style='margin-top:6px'>일일 선정: {daily_selection_status} | 실전 해석: top1 우선, 약세장에서는 감시 후보 중심</div>
          <div class='k' style='margin-top:6px'>보유 요약: {positions_summary_display}</div>
        </div>
        <div class='card'>
          <div class='k'>선정 이력 / 교체율</div>
          <div class='v' style='font-size:16px'>전일 대비 교체율 {selection_turnover_pct:.1f}%</div>
          <div class='k' style='margin-top:6px'>{selection_turnover_note}</div>
          <div class='block-card-grid' style='margin-top:8px'>{_selection_history_cards_html(selection_history_stats, name_map)}</div>
        </div>
      </div>
      <div class='mini-grid' style='margin-top:12px'>
        <div class='card'>
          <div class='k'>시초 우선 관찰</div>
          <div class='v' style='font-size:14px'>{html.escape(_display_text(st.get('opening_priority_summary'), '집계 중'))}</div>
          <div class='k' style='margin-top:6px'>외인/기관 동시 순매수 + 추격 금지 아님</div>
          <div style='margin-top:8px'><button type='button' class='evt-btn' data-stock-quick-filter='priority'>보드에서 바로 보기</button></div>
        </div>
        <div class='card'>
          <div class='k'>A급 오프닝 후보</div>
          <div class='v' style='font-size:14px'>{html.escape(_display_text(st.get('opening_a_grade_summary'), '집계 중'))}</div>
          <div class='k' style='margin-top:6px'>동시 순매수 + VI 미발동 + 추격 금지 아님</div>
          <div style='margin-top:8px'><button type='button' class='evt-btn' data-stock-quick-filter='a_grade'>보드에서 바로 보기</button></div>
        </div>
      </div>
      <div class='section-head' style='margin-top:12px'><div><div class='section-title'>선정 근거 비교</div><div class='v'>top1 품질과 top2~top3 보조 후보 비교</div></div></div>
      <div class='rank-card-list'>
        {selection_rank_cards if selection_rank_cards else "<div class='k'>상위 후보를 집계 중입니다.</div>"}
      </div>
    </div>
    <div class='card cat-selection span-1' id='board-section' data-dashboard-tab='stocks' data-stocks-tab='board'>
      <div class='section-head'><div><div class='section-title'>종목 실행 보드</div><div class='v'>종목별 실시간 상태</div></div><div class='k'>추세, 구조, 돌파, 과열 여부와 액션을 함께 표시합니다.</div></div>
      <div class='sim-toolbar' style='margin-top:0'>
        <input id='stockBoardSearch' type='text' placeholder='종목/사유 검색' />
        <div class='k'>현재 보드에서 종목명, 섹터, 액션, 차단 사유로 바로 검색할 수 있습니다.</div>
      </div>
      <div class='perf-tabs'>
        <button type='button' class='perf-tab active' data-stock-tab='candidate'>{board_primary_title} {board_primary_count}</button>
        <button type='button' class='perf-tab' data-stock-tab='holding'>보유 종목 {stock_holding_count}</button>
        <button type='button' class='perf-tab' data-stock-tab='blocked'>차단 종목 {stock_blocked_count}</button>
      </div>
      <div class='perf-panel active' data-stock-panel='candidate'>
        <div class='stock-grid'>
          {board_primary_cards if board_primary_cards else f"<div class='k'>{board_primary_empty}</div>"}
        </div>
      </div>
      <div class='perf-panel' data-stock-panel='holding'>
        <div class='stock-grid'>
          {stock_holding_cards if stock_holding_cards else "<div class='k'>현재 보유 종목이 없습니다.</div>"}
        </div>
      </div>
      <div class='perf-panel' data-stock-panel='blocked'>
        <div class='stock-grid'>
          {stock_blocked_cards if stock_blocked_cards else "<div class='k'>차단 종목을 집계 중입니다.</div>"}
        </div>
      </div>
    </div>
    <div class='card cat-monitor span-1' id='ops-monitor' data-dashboard-tab='status'>
      <div class='section-head'><div><div class='section-title'>운영 점검</div><div class='v'>알림과 최근 진단</div></div></div>
      <div class='ops-grid'>
        <div class='ops-card'>
          <div class='section-title'>알림</div>
          <div id='alertCenter' style='display:grid;gap:6px'>
            {alerts_html}
          </div>
          <div style='margin-top:8px;display:flex;justify-content:flex-end'>
            <button type='button' class='stop' id='alertResetBtn'>알림 다시 보기</button>
          </div>
        </div>
        <div class='ops-card'>
          <div class='section-title'>최근 진단</div>
          <div class='v' style='font-size:14px;margin-top:2px'>{html.escape(_display_text(diag.get('summary'), '진단 결과 집계 중'))}</div>
          <div class='k'>업데이트: {html.escape(_display_text(diag.get('updated_at'), '집계 전'))} | 상태: {'정상' if diag.get('ok') else '확인 필요'}</div>
          <div style='overflow-x:auto;margin-top:6px;'>
            <table>
              <thead><tr><th>항목</th><th>결과</th><th>상세</th></tr></thead>
              <tbody>{"".join(f"<tr><td>{html.escape(str(x.get('name')))}</td><td>{'OK' if x.get('ok') else 'FAIL'}</td><td>{html.escape(str(x.get('detail')))}</td></tr>" for x in (diag.get('checks') or [])[:6])}</tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
    <div class='card cat-market span-1' id='market-section' data-dashboard-tab='stocks' data-stocks-tab='market'>
      <div class='section-head'>
        <div><div class='section-title'>시장 컨텍스트</div><div class='v'>시장 흐름과 오프라인 히스토리</div></div>
        <div style='display:flex;align-items:center;gap:10px;flex-wrap:wrap'>
          <div class='range-tabs'>{market_range_tabs}</div>
          <div class='k'>현재 범위 {html.escape({"1d":"1일","1w":"1주","1m":"1개월","all":"전체"}.get(market_range, "1주"))} | {len(hist_view)}개 스냅샷</div>
        </div>
      </div>
      <div class='chart-grid'>
        <div class='card chart-card'><div class='chart-label'>지수 추세 + 이동평균 ({html.escape(_display_text(mk.get('index_name'), '시장 지수'))})</div>{price_ma_chart}</div>
        <div class='card chart-card'><div class='chart-label'>KOSPI / KOSDAQ 비교선</div>{market_compare_chart}</div>
        <div class='card chart-card'><div class='chart-label'>시장 폭(Breadth) 추세</div>{breadth_chart}</div>
        <div class='card chart-card'><div class='chart-label'>시장 RSI(14)</div>{rsi_chart}</div>
        <div class='card chart-card'><div class='chart-label'>시장 MACD</div>{macd_chart}</div>
      </div>
      <div class='mini-grid' style='margin-top:10px'>
        <div class='card'>
          <div class='section-title'>오프라인 누적 상태</div>
          <div class='v'>{len(hist)}</div>
          <div class='k'>누적 스냅샷 수 | 과거 지수 기반 백필 포함</div>
        </div>
        <div class='card'>
          <div class='section-title'>시장 심리 추세</div>
          <div class='v'>{_to_float(sentiment_series[-1] if sentiment_series else 0):.1f}</div>
          <div class='k'>최근 심리 점수 | 강세 67 이상 / 약세 33 이하</div>
        </div>
      </div>
      <div class='k' style='margin-top:10px'>{market_range_summary}</div>
      <div class='k' style='margin-top:10px'>최근 시장 상태 기록</div>
      <div style='overflow-x:auto;margin-top:6px;'>
        <table class='tight-table'>
          <thead><tr><th>시각</th><th>지수</th><th>등락</th><th>상승비율</th><th>심리점수</th><th>상태</th></tr></thead>
          <tbody>{market_history_rows if market_history_rows else "<tr><td colspan='6'>시장 상태 기록을 집계 중입니다.</td></tr>"}</tbody>
        </table>
      </div>
    </div>
    <div class='card cat-performance span-1 performance-compare-card' id='performance-section' data-dashboard-tab='performance'>
      <div class='section-head'><div><div class='section-title'>거래 성과</div><div class='v'>실거래와 시뮬레이션 비교</div></div></div>
      <div class='mini-grid' style='margin-top:10px'>
        <div class='card'>
          <div class='section-title'>실거래 요약</div>
          <div class='v' style='font-size:15px'>실제 체결 결과</div>
          <div class='k'>ledger 기준 SELL 체결과 실현손익을 복기합니다.</div>
        </div>
        <div class='card'>
          <div class='section-title'>시뮬레이션 요약</div>
          <div class='v' style='font-size:15px'>{sim_report_type_label}</div>
          <div class='k'>{sim_report_desc}</div>
        </div>
      </div>
      <div class='metric-summary-grid' id='compare-summary-section' style='margin-top:10px'>
        {live_vs_sim_cards}
      </div>
      <div class='card' style='margin-top:10px'>
        <div class='section-title'>전략 비교 요약</div>
        <div class='ops-card-grid' style='margin-top:10px'>
          <div class='ops-card'><div class='section-title'>선별 차이</div><div class='v' style='font-size:14px'>실거래 vs 시뮬레이션</div><div class='k'>{strategy_compare_selection}</div></div>
          <div class='ops-card'><div class='section-title'>진입 차이</div><div class='v' style='font-size:14px'>체결 제약 반영 여부</div><div class='k'>{strategy_compare_entry}</div></div>
          <div class='ops-card'><div class='section-title'>판단 주기 차이</div><div class='v' style='font-size:14px'>얼마나 자주 다시 보나</div><div class='k'>{strategy_compare_cadence}</div></div>
          <div class='ops-card'><div class='section-title'>청산 차이</div><div class='v' style='font-size:14px'>리스크 관리 강도</div><div class='k'>{strategy_compare_exit}</div></div>
          <div class='ops-card'><div class='section-title'>해석 포인트</div><div class='v' style='font-size:14px'>비교할 때 볼 점</div><div class='k'>{strategy_compare_exec}</div></div>
        </div>
      </div>
      {delta_alert_html}
      <div class='card' style='margin-top:10px'>
        <div class='section-title'>비교 해석</div>
        <div class='v' style='font-size:14px'>{html.escape(live_vs_sim_comment)}</div>
        <div class='k' style='margin-top:6px'><strong>{html.escape(live_vs_sim_action)}</strong></div>
        <div class='k' style='margin-top:6px'>실거래/시뮬레이션 차이는 체결, 장중 필터, 보유기간, 리스크 가드 적용 차이에서 주로 발생합니다. 두 컬럼은 서로 독립적으로 보시면 됩니다.</div>
      </div>
      <div class='card focus-compare-card' id='tradeFocusCompareCard' style='margin-top:10px;display:none;'>
        <div class='focus-compare-head'>
          <div>
            <div class='section-title'>현재 비교 종목</div>
            <div class='v' style='font-size:14px'>같은 종목의 실거래와 시뮬레이션만 좁혀서 비교합니다.</div>
          </div>
          <div style='display:flex;gap:8px;align-items:center;flex-wrap:wrap;'>
            <span class='focus-compare-badge' id='tradeFocusSymbolBadge'>-</span>
            <button type='button' class='trade-clear-btn' id='tradeFocusClearBtn'>비교 해제</button>
          </div>
        </div>
        <div class='focus-compare-grid'>
          <div class='focus-compare-panel live'>
            <div class='focus-compare-title'>LIVE <span class='section-badge live'>실거래</span></div>
            <div class='focus-compare-stats' id='tradeFocusLiveStats'></div>
            <div class='focus-compare-chart'>
              <div id='tradeFocusLiveChart'></div>
              <div class='k' id='tradeFocusLiveNote'>실거래 수익률 흐름을 집계합니다.</div>
            </div>
          </div>
          <div class='focus-compare-panel sim'>
            <div class='focus-compare-title'>SIM <span class='section-badge sim'>시뮬레이션</span></div>
            <div class='focus-compare-stats' id='tradeFocusSimStats'></div>
            <div class='focus-compare-chart'>
              <div id='tradeFocusSimChart'></div>
              <div class='k' id='tradeFocusSimNote'>시뮬레이션 수익률 흐름을 집계합니다.</div>
            </div>
          </div>
        </div>
      </div>
    </div>
    <div class='card cat-performance span-1' id='performance-live-section' data-dashboard-tab='performance'>
      <div class='perf-panel active performance-scroll-panel' id='performanceLivePanel' data-perf-panel='live' style='display:block'>
        <div class='section-head' style='margin-top:10px'><div><div class='section-title-row'><div class='section-title' style='margin-bottom:0'>실거래 이력</div><span class='section-badge live'>LIVE</span></div><div class='v'>실제 체결 결과</div></div></div>
        <div class='ops-card-grid'>
          <div class='ops-card'><div class='section-title'>거래 수</div><div class='v'>{live_trade_count}</div><div class='k'>닫힌 실거래 기준</div></div>
          <div class='ops-card'><div class='section-title'>승률</div><div class='v'>{live_win_rate:.1f}%</div><div class='k'>승리 {live_win_count}건</div></div>
          <div class='ops-card'><div class='section-title'>누적 실현손익</div><div class='v'>{live_total_pnl:,.0f}</div><div class='k'>ledger 기준 합산</div></div>
          <div class='ops-card'><div class='section-title'>마지막 청산</div><div class='v' style='font-size:14px'>{live_updated_at}</div><div class='k'>최근 SELL 체결 시각</div></div>
        </div>
        <div class='mini-grid' style='margin-top:10px'>
          <div class='card'>
            <div class='section-title'>현재 실전 전략 설명</div>
            <div class='k' style='margin-top:6px'>{live_strategy_compare_text}</div>
            <div class='ops-card-grid' style='margin-top:10px'>
              <div class='ops-card'><div class='section-title'>선별 전략</div><div class='v' style='font-size:14px'>{live_selection_style_text}</div><div class='k'>{live_selection_style_detail}</div></div>
              <div class='ops-card'><div class='section-title'>운용 폭</div><div class='v' style='font-size:14px'>{live_capacity_style_text}</div><div class='k'>{live_capacity_style_detail}</div></div>
              <div class='ops-card'><div class='section-title'>현재 판단 현황</div><div class='v' style='font-size:14px'>장중 후보/차단 요약</div><div class='k'>{live_decision_activity_detail}</div></div>
              <div class='ops-card'><div class='section-title'>진입 전략</div><div class='v' style='font-size:14px'>{live_entry_style_text}</div><div class='k'>{live_entry_style_detail}</div></div>
              <div class='ops-card'><div class='section-title'>판단 주기</div><div class='v' style='font-size:14px'>{live_cadence_style_text}</div><div class='k'>{live_cadence_style_detail}</div></div>
              <div class='ops-card'><div class='section-title'>청산 원칙</div><div class='v' style='font-size:14px'>{live_exit_style_text}</div><div class='k'>{live_exit_style_detail}</div></div>
              <div class='ops-card'><div class='section-title'>실행 규칙</div><div class='v' style='font-size:14px'>{live_exec_style_text}</div><div class='k'>{live_exec_style_detail}</div></div>
            </div>
          </div>
          <div class='card'>
            <div class='section-title'>장마감 오프닝 복기</div>
            <div class='v' style='font-size:14px'>{html.escape(_display_text(st.get('opening_review_summary'), '집계 중'))}</div>
            <div class='k' style='margin-top:6px'>A급 후보와 시초 우선 관찰 후보의 체결/청산 결과</div>
          </div>
          <div class='card'>
            <div class='section-title'>A급 진입 통계</div>
            <div class='v' style='font-size:14px'>거래 {a_grade_trade_count}건 | 승률 {a_grade_win_rate:.1f}%</div>
            <div class='k' style='margin-top:6px'>누적손익 {a_grade_total_pnl:+,.0f} | 평균수익률 {a_grade_avg_return:+.2f}%</div>
          </div>
        </div>
        <div class='metric-summary-grid' data-perf-anchor='stats' style='margin-top:10px'>
          <div class='metric-summary-card'><div class='section-title'>A급 승률</div><div class='v'>{a_grade_win_rate:.1f}%</div><div class='k'>거래 {a_grade_trade_count}건</div></div>
          <div class='metric-summary-card'><div class='section-title'>일반 승률</div><div class='v'>{regular_win_rate:.1f}%</div><div class='k'>거래 {regular_trade_count}건</div></div>
          <div class='metric-summary-card'><div class='section-title'>A급 누적손익</div><div class='v'>{a_grade_total_pnl:+,.0f}</div><div class='k'>평균수익률 {a_grade_avg_return:+.2f}%</div></div>
          <div class='metric-summary-card'><div class='section-title'>일반 누적손익</div><div class='v'>{regular_total_pnl:+,.0f}</div><div class='k'>평균수익률 {regular_avg_return:+.2f}%</div></div>
        </div>
        <div style='overflow-x:auto;margin-top:10px;'>
          <table class='tight-table'>
            <thead><tr><th>일자</th><th>오프닝 복기</th><th>거래수</th><th>실현손익</th><th>승률</th></tr></thead>
            <tbody>{opening_review_table_rows if opening_review_table_rows else "<tr><td colspan='5'>오프닝 복기 히스토리가 아직 없습니다.</td></tr>"}</tbody>
          </table>
        </div>
        <div class='chart-grid' data-perf-anchor='charts' style='margin-top:10px'>
          <div class='card chart-card'><div class='chart-label'>오프닝 후보 적중률 추세</div>{opening_review_win_chart}</div>
          <div class='card chart-card'><div class='chart-label'>오프닝 후보 실현손익 추세</div>{opening_review_pnl_chart}</div>
        </div>
        <div class='metric-summary-grid' data-perf-anchor='stats'>
          <div class='metric-summary-card'><div class='section-title'>평균 수익 거래</div><div class='v'>{live_stats['avg_win']:,.0f}</div><div class='k'>평균 이익</div></div>
          <div class='metric-summary-card'><div class='section-title'>평균 손실 거래</div><div class='v'>{live_stats['avg_loss']:,.0f}</div><div class='k'>평균 손실</div></div>
          <div class='metric-summary-card'><div class='section-title'>손익비</div><div class='v'>{live_stats['payoff']:.2f}</div><div class='k'>평균 이익 / 손실</div></div>
          <div class='metric-summary-card'><div class='section-title'>Profit Factor</div><div class='v'>{live_stats['profit_factor']:.2f}</div><div class='k'>총이익 / 총손실</div></div>
          <div class='metric-summary-card'><div class='section-title'>Expectancy</div><div class='v'>{live_stats['expectancy']:,.0f}</div><div class='k'>거래당 기대손익</div></div>
        </div>
        <div class='chart-grid' data-perf-anchor='charts' style='margin-top:10px'>
          <div class='card chart-card'><div class='chart-label'>라이브 평가자산 곡선</div>{live_equity_chart}</div>
          <div class='card chart-card'><div class='chart-label'>라이브 드로다운</div>{live_drawdown_chart}</div>
          <div class='card chart-card'><div class='chart-label'>거래별 실현손익</div>{live_trade_pnl_chart}</div>
          <div class='card chart-card'><div class='chart-label'>종목별 손익 기여도</div>{live_symbol_contribution_chart}</div>
          <div class='card chart-card'><div class='chart-label'>보유기간별 성과</div>{live_hold_profile_chart}</div>
          <div class='card chart-card'><div class='chart-label'>요일별 성과 히트맵</div>{live_weekday_heatmap}<div class='k' style='margin-top:8px'>청산 요일 기준 평균 수익률</div></div>
          <div class='card chart-card'><div class='chart-label'>선정 종목 신호 타임라인</div>{selected_intraday_chart.get('chart') if bool(selected_intraday_chart.get('available')) else "<div class='k'>장중에 저장된 선정 종목 타임라인이 아직 충분하지 않습니다.</div>"}<div class='k' style='margin-top:8px'>{html.escape(str(selected_intraday_chart.get('summary') or '선정 종목의 2분 타임라인과 BUY/SELL 신호 변화를 표시합니다.'))}</div></div>
        </div>
        <div class='rank-meta' style='margin-top:8px'>
          {live_top_cards if live_top_cards else "<div class='k'>실거래 체결 이력이 아직 없습니다.</div>"}
        </div>
        <div class='k' data-perf-anchor='trades' style='margin-top:10px'>최근 닫힌 거래</div>
        <div class='sim-toolbar'>
          <select id='liveResultFilter'>
            <option value='all'>전체 결과</option>
            <option value='win'>수익 거래</option>
            <option value='loss'>손실 거래</option>
          </select>
          <select id='liveSortOrder'>
            <option value='sell_desc'>청산 최신순</option>
            <option value='sell_asc'>청산 오래된순</option>
            <option value='return_desc'>수익률 높은순</option>
            <option value='return_asc'>수익률 낮은순</option>
          </select>
          <input id='liveTradeSearch' type='text' placeholder='종목 검색' />
          <button type='button' class='trade-clear-btn' id='liveTradeClearBtn'>필터 해제</button>
          <div class='k'>실거래 SELL 체결을 기준으로 묶어 보여줍니다.</div>
        </div>
        <div style='overflow-x:auto;margin-top:6px;'>
          <table id='liveTradeTable'>
            <thead><tr><th>종목</th><th>매수시각</th><th>매도시각</th><th>매수가</th><th>매도가</th><th>수량</th><th>수익률</th><th>실현손익</th><th>보유</th><th>진입유형</th></tr></thead>
            <tbody>{live_trade_rows if live_trade_rows else "<tr><td colspan='10'>실거래 닫힌 거래가 아직 없습니다.</td></tr>"}</tbody>
          </table>
        </div>
      </div>
    </div>
    <div class='card cat-performance span-1' id='performance-sim-section' data-dashboard-tab='performance'>
      <div class='perf-panel active performance-scroll-panel' id='performanceSimPanel' data-perf-panel='sim' style='display:block'>
        <div class='section-head'><div><div class='section-title-row'><div class='section-title' style='margin-bottom:0'>시뮬레이션 결과</div><span class='section-badge sim'>SIM</span></div><div class='v'>실거래와 같은 화면에서 비교</div></div></div>
        <div class='mini-grid'>
          <div class='card'>
            <div class='section-title'>마지막 실행 결과 설명</div>
            <div class='v' style='font-size:15px' id='simPreviewLabel'>{sim_report_type_label}</div>
            <div class='k' style='margin-top:6px' id='simPreviewDesc'>{sim_report_desc}</div>
            <div class='k' style='margin-top:8px'>왜 이 전략을 보나: <strong id='simPreviewWhy'>{html.escape(sim_why_text)}</strong></div>
            <div class='k' style='margin-top:10px'>적용 전략: <strong id='simPreviewStrategy'>{sim_strategy_label}</strong></div>
            <div class='k'>기간: <strong>{html.escape(sim_period_text)}</strong> | 유니버스: <strong>{html.escape(sim_universe_text)}</strong></div>
            <div class='k'>데이터 기준: <strong>{html.escape(sim_data_fetch_text)}</strong> | 소스: <strong>KR Yahoo 일봉 / US Stooq 일봉 + 서버 캐시</strong></div>
            <div class='k'>마지막 실행 프로필: <strong>{html.escape(str(sim_profile_meta.get("label") or result_sim_profile))}</strong></div>
            <div class='k'>{'실행 기준: ' if result_sim_profile == 'short_term' else '전략 세부: '}<strong>{html.escape(sim_param_text) if result_sim_profile == 'short_term' else sim_strategy_detail}</strong></div>
            <div class='k'>{'핵심 팩터: ' if result_sim_profile == 'short_term' else '랭킹 팩터: '}<strong>{html.escape(sim_ranking_text)}</strong></div>
            <div class='k'>리스크 가드: <strong>{html.escape(sim_guard_text)}</strong></div>
            <div class='k'>2분 선택종목 데이터 상태: {intraday_selected_status_badge}</div>
            <div class='k'>{html.escape(sim_intraday_data_text)}</div>
            <div class='k'>{html.escape(intraday_selected_status_note)}</div>
            <div class='k'>리포트 파일: <strong id='simPreviewReportPath'>{html.escape(str(sim_profile_meta.get("report_path") or "data/short_term_trade_report_top100.json"))}</strong></div>
            <div class='k'>현재 리포트 기준 시각: <strong id='simPreviewRequestedAt'>{html.escape(str(sim_profile_prefs.get("requested_at") or "기본값 기준"))}</strong></div>
            <div class='k'>마지막 실행: <strong>{sim_updated_at}</strong> | 상태: <strong>{html.escape(sim_run_status)}</strong></div>
            <div class='k' style='margin-top:4px'>{html.escape(sim_status_note)}</div>
            <div class='k' style='margin-top:6px' id='simPreviewScope'>{html.escape(_simulation_profile_scope_text(result_sim_profile))}</div>
            <div class='k' style='margin-top:6px'>왼쪽 실행 설정은 프로필을 바꾸면 즉시 바뀌고, 이 카드는 마지막으로 실제 실행된 시뮬레이션 결과를 계속 보여줍니다.</div>
            <div class='k' style='margin-top:6px'>버튼은 `시뮬레이션 실행` 하나만 사용하면 됩니다.</div>
            {f"<div class='k' style='color:#fca5a5'>실패 사유: {html.escape(simulation_reason)}</div>" if simulation_state == "failed" and simulation_reason else ""}
          </div>
          <div class='card'>
            <div class='section-title' id='simFormTitle'>{html.escape(_simulation_profile_form_title(selected_sim_profile))}</div>
            <form method='post' action='/simulation-run' id='simulationRunForm'>
              <div class='sim-toolbar' style='margin-top:8px'>
                <div class='sim-field' data-sim-scope='all'>
                  <select name='profile' id='simulationProfileSelect'>
                    <option value='short_term' {'selected' if selected_sim_profile == 'short_term' else ''}>단기 종목 리포트</option>
                    <option value='rolling_rank' {'selected' if selected_sim_profile == 'rolling_rank' else ''}>롤링 랭킹 스터디</option>
                    <option value='short_horizon' {'selected' if selected_sim_profile == 'short_horizon' else ''}>초단기 랭킹 스터디</option>
                    <option value='daily_selection' {'selected' if selected_sim_profile == 'daily_selection' else ''}>일일 재선정 포트폴리오</option>
                    <option value='rank_weighted' {'selected' if selected_sim_profile == 'rank_weighted' else ''}>랭크 가중 포트폴리오</option>
                    <option value='intraday_replay' {'selected' if selected_sim_profile == 'intraday_replay' else ''}>선정 종목 2분 리플레이</option>
                  </select>
                </div>
                <div class='sim-field' data-sim-scope='all'><input type='number' min='50' max='4000' step='50' name='seed_n' value='{html.escape(sim_form_seed_n)}' placeholder='seed 종목 수' data-sim-pref='seed_n' /></div>
                <div class='sim-field' data-sim-scope='short_term'><input type='number' min='1' max='2000' step='1' name='top_n' value='{html.escape(sim_form_top_n)}' placeholder='top 종목 수' data-sim-pref='top_n' /></div>
                <div class='sim-field' data-sim-scope='rolling_rank short_horizon daily_selection rank_weighted intraday_replay'><input type='number' min='5' max='240' step='1' name='window_days' value='{html.escape(sim_form_window_days)}' placeholder='기간(거래일)' data-sim-pref='window_days' /></div>
                <div class='sim-field' data-sim-scope='all'><input type='number' min='60' max='1000' step='10' name='data_fetch_limit' value='{html.escape(sim_form_data_fetch_limit)}' placeholder='데이터 기간(일)' data-sim-pref='data_fetch_limit' /></div>
                <div class='sim-field' data-sim-scope='short_horizon daily_selection'><input type='number' min='1' max='10' step='1' name='max_hold_days' value='{html.escape(sim_form_max_hold_days)}' placeholder='최대보유일' data-sim-pref='max_hold_days' /></div>
                <div class='sim-field' data-sim-scope='intraday_replay'>
                  <select name='target_day' data-sim-pref='target_day'>
                    <option value=''>저장된 일자 선택</option>
                    {intraday_day_options_html}
                  </select>
                </div>
              </div>
              <div class='sim-toolbar' style='margin-top:8px'>
                <div class='sim-field' data-sim-scope='rank_weighted'><input type='text' name='rank_weights' value='{html.escape(sim_form_rank_weights)}' placeholder='가중치 예: 0.5,0.3,0.2' data-sim-pref='rank_weights' /></div>
                <div class='sim-field' data-sim-scope='daily_selection'><label class='chip'><input type='checkbox' name='relaxed_selected_entry' {sim_form_relaxed_checked} data-sim-pref='relaxed_selected_entry'/> selected+entry_ready 완화</label></div>
                <div class='sim-field' data-sim-scope='daily_selection'><label class='chip'><input type='checkbox' name='selected_continuation_probe' {sim_form_probe_checked} data-sim-pref='selected_continuation_probe'/> continuation probe</label></div>
                <button class='control-btn refresh' id='simulationRunBtn'>시뮬레이션 실행</button>
              </div>
              <div class='sim-toolbar' style='margin-top:8px'>
                <input type='number' min='0' max='100' step='0.5' name='compare_warn_win_rate_gap_pct' value='{html.escape(sim_form_warn_win_gap)}' placeholder='승률 경고 Δ(%p)' data-sim-pref='compare_warn_win_rate_gap_pct' />
                <input type='number' min='0' max='100000000' step='1000' name='compare_warn_pnl_gap_krw' value='{html.escape(sim_form_warn_pnl_gap)}' placeholder='손익 경고 Δ(원)' data-sim-pref='compare_warn_pnl_gap_krw' />
                <input type='number' min='0' max='100000000' step='1000' name='compare_warn_expectancy_gap_krw' value='{html.escape(sim_form_warn_expectancy_gap)}' placeholder='Expectancy 경고 Δ(원)' data-sim-pref='compare_warn_expectancy_gap_krw' />
                <input type='number' min='0' max='30' step='0.1' name='compare_warn_hold_gap_days' value='{html.escape(sim_form_warn_hold_gap)}' placeholder='보유일 경고 Δ' data-sim-pref='compare_warn_hold_gap_days' />
              </div>
              <div class='k' style='margin-top:8px' id='simFormHint'>{html.escape(_simulation_profile_form_hint(selected_sim_profile))}</div>
              <div class='k' style='margin-top:8px'>위 입력칸의 값이 그대로 이번 시뮬레이션 실행값으로 사용됩니다.</div>
            </form>
            <div class='k' style='margin-top:8px'>데이터가 시뮬레이션의 핵심입니다. 선택한 데이터 기간만큼 캐시에 없으면 서버가 자동으로 가져와 저장한 뒤 실행합니다.</div>
          </div>
        </div>
        <div class='ops-card-grid' style='margin-top:10px'>
          <div class='k' id='simStrategyCardHint' style='grid-column:1 / -1;'>현재 프로필의 선별/진입 구조를 아래 카드에서 확인할 수 있습니다.</div>
          {sim_strategy_cards}
        </div>
        <div class='ops-card-grid'>
          <div class='k' id='simExperimentCardHint' style='grid-column:1 / -1;'>현재 프로필에서 가장 먼저 볼 실험 포인트를 아래 카드에 정리합니다.</div>
          {sim_experiment_cards}
        </div>
        <div class='card' style='margin-top:10px'>
          <div class='section-title'>시뮬레이션 핵심 결과</div>
          <div class='v' style='font-size:14px'>{sim_report_type_label} · {sim_updated_at}</div>
          <div class='k' style='margin-top:6px'>{html.escape(sim_result_visibility_note)}</div>
          <div class='k' style='margin-top:6px'>2분 선택 데이터: {intraday_selected_status_badge}</div>
          <div class='k' style='margin-top:6px'>{html.escape(intraday_selected_status_note)}</div>
          <div class='mini-grid' style='margin-top:10px'>
            <div class='card'>
              <div class='section-title'>체결 수</div>
              <div class='v'>{sim_trade_count}</div>
              <div class='k'>닫힌 거래 기준</div>
            </div>
            <div class='card'>
              <div class='section-title'>종목 요약 수</div>
              <div class='v'>{len(sim_summary_rows)}</div>
              <div class='k'>{'summary_rows 기준' if result_sim_profile == 'intraday_replay' else 'summary_by_symbol 기준'}</div>
            </div>
            <div class='card'>
              <div class='section-title'>리포트 파일</div>
              <div class='v' style='font-size:13px'>{html.escape(str(sim_profile_meta.get("report_path") or "data/short_term_trade_report_top100.json"))}</div>
              <div class='k'>현재 보고 있는 결과 파일</div>
            </div>
          </div>
        </div>
        <div class='metric-summary-grid'>
          <div class='metric-summary-card'><div class='section-title'>평균 수익 거래</div><div class='v'>{sim_stats['avg_win']:,.0f}</div><div class='k'>평균 이익</div></div>
          <div class='metric-summary-card'><div class='section-title'>평균 손실 거래</div><div class='v'>{sim_stats['avg_loss']:,.0f}</div><div class='k'>평균 손실</div></div>
          <div class='metric-summary-card'><div class='section-title'>손익비</div><div class='v'>{sim_stats['payoff']:.2f}</div><div class='k'>평균 이익 / 손실</div></div>
          <div class='metric-summary-card'><div class='section-title'>Profit Factor</div><div class='v'>{sim_stats['profit_factor']:.2f}</div><div class='k'>총이익 / 총손실</div></div>
          <div class='metric-summary-card'><div class='section-title'>Expectancy</div><div class='v'>{sim_stats['expectancy']:,.0f}</div><div class='k'>거래당 기대손익</div></div>
        </div>
        <div class='chart-grid' style='margin-top:10px'>
          <div class='card chart-card'><div class='chart-label'>시뮬레이션 누적 손익곡선</div>{sim_cumulative_chart}</div>
          <div class='card chart-card'><div class='chart-label'>시뮬레이션 드로다운</div>{sim_drawdown_chart}</div>
          <div class='card chart-card'><div class='chart-label'>거래별 실현손익</div>{sim_trade_pnl_chart}</div>
          <div class='card chart-card'><div class='chart-label'>종목별 손익 기여도</div>{sim_symbol_contribution_chart}</div>
          <div class='card chart-card'><div class='chart-label'>보유기간별 성과</div>{sim_hold_profile_chart}</div>
          <div class='card chart-card'><div class='chart-label'>요일별 성과 히트맵</div>{sim_weekday_heatmap}<div class='k' style='margin-top:8px'>청산 요일 기준 평균 수익률</div></div>
          <div class='card chart-card'><div class='chart-label'>선정 종목 2분 리플레이</div>{selected_intraday_chart.get('chart') if bool(selected_intraday_chart.get('available')) else "<div class='k'>리플레이용 2분 데이터가 아직 충분하지 않습니다.</div>"}<div class='k' style='margin-top:8px'>{html.escape(str(selected_intraday_chart.get('summary') or '실전에서 저장된 선정 종목 2분 데이터를 기다리는 중입니다.'))}</div></div>
        </div>
        <div class='k' style='margin-top:8px'>리포트 파일: <code id='simReportPathInline'>{html.escape(str(sim_profile_meta.get("report_path") or "data/short_term_trade_report_top100.json"))}</code></div>
        <div class='rank-meta' style='margin-top:8px'>
          {sim_top_cards if sim_top_cards else "<div class='k'>시뮬레이션 리포트를 아직 찾지 못했습니다.</div>"}
        </div>
        {(
          "<div style='overflow-x:auto;margin-top:10px;'><table class='tight-table'>"
          "<thead><tr><th>종목</th><th>일수</th><th>평균 리턴</th><th>누적 기여</th></tr></thead>"
          f"<tbody>{sim_intraday_symbol_rows if sim_intraday_symbol_rows else '<tr><td colspan=\"4\">선택한 하루의 종목별 리플레이 결과가 아직 없습니다.</td></tr>'}</tbody>"
          "</table><div class='k' style='margin-top:6px'>선택한 하루 기준 종목별 intraday replay 요약</div></div>"
        ) if result_sim_profile == 'intraday_replay' else ""}
        <div class='k' data-perf-anchor='trades' style='margin-top:10px'>최근 체결 요약</div>
        <div class='sim-toolbar'>
          <select id='simResultFilter'>
            <option value='all'>전체 결과</option>
            <option value='win'>승리만</option>
            <option value='loss'>손실만</option>
          </select>
          <select id='simSortOrder'>
            <option value='sell_desc'>매도일 최신순</option>
            <option value='sell_asc'>매도일 오래된순</option>
            <option value='return_desc'>수익률 높은순</option>
            <option value='return_asc'>수익률 낮은순</option>
          </select>
          <input id='simTradeSearch' type='text' placeholder='종목 검색' />
          <button type='button' class='trade-clear-btn' id='simTradeClearBtn'>필터 해제</button>
          <div class='k'>최근 체결을 화면에서 바로 정렬하고 필터링할 수 있습니다.</div>
        </div>
        <div style='overflow-x:auto;margin-top:6px;'>
          <table id='simTradeTable'>
            <thead><tr><th>종목</th><th>매수일</th><th>매도일</th><th>매수가</th><th>매도가</th><th>수량</th><th>수익률</th><th>실현손익</th><th>보유</th><th>유형</th></tr></thead>
            <tbody>{sim_trade_rows if sim_trade_rows else "<tr><td colspan='10'>시뮬레이션 거래가 아직 없습니다.</td></tr>"}</tbody>
          </table>
        </div>
      </div>
    </div>
    <div class='card cat-monitor span-1' id='block-section' data-dashboard-tab='stocks' data-stocks-tab='blocks'>
      <div class='section-head'><div><div class='section-title'>차단 사유</div><div class='v'>차단 사유와 팩터</div></div></div>
      <div class='k'>최근 루프 기준 차단 사유</div>
      <div class='block-card-list'>
        {reason_hist_cards if reason_hist_cards else "<div class='k'>차단 사유를 집계 중입니다.</div>"}
      </div>
      <div class='k' style='margin-top:10px'>현재 선택 종목 팩터 스냅샷</div>
      <div style='overflow-x:auto;margin-top:6px;'>
        <table class='tight-table'>
          <thead><tr><th>종목</th><th>섹터</th><th>점수</th><th>20D</th><th>5D</th><th>상대강도</th><th>추세</th><th>변동성</th><th>관심도</th><th>스파이크</th><th>RSI</th><th>추격과열</th></tr></thead>
          <tbody>{factor_rows if factor_rows else "<tr><td colspan='12'>팩터 데이터를 집계 중입니다.</td></tr>"}</tbody>
        </table>
      </div>
    </div>
    <div class='card cat-monitor span-1' id='event-section' data-dashboard-tab='status'>
      <div class='section-head'><div><div class='section-title'>이벤트 스트림</div><div class='v'>최근 이벤트</div></div></div>
      <div class='ops-card-grid'>
        {event_summary_cards if event_summary_cards else "<div class='k'>이벤트 요약을 집계 중입니다.</div>"}
      </div>
      <div class='evt-filter'>
        <button type='button' class='evt-btn active' data-evt='ALL'>ALL</button>
        <button type='button' class='evt-btn' data-evt='ORDER'>ORDER</button>
        <button type='button' class='evt-btn' data-evt='RISK'>RISK</button>
        <button type='button' class='evt-btn' data-evt='REGIME'>REGIME</button>
        <button type='button' class='evt-btn' data-evt='ERROR'>ERROR</button>
      </div>
      <pre id='eventLog'>{events}</pre>
    </div>
    <div class='card cat-monitor span-1' id='journal-section' data-dashboard-tab='status'>
      <div class='section-head'><div><div class='section-title'>주문 저널</div><div class='v'>주문 리컨실리에이션</div></div></div>
      <div class='ops-card-grid'>
        {order_summary_cards if order_summary_cards else "<div class='k'>주문 요약을 집계 중입니다.</div>"}
      </div>
      <div class='k'>대기 {int(_to_float(reconcile.get('pending')))} | 정합 완료 {int(_to_float(reconcile.get('reconciled_this_loop')))} | 시간초과 {int(_to_float(reconcile.get('timeout_this_loop')))} | 저널 {int(_to_float(reconcile.get('journal_size')))}</div>
      <div style='overflow-x:auto;margin-top:6px;'>
        <table>
          <thead><tr><th>시각</th><th>종목</th><th>방향</th><th>수량</th><th>가격</th><th>상태</th><th>상세</th></tr></thead>
          <tbody>{order_rows if order_rows else "<tr><td colspan='7'>주문 이력이 아직 없습니다.</td></tr>"}</tbody>
        </table>
      </div>
    </div>
</div>
<div id='guideModal' class='help-modal' aria-hidden='true'>
  <div class='help-backdrop' id='guideBackdrop'></div>
  <div class='help-dialog' role='dialog' aria-modal='true' aria-labelledby='guideTitle'>
    <div class='help-head'>
      <div>
        <div id='guideTitle' class='help-title'>메뉴/화면 가이드</div>
        <div class='help-k'>메뉴 기능, 화면 해석법, 체크리스트를 빠르게 확인합니다.</div>
      </div>
      <button type='button' id='guideCloseBtn' class='help-close'>닫기</button>
    </div>
    <div class='help-body'>
      <div class='help-card'>
        <div class='help-v'>0) 메뉴/콘텐츠/지표 읽는 법</div>
        <div class='help-k'>메뉴 설명</div>
        <table>
          <thead><tr><th>메뉴</th><th>기능</th><th>권장 사용법</th></tr></thead>
          <tbody>{menu_guide_rows}</tbody>
        </table>
        <div class='help-k' style='margin-top:8px'>화면/콘텐츠 설명</div>
        <table>
          <thead><tr><th>영역</th><th>읽는 법</th></tr></thead>
          <tbody>{panel_guide_rows}</tbody>
        </table>
        <div class='help-k' style='margin-top:8px'>팩터 용어 사전</div>
        <table>
          <thead><tr><th>용어</th><th>의미</th></tr></thead>
          <tbody>{factor_glossary_rows}</tbody>
        </table>
        <div class='help-k' style='margin-top:8px'>초보 체크리스트</div>
        <table>
          <thead><tr><th>Step</th><th>체크 항목</th><th>완료</th></tr></thead>
          <tbody>{checklist_beginner_rows}</tbody>
        </table>
        <div class='help-k' style='margin-top:8px'>중급 체크리스트</div>
        <table>
          <thead><tr><th>Step</th><th>체크 항목</th><th>완료</th></tr></thead>
          <tbody>{checklist_intermediate_rows}</tbody>
        </table>
        <div class='help-k' style='margin-top:8px'>실전 체크리스트</div>
        <table>
          <thead><tr><th>Step</th><th>체크 항목</th><th>완료</th></tr></thead>
          <tbody>{checklist_live_rows}</tbody>
        </table>
        <div style='margin-top:8px;display:flex;justify-content:flex-end'>
          <button type='button' class='help-close' id='checklistResetBtnHelp'>체크리스트 초기화</button>
        </div>
      </div>
    </div>
  </div>
</div>
<div id='helpModal' class='help-modal' aria-hidden='true'>
  <div class='help-backdrop' id='helpBackdrop'></div>
  <div class='help-dialog' role='dialog' aria-modal='true' aria-labelledby='helpTitle'>
    <div class='help-head'>
      <div>
        <div id='helpTitle' class='help-title'>자동매매 전략 가이드</div>
        <div class='help-k'>자동 유니버스 스캔, 상승 추세 선별, 진입/청산 조건, 예외 규칙을 현재 설정값 기준으로 설명합니다.</div>
      </div>
      <button type='button' id='helpCloseBtn' class='help-close'>닫기</button>
    </div>
    <div class='help-body'>
      <div class='help-grid'>
        <div class='help-card'><div class='help-k'>현재 시장 국면</div><div class='help-v'>{current_regime}</div></div>
        <div class='help-card'><div class='help-k'>현재 선택 종목</div><div class='help-v'>{selected_symbol}</div></div>
        <div class='help-card'><div class='help-k'>선정 기준/점수</div><div class='help-v'>{selection_ref} / {selection_score:+.2f}</div></div>
      </div>
      <div class='help-card'>
        <div class='help-v'>1) 어떻게 종목을 고르고 언제 다시 고르나?</div>
        <div class='help-k'>운용 흐름</div>
        <ul>
          <li>후보 스캔: 현재는 KIND 자동 유니버스를 기준으로 전종목을 갱신하고, 점수 상위 <code>{max(3, int(settings.candidate_refresh_top_n))}</code>개 후보를 운영 준비 대상으로 압축합니다.</li>
          <li>최종 선정: 최종 실전 운용 바스켓은 최대 <code>{settings.trend_select_count}</code>개까지 유지합니다.</li>
          <li>장중 재평가: 현재 설정 기준으로 약 <code>{max(1, int(getattr(settings, 'intraday_reselect_minutes', settings.candidate_refresh_minutes)))}</code>분마다 후보와 선정 종목을 다시 평가합니다.</li>
          <li>알림: 선정 종목이 바뀌면 Slack으로 변경 종목과 선정 이유를 보내고, 장 시작 직후에는 오프닝 브리프 1회, 정규장 중에는 매시각 요약 리포트를 보냅니다.</li>
        </ul>
        <div class='help-k'>시장 국면 판정 기준 (리스크 강도 조절용)</div>
        <ul>
          <li>강세: 지수등락률 ≥ <code>+0.70%</code> 그리고 상승비율 ≥ <code>55%</code></li>
          <li>약세: 지수등락률 ≤ <code>-0.70%</code> 그리고 상승비율 ≤ <code>45%</code></li>
          <li>그 외는 중립</li>
        </ul>
        <div class='help-k'>현재 판정 근거: 지수등락 <code>{regime_idx_pct:+.2f}%</code> / 상승비율 <code>{regime_breadth_pct:.1f}%</code> → <strong>{_display_label(regime_calc, kind="regime")}</strong> ({regime_basis})</div>
        <div class='help-k'>현재 시장 국면 신뢰도: <code>{round(float(st.get('regime_confidence', 0.0)) * 100.0, 1)}%</code></div>
        <div class='help-k'>종목 스캔 범위</div>
        <ul>
          <li>현재 소스: <code>{html.escape(universe_source_label)}</code></li>
          <li>{html.escape(universe_scope_text)}</li>
          <li>시뮬레이션/백테스트는 캐시 기준 KRX seed <code>300</code>개에서 거래대금 상위 <code>100</code>종목을 우선 평가합니다.</li>
          <li>{html.escape(universe_seed_text)}. 자동 유니버스가 일시적으로 비면 <code>SYMBOL</code>로만 최소 부트스트랩합니다.</li>
        </ul>
        <div class='help-k'>선정 방식</div>
        <ul>
          <li>상승 추세 필터: <code>MA5 &gt; MA20 &gt; MA60</code> 그리고 최근 고점/저점 구조가 우상향이어야 함</li>
          <li>거래대금 관심도: 5일 평균 거래대금 / 20일 평균 거래대금 ≥ <code>{settings.trend_min_turnover_ratio_5_to_20:.2f}</code></li>
          <li>거래대금 스파이크: 최근 20일 평균 대비 ≥ <code>{settings.trend_min_value_spike_ratio:.2f}</code></li>
          <li>변동성 필터: ATR14 비율이 <code>{settings.trend_min_atr14_pct:.1f}%</code> ~ <code>{settings.trend_max_atr14_pct:.1f}%</code></li>
          <li>과열 제외: 1일 상승률 ≥ <code>{settings.trend_overheat_day_pct:.1f}%</code> 또는 2일 상승률 ≥ <code>{settings.trend_overheat_2day_pct:.1f}%</code></li>
          <li>추격 과열 감점: 너무 멀리 연장된 종목은 점수에서 감점하고 후순위로 밀립니다.</li>
          <li>최대 선정 종목 수 <code>{settings.trend_select_count}</code>, 동일 섹터 최대 <code>{settings.trend_max_sector_names}</code>개</li>
        </ul>
      </div>
      <div class='help-card'>
        <div class='help-v'>2) 어떤 경우에 매수/매도하나?</div>
        <div class='help-k'>신규 진입 조건</div>
        <ul>
          <li>하루 1회 선정된 종목 안에서만 신규 진입을 검토합니다.</li>
          <li>실행 기준은 <code>{settings.bar_interval_minutes}</code>분봉이며, 현재는 봉 마감 시점에만 판단하도록 고정되어 있습니다.</li>
          <li>일봉 추세 필터 통과 + RSI(14) <code>{settings.trend_daily_rsi_min:.1f}</code> ~ <code>{settings.trend_daily_rsi_max:.1f}</code></li>
          <li>기본 진입은 단기 눌림목 회복이며, 추세·구조가 유지되면 제한적 추격 진입도 허용합니다.</li>
          <li>상단 밴드 근처인데 수급 가속이 약한 늦은 추격, 시장 급등일에 종목만 짧게 튄 탄력 추격은 차단합니다.</li>
          <li>갭 필터: 전일 종가 대비 <code>{settings.trend_gap_skip_down_pct:.1f}%</code> 미만 급락은 제외, <code>{settings.trend_gap_skip_up_pct:.1f}%</code> 이상 과도한 갭상승도 제외</li>
          <li>신호 확인은 기본 <code>{settings.signal_confirm_cycles}</code>회이며, 기술 우선/단기 강세 신호는 더 빠르게 진입할 수 있습니다.</li>
          <li>시장 상태 필터가 켜져 있으면 충격장·약세 국면에서는 일반 신규 BUY를 억제하고, 약세 예외 후보만 제한적으로 허용합니다.</li>
          <li>약세장 예외 진입은 현재 <code>{_display_label("ON" if settings.enable_bearish_exception else "OFF", kind="bool")}</code>이며, OFF일 때는 역추세 예외 BUY를 사용하지 않습니다.</li>
        </ul>
        <div class='help-k'>청산 원칙</div>
        <ul>
          <li>단기 전략은 수익이 빠르게 나면 조기 익절하며, 보유 기간이 길어질수록 수익 보호를 우선합니다. 현재 코드는 최대 5일 고정 보유가 아니라 조건형 청산입니다.</li>
          <li><code>2</code>거래일 이상 보유했고 수익이 <code>0.8%</code> 이상이면서 상단권이면 조기 정리합니다.</li>
          <li><code>3</code>거래일 이상 보유했고 수익이 <code>0.2%</code> 이상이면 보수적으로 정리합니다.</li>
          <li>즉, 보통은 <code>2~3</code>거래일 수익 구간에서 먼저 정리되며, 손절·트레일링·구조 훼손 여부에 따라 더 짧아질 수 있습니다.</li>
          <li>종목 손실 캡: 수익률 ≤ <code>{settings.max_symbol_loss_pct:.2f}%</code></li>
          <li>초기 실패형 청산: 진입 직후 1 bar 안에 약 <code>-2.4%</code> 이상 밀리고 추세가 훼손되면 빠르게 정리합니다.</li>
          <li>시장 국면에 따라 ATR 손절/익절/트레일링 강도를 조정합니다.</li>
          <li>ATR 계산 창: <code>{settings.atr_exit_lookback_days}</code>일</li>
          <li>동적 배수 하한: stop <code>{settings.atr_stop_mult:.2f}</code>, take <code>{settings.atr_take_mult:.2f}</code>, trailing <code>{settings.atr_trailing_mult:.2f}</code></li>
        </ul>
        <div class='help-k'>추가 제한</div>
        <ul>
          <li>일일 손실 한도: <code>{settings.daily_loss_limit_pct:.2f}%</code> 이하 시 당일 신규 BUY 차단 (리스크 홀트)</li>
          <li>포트폴리오 히트: <code>{settings.max_portfolio_heat_pct:.2f}%</code> 초과 시 신규 BUY 차단</li>
          <li>시세 stale 보호: 데이터 지연이 <code>{settings.stale_data_max_age_sec}</code>초 초과 시 신규 BUY 차단</li>
          <li>종목별 주문 쿨다운: 최근 체결 후 <code>{settings.trade_cooldown_sec}</code>초 동안 재주문 차단</li>
          <li>손실 청산 직후 같은 종목은 최소 1개 봉 동안 재진입을 막아 반등 착시 재추격을 줄입니다.</li>
          <li>시그널 확인: BUY/SELL 신호가 <code>{settings.signal_confirm_cycles}</code>회 연속 확인되어야 주문 실행</li>
          <li>주문 수량: 기본 <code>{settings.position_size}</code> + 변동성 기반 리스크 사이징(<code>{settings.target_risk_per_trade_pct:.2f}%</code>/trade)</li>
          <li>일 최대 주문: <code>{settings.max_daily_orders}</code></li>
          <li>거래 모드: <code>{_display_label(settings.trade_mode, kind="mode")}</code> / 실거래 승인 <code>{_display_label("ON" if settings.live_armed else "OFF", kind="bool")}</code></li>
        </ul>
        <div class='help-k'>설정 반영 규칙</div>
        <ul>
          <li>설정 저장 시 봇은 자동 재시작되어 즉시 반영됩니다.</li>
          <li>현재 화면과 KRX 런타임은 단일 추세추종 전략 기준으로 동작합니다. 별도 전략 리더보드/프로필 프리셋은 사용하지 않습니다.</li>
          <li>미국 mock 리포트는 보조 레거시 시뮬레이터이며 기본값은 비활성입니다. 필요할 때만 별도로 켜서 사용합니다.</li>
        </ul>
      </div>
      <div class='help-card'>
        <div class='help-v'>3) 지금 선택 이유 (실제 이벤트 로그 기준)</div>
        <div class='help-k'>최근 SELECT 이벤트</div>
        <pre>{html.escape(select_event or '없음')}</pre>
        <div class='help-k'>최근 REGIME_SHIFT</div>
        <pre>{html.escape(regime_event or '없음')}</pre>
        <div class='help-k'>최근 REGIME_CANDIDATE</div>
        <pre>{html.escape(regime_candidate_event or '없음')}</pre>
        <div class='help-k'>최근 ROTATE_TARGET</div>
        <pre>{html.escape(rotate_event or '없음')}</pre>
        <div class='help-k'>최근 RISK_EXIT</div>
        <pre>{html.escape(risk_event or '없음')}</pre>
        <div class='help-k'>최근 RISK_HALT</div>
        <pre>{html.escape(risk_halt_event or '없음')}</pre>
        <div class='help-k'>최근 NO_TRADE_SUMMARY</div>
        <pre>{html.escape(no_trade_event or '없음')}</pre>
        <div class='help-k'>SELECT 파싱 결과</div>
        <pre>{html.escape(json.dumps(parsed, ensure_ascii=False, indent=2) if parsed else '파싱 가능한 SELECT 이벤트가 없습니다.')}</pre>
      </div>
      <div class='help-card'>
      <div class='help-v'>4) 현재 선정 근거 수치</div>
      <div class='help-k'>{selection_exact_summary}</div>
      <div class='help-k'>선정 섹터: {html.escape(_display_text(selected_sector_summary, '섹터 매핑 대기 중'))}</div>
      <pre>{selection_reason_text}</pre>
        <table>
          <thead><tr><th>순위</th><th>종목</th><th>점수</th><th>20일 수익률</th><th>5일 수익률</th><th>상대강도</th><th>추세</th><th>변동성</th><th>RAM</th><th>TEF</th><th>TQP</th><th>관심도</th><th>스파이크</th><th>RSI</th></tr></thead>
          <tbody>{selection_rank_rows if selection_rank_rows else "<tr><td colspan='14'>랭킹 집계 중입니다.</td></tr>"}</tbody>
        </table>
        {(f"<div class='help-k' style='margin-top:8px;color:#e89a56'>Fallback 이유: {fallback_reason_text}</div>" if fallback_reason_text else "")}
      </div>
    </div>
  </div>
</div>
<div id='configModal' class='config-modal' aria-hidden='true'>
  <div class='help-backdrop' id='configBackdrop'></div>
  <div class='config-dialog' role='dialog' aria-modal='true' aria-labelledby='configTitle'>
    <div class='help-head'>
      <div>
        <div id='configTitle' class='help-title'>연결/알림 설정</div>
        <div class='help-k'>연결/리스크/알림 설정을 저장하면 봇을 자동 재시작하여 즉시 반영합니다.</div>
      </div>
      <button type='button' id='configCloseBtn' class='help-close'>닫기</button>
    </div>
    <div class='help-body'>
      <form method='post' action='/config-save' id='configForm'>
        <div class='help-k' style='margin-bottom:6px'>연결 설정</div>
        <div class='cfg-grid'>
          <div class='cfg-field'>
            <label>KIWOOM_BASE_URL</label>
            <input name='KIWOOM_BASE_URL' value='{cfg_base_url}' />
          </div>
          <div class='cfg-field'>
            <label>ACCOUNT_NO</label>
            <input name='ACCOUNT_NO' value='{cfg_account_no}' />
          </div>
          <div class='cfg-field'>
            <label>PRICE_PATH</label>
            <input name='PRICE_PATH' value='{cfg_price_path}' />
          </div>
          <div class='cfg-field'>
            <label>ORDER_PATH</label>
            <input name='ORDER_PATH' value='{cfg_order_path}' />
          </div>
        </div>
        <div class='help-k' style='margin:10px 0 6px'>슬랙 알림 설정</div>
        <div class='cfg-grid'>
          <div class='cfg-field'>
            <label>SLACK_WEBHOOK_URL</label>
            <input name='SLACK_WEBHOOK_URL' value='{cfg_slack_webhook}' />
          </div>
          <div class='cfg-field'>
            <label>SLACK_EVENT_KEYWORDS (쉼표 구분)</label>
            <input name='SLACK_EVENT_KEYWORDS' value='{cfg_slack_keywords}' />
          </div>
        </div>
        <div class='cfg-field' style='margin-top:10px'>
          <label><input type='checkbox' name='SLACK_ENABLED' value='1' {slack_enabled_checked} /> SLACK_ENABLED</label>
        </div>
        <div class='cfg-field'>
          <label><input type='checkbox' name='HOURLY_MARKET_REPORT_ENABLED' value='1' {cfg_hourly_market_report_enabled_checked} /> HOURLY_MARKET_REPORT_ENABLED</label>
        </div>
        <div class='help-k' style='margin:10px 0 6px'>실거래/시뮬레이션 비교 경고 기준</div>
        <div class='cfg-grid'>
          <div class='cfg-field'>
            <label>COMPARE_WARN_WIN_RATE_GAP_PCT</label>
            <input name='COMPARE_WARN_WIN_RATE_GAP_PCT' value='{cfg_compare_warn_win_rate_gap}' />
          </div>
          <div class='cfg-field'>
            <label>COMPARE_WARN_PNL_GAP_KRW</label>
            <input name='COMPARE_WARN_PNL_GAP_KRW' value='{cfg_compare_warn_pnl_gap}' />
          </div>
          <div class='cfg-field'>
            <label>COMPARE_WARN_EXPECTANCY_GAP_KRW</label>
            <input name='COMPARE_WARN_EXPECTANCY_GAP_KRW' value='{cfg_compare_warn_expectancy_gap}' />
          </div>
          <div class='cfg-field'>
            <label>COMPARE_WARN_HOLD_GAP_DAYS</label>
            <input name='COMPARE_WARN_HOLD_GAP_DAYS' value='{cfg_compare_warn_hold_gap}' />
          </div>
        </div>
        <div class='help-k' style='margin:10px 0 6px'>iPhone 설치 링크</div>
        <div class='cfg-grid'>
          <div class='cfg-field'>
            <label>IOS_TESTFLIGHT_URL</label>
            <input name='IOS_TESTFLIGHT_URL' value='{cfg_ios_testflight_url}' placeholder='https://testflight.apple.com/join/...' />
          </div>
          <div class='cfg-field'>
            <label>IOS_APP_STORE_URL</label>
            <input name='IOS_APP_STORE_URL' value='{cfg_ios_app_store_url}' placeholder='https://apps.apple.com/...' />
          </div>
          <div class='cfg-field'>
            <label>IOS_MANIFEST_URL</label>
            <input name='IOS_MANIFEST_URL' value='{cfg_ios_manifest_url}' placeholder='https://your-domain/app.plist' />
          </div>
          <div class='cfg-field'>
            <label>MOBILE_SERVER_URL</label>
            <input name='MOBILE_SERVER_URL' value='{cfg_mobile_server_url}' placeholder='https://your-dashboard.example.com 또는 http://192.168.x.x:8080' />
          </div>
          <div class='cfg-field'>
            <label>MOBILE_SERVER_LABEL</label>
            <input name='MOBILE_SERVER_LABEL' value='{cfg_mobile_server_label}' placeholder='AITRADER Server' />
          </div>
          <div class='cfg-field'>
            <label>MOBILE_APP_SCHEME</label>
            <input name='MOBILE_APP_SCHEME' value='{cfg_mobile_app_scheme}' placeholder='aitrader' />
          </div>
        </div>
        <div class='help-k' style='margin:10px 0 6px'>웹 접근 보호</div>
        <div class='cfg-grid'>
          <div class='cfg-field'>
            <label><input type='checkbox' name='WEB_ACCESS_ENABLED' value='1' {cfg_web_access_enabled_checked} /> WEB_ACCESS_ENABLED</label>
          </div>
          <div class='cfg-field'>
            <label>WEB_ACCESS_KEY</label>
            <input name='WEB_ACCESS_KEY' value='{cfg_web_access_key}' placeholder='휴대폰 등록용 접근 키' />
          </div>
          <div class='cfg-field'>
            <label>WEB_TRUSTED_DEVICE_DAYS</label>
            <input name='WEB_TRUSTED_DEVICE_DAYS' value='{cfg_web_trusted_device_days}' />
          </div>
          <div class='cfg-field'>
            <label>WEB_MAX_TRUSTED_DEVICES</label>
            <input name='WEB_MAX_TRUSTED_DEVICES' value='{cfg_web_max_trusted_devices}' />
          </div>
        </div>
        <div class='help-k' style='margin-top:6px'>localhost는 항상 허용되고, 휴대폰은 접근 키를 한 번 입력하면 신뢰 기기로 등록됩니다.</div>
        <div class='help-k' style='margin:10px 0 6px'>등록된 신뢰 기기</div>
        <div class='help-card' style='padding:0'>
          <table>
            <thead><tr><th>기기</th><th>최근 IP</th><th>등록 시각</th><th>최근 사용</th><th>관리</th></tr></thead>
            <tbody>{trusted_device_rows}</tbody>
          </table>
        </div>
        <div class='help-k' style='margin:10px 0 6px'>리스크/집행 설정</div>
        <div class='cfg-grid'>
          <div class='cfg-field'>
            <label>TRADE_MODE (DRY/LIVE)</label>
            <input id='cfg_trade_mode' name='TRADE_MODE' value='{cfg_trade_mode}' />
          </div>
          <div class='cfg-field'>
            <label><input type='checkbox' name='LIVE_ARMED' value='1' {cfg_live_armed_checked} /> LIVE_ARMED (실거래 주문 허용)</label>
          </div>
          <div class='cfg-field'>
            <label>TRADE_COOLDOWN_SEC</label>
            <input id='cfg_trade_cooldown_sec' name='TRADE_COOLDOWN_SEC' value='{cfg_trade_cooldown}' />
          </div>
          <div class='cfg-field'>
            <label>STALE_DATA_MAX_AGE_SEC</label>
            <input id='cfg_stale_data_max_age_sec' name='STALE_DATA_MAX_AGE_SEC' value='{cfg_stale_age}' />
          </div>
          <div class='cfg-field'>
            <label>TARGET_RISK_PER_TRADE_PCT</label>
            <input id='cfg_target_risk_per_trade_pct' name='TARGET_RISK_PER_TRADE_PCT' value='{cfg_target_risk}' />
          </div>
          <div class='cfg-field'>
            <label>DAILY_LOSS_LIMIT_PCT</label>
            <input id='cfg_daily_loss_limit_pct' name='DAILY_LOSS_LIMIT_PCT' value='{cfg_daily_loss_limit}' />
          </div>
          <div class='cfg-field'>
            <label>MAX_SYMBOL_LOSS_PCT</label>
            <input id='cfg_max_symbol_loss_pct' name='MAX_SYMBOL_LOSS_PCT' value='{cfg_max_symbol_loss}' />
          </div>
          <div class='cfg-field'>
            <label>MAX_PORTFOLIO_HEAT_PCT</label>
            <input id='cfg_max_portfolio_heat_pct' name='MAX_PORTFOLIO_HEAT_PCT' value='{cfg_max_portfolio_heat}' />
          </div>
          <div class='cfg-field'>
            <label>SIGNAL_CONFIRM_CYCLES</label>
            <input id='cfg_signal_confirm_cycles' name='SIGNAL_CONFIRM_CYCLES' value='{cfg_signal_confirm_cycles}' />
          </div>
          <div class='cfg-field'>
            <label>BAR_INTERVAL_MINUTES</label>
            <input id='cfg_bar_interval_minutes' name='BAR_INTERVAL_MINUTES' value='{cfg_bar_interval_minutes}' />
          </div>
          <div class='cfg-field'>
            <label><input type='checkbox' name='DECISION_ON_BAR_CLOSE_ONLY' value='1' {cfg_decision_on_bar_close_only_checked} /> DECISION_ON_BAR_CLOSE_ONLY</label>
          </div>
          <div class='cfg-field'>
            <label><input type='checkbox' name='MARKET_STATUS_FILTER_ENABLED' value='1' {cfg_market_status_filter_enabled_checked} /> MARKET_STATUS_FILTER_ENABLED</label>
          </div>
          <div class='cfg-field'>
            <label><input type='checkbox' name='ENABLE_BEARISH_EXCEPTION' value='1' {cfg_enable_bearish_exception_checked} /> ENABLE_BEARISH_EXCEPTION</label>
          </div>
          <div class='cfg-field'>
            <label>ATR_EXIT_LOOKBACK_DAYS</label>
            <input id='cfg_atr_exit_lookback_days' name='ATR_EXIT_LOOKBACK_DAYS' value='{cfg_atr_exit_lookback_days}' />
          </div>
          <div class='cfg-field'>
            <label>ATR_STOP_MULT</label>
            <input id='cfg_atr_stop_mult' name='ATR_STOP_MULT' value='{cfg_atr_stop_mult}' />
          </div>
          <div class='cfg-field'>
            <label>ATR_TAKE_MULT</label>
            <input id='cfg_atr_take_mult' name='ATR_TAKE_MULT' value='{cfg_atr_take_mult}' />
          </div>
          <div class='cfg-field'>
            <label>ATR_TRAILING_MULT</label>
            <input id='cfg_atr_trailing_mult' name='ATR_TRAILING_MULT' value='{cfg_atr_trailing_mult}' />
          </div>
        </div>
        <div class='help-k' style='margin:10px 0 6px'>추세 전략 설정</div>
        <div class='help-k' style='margin-bottom:8px'>이 화면은 현재 실행 중인 실제 전략 파라미터를 기준으로 채워집니다. 저장하면 <code>.env</code> 와 런타임 override가 함께 갱신됩니다.</div>
        <div class='cfg-grid'>
          <div class='cfg-field'>
            <label>MAX_ACTIVE_POSITIONS</label>
            <input id='cfg_max_active_positions' name='MAX_ACTIVE_POSITIONS' value='{cfg_max_active_positions}' />
          </div>
          <div class='cfg-field'>
            <label>CANDIDATE_REFRESH_TOP_N</label>
            <input id='cfg_candidate_refresh_top_n' name='CANDIDATE_REFRESH_TOP_N' value='{cfg_candidate_refresh_top_n}' />
          </div>
          <div class='cfg-field'>
            <label>CANDIDATE_REFRESH_MINUTES</label>
            <input id='cfg_candidate_refresh_minutes' name='CANDIDATE_REFRESH_MINUTES' value='{cfg_candidate_refresh_minutes}' />
          </div>
          <div class='cfg-field'>
            <label><input type='checkbox' name='INTRADAY_RESELECT_ENABLED' value='1' {cfg_intraday_reselect_enabled_checked} /> INTRADAY_RESELECT_ENABLED</label>
          </div>
          <div class='cfg-field'>
            <label>INTRADAY_RESELECT_MINUTES</label>
            <input id='cfg_intraday_reselect_minutes' name='INTRADAY_RESELECT_MINUTES' value='{cfg_intraday_reselect_minutes}' />
          </div>
          <div class='cfg-field'>
            <label>TREND_SELECT_COUNT</label>
            <input id='cfg_trend_select_count' name='TREND_SELECT_COUNT' value='{cfg_trend_select_count}' />
          </div>
          <div class='cfg-field'>
            <label>TREND_MIN_AVG_TURNOVER20_KRW</label>
            <input id='cfg_trend_min_avg_turnover20_krw' name='TREND_MIN_AVG_TURNOVER20_KRW' value='{cfg_trend_min_avg_turnover20_krw}' />
          </div>
          <div class='cfg-field'>
            <label>TREND_MIN_TURNOVER_RATIO_5_TO_20</label>
            <input id='cfg_trend_turnover_ratio' name='TREND_MIN_TURNOVER_RATIO_5_TO_20' value='{cfg_trend_turnover_ratio}' />
          </div>
          <div class='cfg-field'>
            <label>TREND_MIN_VALUE_SPIKE_RATIO</label>
            <input id='cfg_trend_value_spike_ratio' name='TREND_MIN_VALUE_SPIKE_RATIO' value='{cfg_trend_value_spike_ratio}' />
          </div>
          <div class='cfg-field'>
            <label>TREND_BREAKOUT_BUFFER_PCT</label>
            <input id='cfg_trend_breakout_buffer' name='TREND_BREAKOUT_BUFFER_PCT' value='{cfg_trend_breakout_buffer}' />
          </div>
          <div class='cfg-field'>
            <label>TREND_MIN_ATR14_PCT</label>
            <input id='cfg_trend_min_atr14' name='TREND_MIN_ATR14_PCT' value='{cfg_trend_min_atr14}' />
          </div>
          <div class='cfg-field'>
            <label>TREND_MAX_ATR14_PCT</label>
            <input id='cfg_trend_max_atr14' name='TREND_MAX_ATR14_PCT' value='{cfg_trend_max_atr14}' />
          </div>
          <div class='cfg-field'>
            <label>TREND_OVERHEAT_DAY_PCT</label>
            <input id='cfg_trend_overheat_day' name='TREND_OVERHEAT_DAY_PCT' value='{cfg_trend_overheat_day}' />
          </div>
          <div class='cfg-field'>
            <label>TREND_OVERHEAT_2DAY_PCT</label>
            <input id='cfg_trend_overheat_2day' name='TREND_OVERHEAT_2DAY_PCT' value='{cfg_trend_overheat_2day}' />
          </div>
          <div class='cfg-field'>
            <label>TREND_DAILY_RSI_MIN</label>
            <input id='cfg_trend_daily_rsi_min' name='TREND_DAILY_RSI_MIN' value='{cfg_trend_daily_rsi_min}' />
          </div>
          <div class='cfg-field'>
            <label>TREND_DAILY_RSI_MAX</label>
            <input id='cfg_trend_daily_rsi_max' name='TREND_DAILY_RSI_MAX' value='{cfg_trend_daily_rsi_max}' />
          </div>
          <div class='cfg-field'>
            <label>TREND_GAP_SKIP_UP_PCT</label>
            <input id='cfg_trend_gap_skip_up' name='TREND_GAP_SKIP_UP_PCT' value='{cfg_trend_gap_skip_up}' />
          </div>
          <div class='cfg-field'>
            <label>TREND_GAP_SKIP_DOWN_PCT</label>
            <input id='cfg_trend_gap_skip_down' name='TREND_GAP_SKIP_DOWN_PCT' value='{cfg_trend_gap_skip_down}' />
          </div>
          <div class='cfg-field'>
            <label>TREND_MAX_CHASE_FROM_OPEN_PCT</label>
            <input id='cfg_trend_max_chase_from_open' name='TREND_MAX_CHASE_FROM_OPEN_PCT' value='{cfg_trend_max_chase_from_open}' />
          </div>
          <div class='cfg-field'>
            <label>TREND_MAX_SECTOR_NAMES</label>
            <input id='cfg_trend_max_sector_names' name='TREND_MAX_SECTOR_NAMES' value='{cfg_trend_max_sector_names}' />
          </div>
          <div class='cfg-field' style='grid-column:1 / -1'>
            <label>SYMBOL_SECTOR_MAP (예: 005930:반도체,000660:반도체)</label>
            <input id='cfg_symbol_sector_map' name='SYMBOL_SECTOR_MAP' value='{cfg_symbol_sector_map}' />
          </div>
          <div class='cfg-field' style='grid-column:1 / -1'>
            <label><input type='checkbox' name='SECTOR_AUTO_MAP_ENABLED' value='1' {cfg_sector_auto_map_checked} /> SECTOR_AUTO_MAP_ENABLED</label>
          </div>
        </div>
        <div class='cfg-actions'>
          <button type='button' class='stop' id='configCancelBtn'>취소</button>
          <button type='submit' class='start'>저장</button>
        </div>
      </form>
    </div>
  </div>
</div>
<div class='wrap' style='margin-top:4px;margin-bottom:10px;'>
  <div class='k' style='text-align:right;opacity:.9;'>by superarchi</div>
</div>
<nav class='mobile-dock' aria-label='모바일 빠른 이동'>
  <a href='#' data-dashboard-tab-switch='status'>상태</a>
  <a href='#' data-dashboard-tab-switch='stocks'>분석</a>
  <a href='#' data-dashboard-tab-switch='performance'>성과</a>
  <a href='#control-panel-card' data-dashboard-tab-switch='status'>제어</a>
</nav>
<script>
(() => {{
  if (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) {{
    document.body.classList.add('standalone-app');
  }}
  if (window.navigator && window.navigator.standalone) {{
    document.body.classList.add('standalone-app');
  }}
  const safeGetLS = (key) => {{
    try {{
      return window.localStorage.getItem(key);
    }} catch (_e) {{
      return null;
    }}
  }};
  const safeSetLS = (key, value) => {{
    try {{
      window.localStorage.setItem(key, value);
    }} catch (_e) {{
      // ignore storage failure to keep UI buttons functional
    }}
  }};
  const safeRemoveLS = (key) => {{
    try {{
      window.localStorage.removeItem(key);
    }} catch (_e) {{
      // ignore storage failure
    }}
  }};
  const topbarAlert = document.querySelector('.topbar-alert');
  const topbarClose = document.querySelector('.topbar-alert-close');
  const topbarLegacyPrefix = 'aitrader_compare_topbar_hidden:';
  if (topbarAlert) {{
    const rawAlertKey = topbarAlert.getAttribute('data-alert-key') || 'default';
    const dismissKey = `${{topbarLegacyPrefix}}${{rawAlertKey}}`;
    const priorKeys = [];
    try {{
      for (let i = 0; i < window.localStorage.length; i += 1) {{
        const key = window.localStorage.key(i);
        if (key && key.startsWith(topbarLegacyPrefix) && key !== dismissKey) {{
          priorKeys.push(key);
        }}
      }}
    }} catch (_e) {{
      // ignore storage enumeration failure
    }}
    priorKeys.forEach((key) => safeRemoveLS(key));
    if (safeGetLS(dismissKey) === '1') {{
      topbarAlert.style.display = 'none';
    }}
    if (topbarClose) {{
      topbarClose.addEventListener('click', (event) => {{
        event.preventDefault();
        event.stopPropagation();
        safeSetLS(dismissKey, '1');
        topbarAlert.style.display = 'none';
      }});
    }}
  }}
  const autoOn = document.getElementById('autoRefreshEnabled');
  const autoSec = document.getElementById('autoRefreshSec');
  const autoCountdown = document.getElementById('autoRefreshCountdown');
  let autoTimer = null;
  let remainSec = 0;
  const LS_ON = 'aitrader_auto_refresh_on';
  const LS_SEC = 'aitrader_auto_refresh_sec';

  const isAnyModalOpen = () => {{
    const g1 = document.getElementById('guideModal');
    const m1 = document.getElementById('helpModal');
    const m2 = document.getElementById('configModal');
    return !!(
      (g1 && g1.classList.contains('show'))
      || (m1 && m1.classList.contains('show'))
      || (m2 && m2.classList.contains('show'))
    );
  }};
  const drawCountdown = () => {{
    if (!autoCountdown) return;
    if (!autoOn || !autoOn.checked) {{
      autoCountdown.textContent = '꺼짐';
      return;
    }}
    if (isAnyModalOpen()) {{
      autoCountdown.textContent = '일시정지(팝업 열림)';
      return;
    }}
    autoCountdown.textContent = `다음 새로고침 ${{Math.max(0, remainSec)}}초`;
  }};
  const stopAutoRefresh = () => {{
    if (autoTimer) {{
      clearInterval(autoTimer);
      autoTimer = null;
    }}
    drawCountdown();
  }};
  const startAutoRefresh = () => {{
    stopAutoRefresh();
    if (!autoOn || !autoSec || !autoOn.checked) return;
    const sec = Math.max(5, parseInt(autoSec.value || '60', 10));
    remainSec = sec;
    drawCountdown();
    autoTimer = setInterval(() => {{
      if (!autoOn.checked) {{
        stopAutoRefresh();
        return;
      }}
      if (isAnyModalOpen()) {{
        drawCountdown();
        return;
      }}
      remainSec -= 1;
      if (remainSec <= 0) {{
        window.location.reload();
        return;
      }}
      drawCountdown();
    }}, 1000);
  }};
  if (autoOn && autoSec) {{
    const savedOn = safeGetLS(LS_ON) === '1';
    const savedSec = safeGetLS(LS_SEC);
    if (savedSec && Array.from(autoSec.options).some(o => o.value === savedSec)) {{
      autoSec.value = savedSec;
    }}
    autoOn.checked = savedOn;
    autoOn.addEventListener('change', () => {{
      safeSetLS(LS_ON, autoOn.checked ? '1' : '0');
      startAutoRefresh();
    }});
    autoSec.addEventListener('change', () => {{
      safeSetLS(LS_SEC, autoSec.value);
      startAutoRefresh();
    }});
    startAutoRefresh();
  }}

  const evtButtons = Array.from(document.querySelectorAll('.evt-btn'));
  const evtLog = document.getElementById('eventLog');
  const evtRows = {events_json};
  const LS_EVT = 'aitrader_event_filter';
  const evtMatch = (row, key) => {{
    if (key === 'ALL') return true;
    if (key === 'ORDER') return row.includes('ORDER') || row.includes('SIM_FILL') || row.includes('ROTATE_EXIT');
    if (key === 'RISK') return row.includes('RISK_');
    if (key === 'REGIME') return row.includes('REGIME_') || row.includes('SELECT ') || row.includes('ROTATE_TARGET');
    if (key === 'ERROR') return row.includes('ERROR') || row.includes('FALLBACK') || row.includes('Loop error') || row.includes('Startup error');
    return true;
  }};
  const renderEvents = (key) => {{
    if (!evtLog) return;
    const lines = evtRows.filter((x) => evtMatch(x, key));
    evtLog.textContent = lines.slice(-80).join('\\n');
  }};
  if (evtButtons.length > 0 && evtLog) {{
    const savedEvt = safeGetLS(LS_EVT) || 'ALL';
    evtButtons.forEach((btn) => {{
      btn.addEventListener('click', () => {{
        evtButtons.forEach((b) => b.classList.remove('active'));
        btn.classList.add('active');
        const key = btn.dataset.evt || 'ALL';
        safeSetLS(LS_EVT, key);
        renderEvents(key);
      }});
      if ((btn.dataset.evt || 'ALL') === savedEvt) {{
        evtButtons.forEach((b) => b.classList.remove('active'));
        btn.classList.add('active');
      }}
    }});
    renderEvents(savedEvt);
  }}

  const simTradeTable = document.getElementById('simTradeTable');
  const simResultFilter = document.getElementById('simResultFilter');
  const simSortOrder = document.getElementById('simSortOrder');
  const simTradeSearch = document.getElementById('simTradeSearch');
  const simTradeClearBtn = document.getElementById('simTradeClearBtn');
  const liveTradeTable = document.getElementById('liveTradeTable');
  const liveResultFilter = document.getElementById('liveResultFilter');
  const liveSortOrder = document.getElementById('liveSortOrder');
  const liveTradeSearch = document.getElementById('liveTradeSearch');
  const liveTradeClearBtn = document.getElementById('liveTradeClearBtn');
  const simulationRunForm = document.getElementById('simulationRunForm');
  const simulationRunBtn = document.getElementById('simulationRunBtn');
  const perfTabs = Array.from(document.querySelectorAll('[data-perf-tab]'));
  const perfPanels = Array.from(document.querySelectorAll('[data-perf-panel]'));
  const stockTabs = Array.from(document.querySelectorAll('[data-stock-tab]'));
  const stockPanels = Array.from(document.querySelectorAll('[data-stock-panel]'));
  const dashboardTabs = Array.from(document.querySelectorAll('[data-dashboard-tab]'));
  const dashboardTabButtons = Array.from(document.querySelectorAll('.dashboard-main-tab'));
  const stocksSubtabButtons = Array.from(document.querySelectorAll('.stocks-subtab'));
  const mobileDockButtons = Array.from(document.querySelectorAll('[data-dashboard-tab-switch]'));
  const stocksSubtabBar = document.querySelector('.stocks-subtabbar');
  const stocksSubtabHelp = document.querySelector('.stocks-subtab-help');
  const stockBoardSearch = document.getElementById('stockBoardSearch');
  const performanceLivePanel = document.getElementById('performanceLivePanel');
  const performanceSimPanel = document.getElementById('performanceSimPanel');
  const tradeFocusCompareCard = document.getElementById('tradeFocusCompareCard');
  const tradeFocusSymbolBadge = document.getElementById('tradeFocusSymbolBadge');
  const tradeFocusClearBtn = document.getElementById('tradeFocusClearBtn');
  const tradeFocusLiveStats = document.getElementById('tradeFocusLiveStats');
  const tradeFocusSimStats = document.getElementById('tradeFocusSimStats');
  const tradeFocusLiveChart = document.getElementById('tradeFocusLiveChart');
  const tradeFocusSimChart = document.getElementById('tradeFocusSimChart');
  const tradeFocusLiveNote = document.getElementById('tradeFocusLiveNote');
  const tradeFocusSimNote = document.getElementById('tradeFocusSimNote');
  const tradeSymbolButtons = Array.from(document.querySelectorAll('[data-trade-symbol]'));
  const scheduleMasonry = () => {{}};
  const currentSessionPhase = {json.dumps(str(st.get("session_phase") or ""))};
  const marketTimePhases = ['PREMARKET_BRIEF', 'OPENING_FOCUS', 'REGULAR_SESSION', 'CLOSE_GUARD'];
  const postMarketPhases = ['AFTER_MARKET', 'OFF_HOURS'];
  const LS_DASHBOARD_TAB = 'aitrader_dashboard_tab_v1';
  const LS_STOCKS_SUBTAB = 'aitrader_stocks_subtab_v1';
  const getDefaultDashboardTab = () => {{
    if (marketTimePhases.includes(currentSessionPhase)) return 'stocks';
    if (postMarketPhases.includes(currentSessionPhase)) return 'performance';
    return 'status';
  }};
  const getDefaultStocksSubtab = () => {{
    if (marketTimePhases.includes(currentSessionPhase)) return 'board';
    if (postMarketPhases.includes(currentSessionPhase)) return 'focus';
    return 'focus';
  }};
  let activeDashboardTab = getDefaultDashboardTab();
  let activeStocksSubtab = getDefaultStocksSubtab();
  const getDefaultBoardTab = () => {{
    if (marketTimePhases.includes(currentSessionPhase)) return 'candidate';
    return 'candidate';
  }};
  const getDefaultPerfTab = () => {{
    if (postMarketPhases.includes(currentSessionPhase)) return 'live';
    return 'live';
  }};
  const getLayoutPriority = () => {{
    if (marketTimePhases.includes(currentSessionPhase)) {{
      return {{
        'ops-status': 0,
        'today-detail-section': 1,
        'focus-section': 2,
        'board-section': 3,
        'hero-summary': 4,
        'ops-monitor': 5,
        'market-section': 6,
        'block-section': 7,
        'event-section': 8,
        'performance-section': 9,
        'performance-live-section': 10,
        'performance-sim-section': 11,
        'journal-section': 12,
        'control-panel-card': 13,
      }};
    }}
    if (postMarketPhases.includes(currentSessionPhase)) {{
      return {{
        'today-detail-section': 0,
        'performance-section': 1,
        'performance-live-section': 2,
        'performance-sim-section': 3,
        'focus-section': 4,
        'market-section': 5,
        'block-section': 6,
        'ops-status': 7,
        'hero-summary': 8,
        'ops-monitor': 9,
        'event-section': 10,
        'journal-section': 11,
        'control-panel-card': 12,
      }};
    }}
    return {{
      'hero-summary': 0,
      'ops-status': 1,
      'today-detail-section': 2,
      'focus-section': 3,
      'board-section': 4,
      'market-section': 5,
      'performance-section': 6,
      'performance-live-section': 7,
      'performance-sim-section': 8,
      'block-section': 9,
      'ops-monitor': 10,
      'event-section': 11,
      'journal-section': 12,
      'control-panel-card': 13,
    }};
  }};
  const getColumnCount = () => {{
    if (window.innerWidth >= 1280) return 3;
    if (window.innerWidth >= 900) return 2;
    return 1;
  }};
  const layoutCardColumns = (selector) => {{
    const container = document.querySelector(selector);
    if (!container) return;
    if (!container.__cards) {{
      container.__cards = Array.from(container.querySelectorAll(':scope > .card'));
    }}
    const layoutPriority = getLayoutPriority();
    const cards = (container.__cards || []).slice().sort((a, b) => {{
      const aOrder = layoutPriority[a.id || ''] ?? 999;
      const bOrder = layoutPriority[b.id || ''] ?? 999;
      if (aOrder !== bOrder) return aOrder - bOrder;
      return 0;
    }});
    container.innerHTML = '';
    const colCount = getColumnCount();
    const cols = Array.from({{ length: colCount }}, () => {{
      const col = document.createElement('div');
      col.className = 'masonry-col';
      container.appendChild(col);
      return col;
    }});
    const visibleCards = cards.filter((card) => {{
      const mainTab = card.dataset.dashboardTab || 'status';
      if (mainTab !== activeDashboardTab) return false;
      if (mainTab !== 'stocks') return true;
      return (card.dataset.stocksTab || 'focus') === activeStocksSubtab;
    }});
    if (selector === '.pro-grid' && activeDashboardTab === 'performance') {{
      container.innerHTML = '';
      const board = document.createElement('div');
      board.className = 'performance-board';
      const compareCard = visibleCards.find((card) => card.id === 'performance-section');
      const liveCard = visibleCards.find((card) => card.id === 'performance-live-section');
      const simCard = visibleCards.find((card) => card.id === 'performance-sim-section');
      if (compareCard) board.appendChild(compareCard);
      if (liveCard) board.appendChild(liveCard);
      if (simCard) board.appendChild(simCard);
      visibleCards
        .filter((card) => !['performance-section', 'performance-live-section', 'performance-sim-section'].includes(card.id || ''))
        .forEach((card) => board.appendChild(card));
      container.appendChild(board);
      return;
    }}
    visibleCards.forEach((card) => {{
      let target = cols[0];
      cols.forEach((col) => {{
        if (col.scrollHeight < target.scrollHeight) target = col;
      }});
      target.appendChild(card);
    }});
  }};
  const scheduleCardLayout = () => {{
    layoutCardColumns('.dashboard-hero');
    layoutCardColumns('.pro-grid');
  }};
  const scrollPageToEl = (el) => {{
    if (!el) return;
    el.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
  }};
  const buildFocusStatsHtml = (stats) => {{
    const rows = [
      ['거래 수', `${{stats.count}}건`],
      ['누적손익', stats.pnlText],
      ['평균수익률', stats.avgReturnText],
      ['마지막 청산', stats.latest || '-'],
    ];
    return rows.map(([label, value]) => `<div class='focus-stat'><div class='label'>${{label}}</div><div class='value'>${{value}}</div></div>`).join('');
  }};
  const buildFocusSparkline = (values, color) => {{
    if (!Array.isArray(values) || values.length === 0) {{
      return "<div class='k'>비교할 거래가 없습니다.</div>";
    }}
    const width = 320;
    const height = 96;
    const padding = 10;
    const minValue = Math.min(...values, 0);
    const maxValue = Math.max(...values, 0);
    const span = Math.max(1, maxValue - minValue);
    const zeroY = padding + ((maxValue - 0) / span) * (height - padding * 2);
    const xStep = values.length > 1 ? (width - padding * 2) / (values.length - 1) : 0;
    const points = values.map((value, idx) => {{
      const x = padding + xStep * idx;
      const y = padding + ((maxValue - value) / span) * (height - padding * 2);
      return `${{x.toFixed(1)}},${{y.toFixed(1)}}`;
    }}).join(' ');
    return (
      '<svg viewBox="0 0 ' + width + ' ' + height + '" preserveAspectRatio="none" aria-hidden="true">'
      + '<line x1="' + padding + '" y1="' + zeroY.toFixed(1) + '" x2="' + (width - padding) + '" y2="' + zeroY.toFixed(1) + '" stroke="rgba(148,163,184,0.35)" stroke-width="1" stroke-dasharray="4 4"></line>'
      + '<polyline fill="none" stroke="' + color + '" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" points="' + points + '"></polyline>'
      + '</svg>'
    );
  }};
  const extractFocusedTradeStats = (rows) => {{
    const normalized = (rows || []).map((row) => {{
      const pnl = parseFloat(row.dataset.pnl || '0');
      const ret = parseFloat(row.dataset.return || '0');
      const sellDate = row.dataset.sellDate || '';
      return {{ pnl, ret, sellDate }};
    }});
    const count = normalized.length;
    const totalPnl = normalized.reduce((sum, row) => sum + row.pnl, 0);
    const avgReturn = count ? normalized.reduce((sum, row) => sum + row.ret, 0) / count : 0;
    const latest = normalized
      .slice()
      .sort((a, b) => (Date.parse(b.sellDate || '') || 0) - (Date.parse(a.sellDate || '') || 0))[0]?.sellDate || '-';
    return {{
      count,
      totalPnl,
      pnlText: `${{totalPnl >= 0 ? '+' : ''}}${{Math.round(totalPnl).toLocaleString()}}`,
      avgReturn,
      avgReturnText: `${{avgReturn >= 0 ? '+' : ''}}${{avgReturn.toFixed(2)}}%`,
      latest,
      returns: normalized.map((row) => row.ret),
    }};
  }};
  const syncTradeFocusCard = () => {{
    if (!tradeFocusCompareCard) return;
    const liveNeedle = ((liveTradeSearch && liveTradeSearch.value) || '').trim().toLowerCase();
    const simNeedle = ((simTradeSearch && simTradeSearch.value) || '').trim().toLowerCase();
    const symbol = liveNeedle && simNeedle && liveNeedle === simNeedle ? liveNeedle : '';
    if (!symbol) {{
      tradeFocusCompareCard.style.display = 'none';
      return;
    }}
    const liveRows = Array.from(document.querySelectorAll(`#liveTradeTable tbody tr[data-symbol="${{symbol}}"]`));
    const simRows = Array.from(document.querySelectorAll(`#simTradeTable tbody tr[data-symbol="${{symbol}}"]`));
    if (liveRows.length === 0 && simRows.length === 0) {{
      tradeFocusCompareCard.style.display = 'none';
      return;
    }}
    const liveStats = extractFocusedTradeStats(liveRows);
    const simStats = extractFocusedTradeStats(simRows);
    tradeFocusCompareCard.style.display = 'grid';
    if (tradeFocusSymbolBadge) {{
      const labelSource = liveRows[0]?.querySelector('.trade-symbol-btn') || simRows[0]?.querySelector('.trade-symbol-btn');
      tradeFocusSymbolBadge.textContent = (labelSource && labelSource.textContent ? labelSource.textContent.trim() : symbol.toUpperCase()) || symbol.toUpperCase();
    }}
    if (tradeFocusLiveStats) tradeFocusLiveStats.innerHTML = buildFocusStatsHtml(liveStats);
    if (tradeFocusSimStats) tradeFocusSimStats.innerHTML = buildFocusStatsHtml(simStats);
    if (tradeFocusLiveChart) tradeFocusLiveChart.innerHTML = buildFocusSparkline(liveStats.returns, '#5eead4');
    if (tradeFocusSimChart) tradeFocusSimChart.innerHTML = buildFocusSparkline(simStats.returns, '#c084fc');
    if (tradeFocusLiveNote) tradeFocusLiveNote.textContent = liveStats.count ? ('최근 ' + liveStats.count + '건 실거래 수익률 흐름') : '실거래 비교 데이터가 없습니다.';
    if (tradeFocusSimNote) tradeFocusSimNote.textContent = simStats.count ? ('최근 ' + simStats.count + '건 시뮬레이션 수익률 흐름') : '시뮬레이션 비교 데이터가 없습니다.';
  }};
  tradeSymbolButtons.forEach((btn) => {{
    btn.addEventListener('click', () => {{
      const symbol = (btn.dataset.tradeSymbol || '').trim();
      if (!symbol) return;
      if (liveTradeSearch) {{
        liveTradeSearch.value = symbol;
        liveTradeSearch.dispatchEvent(new Event('input', {{ bubbles: true }}));
      }}
      if (simTradeSearch) {{
        simTradeSearch.value = symbol;
        simTradeSearch.dispatchEvent(new Event('input', {{ bubbles: true }}));
      }}
      syncTradeFocusCard();
    }});
  }});
  if (tradeFocusClearBtn) {{
    tradeFocusClearBtn.addEventListener('click', () => {{
      if (liveTradeSearch) {{
        liveTradeSearch.value = '';
        liveTradeSearch.dispatchEvent(new Event('input', {{ bubbles: true }}));
      }}
      if (simTradeSearch) {{
        simTradeSearch.value = '';
        simTradeSearch.dispatchEvent(new Event('input', {{ bubbles: true }}));
      }}
      syncTradeFocusCard();
    }});
  }}
  const applyDashboardTab = (key) => {{
    activeDashboardTab = key || getDefaultDashboardTab();
    safeSetLS(LS_DASHBOARD_TAB, activeDashboardTab);
    dashboardTabButtons.forEach((btn) => btn.classList.toggle('active', (btn.dataset.dashboardTab || '') === activeDashboardTab));
    mobileDockButtons.forEach((btn) => btn.classList.toggle('active', (btn.dataset.dashboardTabSwitch || '') === activeDashboardTab));
    if (stocksSubtabBar) stocksSubtabBar.style.display = activeDashboardTab === 'stocks' ? 'flex' : 'none';
    if (stocksSubtabHelp) stocksSubtabHelp.style.display = activeDashboardTab === 'stocks' ? 'block' : 'none';
    window.requestAnimationFrame(scheduleCardLayout);
  }};
  const applyStocksSubtab = (key) => {{
    activeStocksSubtab = key || 'focus';
    safeSetLS(LS_STOCKS_SUBTAB, activeStocksSubtab);
    stocksSubtabButtons.forEach((btn) => btn.classList.toggle('active', (btn.dataset.stocksTab || '') === activeStocksSubtab));
    if (activeDashboardTab === 'stocks') {{
      window.requestAnimationFrame(scheduleCardLayout);
    }}
  }};
  window.addEventListener('resize', () => {{
    window.requestAnimationFrame(scheduleCardLayout);
  }});
  dashboardTabButtons.forEach((btn) => {{
    btn.addEventListener('click', () => applyDashboardTab(btn.dataset.dashboardTab || 'status'));
  }});
  stocksSubtabButtons.forEach((btn) => {{
    btn.addEventListener('click', () => {{
      applyDashboardTab('stocks');
      applyStocksSubtab(btn.dataset.stocksTab || 'focus');
    }});
  }});
  Array.from(document.querySelectorAll('[data-jump-target]')).forEach((btn) => {{
    btn.addEventListener('click', (e) => {{
      e.preventDefault();
      const mainTab = btn.dataset.jumpTab || 'status';
      const stocksTab = btn.dataset.jumpStocks || '';
      const targetId = btn.dataset.jumpTarget || '';
      applyDashboardTab(mainTab);
      if (mainTab === 'stocks' && stocksTab) {{
        applyStocksSubtab(stocksTab);
      }}
      window.requestAnimationFrame(() => {{
        const el = document.getElementById(targetId);
        if (el) el.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
      }});
    }});
  }});
  mobileDockButtons.forEach((btn) => {{
    btn.addEventListener('click', (e) => {{
      e.preventDefault();
      applyDashboardTab(btn.dataset.dashboardTabSwitch || 'status');
      const targetId = btn.getAttribute('href');
      if (targetId && targetId.startsWith('#') && targetId.length > 1) {{
        const el = document.querySelector(targetId);
        if (el) el.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
      }} else {{
        window.scrollTo({{ top: 0, behavior: 'smooth' }});
      }}
    }});
  }});
  applyStocksSubtab(getDefaultStocksSubtab());
  applyDashboardTab(getDefaultDashboardTab());
  if (perfTabs.length > 0 && perfPanels.length > 0) {{
    const activatePerfTab = (key) => {{
      perfTabs.forEach((btn) => btn.classList.toggle('active', (btn.dataset.perfTab || '') === key));
      perfPanels.forEach((panel) => panel.classList.toggle('active', (panel.dataset.perfPanel || '') === key));
      scheduleMasonry();
    }};
    perfTabs.forEach((btn) => {{
      btn.addEventListener('click', () => activatePerfTab(btn.dataset.perfTab || 'live'));
    }});
    activatePerfTab(getDefaultPerfTab());
  }}
  if (stockTabs.length > 0 && stockPanels.length > 0) {{
    const applyStockSearch = () => {{
      const needle = ((stockBoardSearch && stockBoardSearch.value) || '').trim().toLowerCase();
      stockPanels.forEach((panel) => {{
        const cards = Array.from(panel.querySelectorAll('.stock-card'));
        let visible = 0;
        cards.forEach((card) => {{
          const hay = (card.getAttribute('data-stock-search') || '').toLowerCase();
          const ok = !needle || hay.includes(needle);
          card.style.display = ok ? '' : 'none';
          if (ok) visible += 1;
        }});
        let empty = panel.querySelector('.stock-empty-search');
        if (!empty) {{
          empty = document.createElement('div');
          empty.className = 'k stock-empty-search';
          empty.style.marginTop = '8px';
          panel.appendChild(empty);
        }}
        empty.textContent = visible === 0 ? '검색 조건에 맞는 종목이 없습니다.' : '';
      }});
      scheduleMasonry();
    }};
    const activateStockTab = (key) => {{
      stockTabs.forEach((btn) => btn.classList.toggle('active', (btn.dataset.stockTab || '') === key));
      stockPanels.forEach((panel) => panel.classList.toggle('active', (panel.dataset.stockPanel || '') === key));
      applyStockSearch();
      scheduleMasonry();
    }};
    stockTabs.forEach((btn) => {{
      btn.addEventListener('click', () => activateStockTab(btn.dataset.stockTab || 'candidate'));
    }});
    if (stockBoardSearch) stockBoardSearch.addEventListener('input', applyStockSearch);
    Array.from(document.querySelectorAll('[data-stock-quick-filter]')).forEach((btn) => {{
      btn.addEventListener('click', () => {{
        if (!stockBoardSearch) return;
        applyDashboardTab('stocks');
        applyStocksSubtab('board');
        const key = btn.dataset.stockQuickFilter || '';
        if (key === 'a_grade') {{
          stockBoardSearch.value = 'a급 오프닝';
        }} else if (key === 'priority') {{
          stockBoardSearch.value = '수급 동행';
        }}
        activateStockTab(getDefaultBoardTab());
        applyStockSearch();
        const board = document.getElementById('board-section');
        if (board) board.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
      }});
    }});
    activateStockTab(getDefaultBoardTab());
  }}
  if (simTradeTable && simResultFilter && simSortOrder) {{
    const simBody = simTradeTable.querySelector('tbody');
    const simRows = Array.from(simBody ? simBody.querySelectorAll('tr[data-result]') : []);
    const renderSimRows = () => {{
      if (!simBody || simRows.length === 0) return;
      const resultFilter = simResultFilter.value || 'all';
      const sortOrder = simSortOrder.value || 'sell_desc';
      const search = ((simTradeSearch && simTradeSearch.value) || '').trim().toLowerCase();
      const filtered = simRows.filter((row) => {{
        const matchesResult = resultFilter === 'all' || row.dataset.result === resultFilter;
        const matchesSearch = !search || row.textContent.toLowerCase().includes(search);
        return matchesResult && matchesSearch;
      }});
      const sorted = filtered.slice().sort((a, b) => {{
        const aSell = Date.parse(a.dataset.sellDate || '') || 0;
        const bSell = Date.parse(b.dataset.sellDate || '') || 0;
        const aReturn = parseFloat(a.dataset.return || '0');
        const bReturn = parseFloat(b.dataset.return || '0');
        if (sortOrder === 'sell_asc') return aSell - bSell;
        if (sortOrder === 'return_desc') return bReturn - aReturn;
        if (sortOrder === 'return_asc') return aReturn - bReturn;
        return bSell - aSell;
      }});
      simBody.innerHTML = '';
      if (sorted.length === 0) {{
        simBody.innerHTML = "<tr><td colspan='10'>선택한 조건에 맞는 시뮬레이션 거래가 없습니다.</td></tr>";
        syncTradeFocusCard();
        return;
      }}
      sorted.forEach((row) => {{
        const rowSymbol = ((row.dataset.symbol || '').trim().toLowerCase());
        const exactFocus = !!search && rowSymbol === search;
        row.classList.toggle('trade-row-focus', exactFocus);
        simBody.appendChild(row);
      }});
      syncTradeFocusCard();
    }};
    simResultFilter.addEventListener('change', renderSimRows);
    simSortOrder.addEventListener('change', renderSimRows);
    if (simTradeSearch) simTradeSearch.addEventListener('input', renderSimRows);
    if (simTradeClearBtn) {{
      simTradeClearBtn.addEventListener('click', () => {{
        if (simTradeSearch) {{
          simTradeSearch.value = '';
          simTradeSearch.dispatchEvent(new Event('input', {{ bubbles: true }}));
        }} else {{
          renderSimRows();
        }}
        syncTradeFocusCard();
      }});
    }}
    renderSimRows();
    scheduleMasonry();
  }}
  if (liveTradeTable && liveResultFilter && liveSortOrder) {{
    const liveBody = liveTradeTable.querySelector('tbody');
    const liveRows = Array.from(liveBody ? liveBody.querySelectorAll('tr[data-result]') : []);
    const renderLiveRows = () => {{
      if (!liveBody || liveRows.length === 0) return;
      const resultFilter = liveResultFilter.value || 'all';
      const sortOrder = liveSortOrder.value || 'sell_desc';
      const search = ((liveTradeSearch && liveTradeSearch.value) || '').trim().toLowerCase();
      const filtered = liveRows.filter((row) => {{
        const matchesResult = resultFilter === 'all' || row.dataset.result === resultFilter;
        const matchesSearch = !search || row.textContent.toLowerCase().includes(search);
        return matchesResult && matchesSearch;
      }});
      const sorted = filtered.slice().sort((a, b) => {{
        const aSell = Date.parse(a.dataset.sellDate || '') || 0;
        const bSell = Date.parse(b.dataset.sellDate || '') || 0;
        const aReturn = parseFloat(a.dataset.return || '0');
        const bReturn = parseFloat(b.dataset.return || '0');
        if (sortOrder === 'sell_asc') return aSell - bSell;
        if (sortOrder === 'return_desc') return bReturn - aReturn;
        if (sortOrder === 'return_asc') return aReturn - bReturn;
        return bSell - aSell;
      }});
      liveBody.innerHTML = '';
      if (sorted.length === 0) {{
        liveBody.innerHTML = "<tr><td colspan='10'>선택한 조건에 맞는 실거래 이력이 없습니다.</td></tr>";
        syncTradeFocusCard();
        return;
      }}
      sorted.forEach((row) => {{
        const rowSymbol = ((row.dataset.symbol || '').trim().toLowerCase());
        const exactFocus = !!search && rowSymbol === search;
        row.classList.toggle('trade-row-focus', exactFocus);
        liveBody.appendChild(row);
      }});
      syncTradeFocusCard();
    }};
    liveResultFilter.addEventListener('change', renderLiveRows);
    liveSortOrder.addEventListener('change', renderLiveRows);
    if (liveTradeSearch) liveTradeSearch.addEventListener('input', renderLiveRows);
    if (liveTradeClearBtn) {{
      liveTradeClearBtn.addEventListener('click', () => {{
        if (liveTradeSearch) {{
          liveTradeSearch.value = '';
          liveTradeSearch.dispatchEvent(new Event('input', {{ bubbles: true }}));
        }} else {{
          renderLiveRows();
        }}
        syncTradeFocusCard();
      }});
    }}
    renderLiveRows();
    scheduleMasonry();
  }}
  if (simulationRunForm && simulationRunBtn) {{
    const simulationProfileSelect = document.getElementById('simulationProfileSelect');
    const simulationProfilePrefs = {sim_profile_pref_json};
    const simulationProfileMeta = {sim_profile_meta_preview_json};
    const simPrefInputs = Array.from(document.querySelectorAll('[data-sim-pref]'));
    const simFormTitle = document.getElementById('simFormTitle');
    const simFormHint = document.getElementById('simFormHint');
    const simScopedFields = Array.from(document.querySelectorAll('[data-sim-scope]'));
    const profileUses = {{
      short_term: ['all', 'short_term'],
      rolling_rank: ['all', 'rolling_rank'],
      short_horizon: ['all', 'short_horizon'],
      daily_selection: ['all', 'daily_selection'],
      rank_weighted: ['all', 'rank_weighted'],
      intraday_replay: ['all', 'intraday_replay'],
    }};
    const syncSimulationPreview = (profile) => {{
      const meta = simulationProfileMeta[profile] || simulationProfileMeta.short_term || {{}};
      if (simFormTitle) simFormTitle.textContent = meta.form_title || '시뮬레이션 설정';
      if (simFormHint) simFormHint.textContent = meta.form_hint || '현재 프로필에 필요한 입력만 확인하면 됩니다.';
    }};
    const syncSimulationFieldVisibility = (profile) => {{
      const allowed = new Set(profileUses[profile] || ['all']);
      simScopedFields.forEach((el) => {{
        const scopes = String(el.getAttribute('data-sim-scope') || 'all').split(/\\s+/).filter(Boolean);
        const visible = scopes.some((scope) => allowed.has(scope));
        el.style.display = visible ? '' : 'none';
      }});
    }};
    const applySimulationProfilePrefs = (profile) => {{
      const prefs = simulationProfilePrefs[profile] || simulationProfilePrefs.short_term || {{}};
      simPrefInputs.forEach((el) => {{
        const prefKey = el.dataset.simPref || '';
        if (!prefKey || !(prefKey in prefs)) return;
        const nextValue = prefs[prefKey];
        if (el.type === 'checkbox') {{
          el.checked = !!nextValue;
        }} else {{
          el.value = `${{nextValue ?? ''}}`;
        }}
      }});
      syncSimulationFieldVisibility(profile);
      syncSimulationPreview(profile);
    }};
    if (simulationProfileSelect) {{
      simulationProfileSelect.addEventListener('change', () => {{
        applySimulationProfilePrefs(simulationProfileSelect.value || 'short_term');
      }});
      syncSimulationPreview(simulationProfileSelect.value || 'short_term');
      syncSimulationFieldVisibility(simulationProfileSelect.value || 'short_term');
    }}
    simulationRunForm.addEventListener('submit', () => {{
      simulationRunBtn.disabled = true;
      simulationRunBtn.textContent = '시뮬레이션 실행 중...';
    }});
  }}

  const checklistItems = Array.from(document.querySelectorAll('.check-item'));
  const LS_CHECK = 'aitrader_checklist_state_v1';
  const checklistResetBtn = document.getElementById('checklistResetBtn');
  const checklistResetBtnHelp = document.getElementById('checklistResetBtnHelp');
  const startForm = document.getElementById('startForm');
  const liveModeForm = document.getElementById('liveModeForm');
  const alertRows = Array.from(document.querySelectorAll('.alert-row'));
  const alertAckBtns = Array.from(document.querySelectorAll('.alert-ack-btn'));
  const alertResetBtn = document.getElementById('alertResetBtn');
  const tradeMode = '{settings.trade_mode}';
  const liveArmed = {str(bool(settings.live_armed)).lower()};
  const sessionProfileLabel = '{html.escape(_display_label(st.get("session_profile"), kind="session"))}';
  const diagOk = {str(bool(diag.get("ok"))).lower()};
  const diagUpdatedAt = '{html.escape(str(diag.get("updated_at") or ""))}';
  if (checklistItems.length > 0) {{
    let checkState = {{}};
    try {{
      const raw = safeGetLS(LS_CHECK);
      const parsed = raw ? JSON.parse(raw) : {{}};
      if (parsed && typeof parsed === 'object') checkState = parsed;
    }} catch (_e) {{
      checkState = {{}};
    }}
    const syncById = (id, value) => {{
      checklistItems.forEach((el) => {{
        if ((el.dataset.checkId || '') === id) {{
          el.checked = !!value;
        }}
      }});
    }};
    checklistItems.forEach((el) => {{
      const id = el.dataset.checkId || '';
      if (!id) return;
      el.checked = !!checkState[id];
      el.addEventListener('change', () => {{
        const value = !!el.checked;
        checkState[id] = value;
        syncById(id, value);
        safeSetLS(LS_CHECK, JSON.stringify(checkState));
        window.dispatchEvent(new Event('checklist-updated'));
      }});
    }});
    const clearAll = () => {{
      checkState = {{}};
      checklistItems.forEach((el) => {{
        el.checked = false;
      }});
      safeSetLS(LS_CHECK, JSON.stringify(checkState));
      window.dispatchEvent(new Event('checklist-updated'));
    }};
    if (checklistResetBtn) {{
      checklistResetBtn.addEventListener('click', clearAll);
    }}
    if (checklistResetBtnHelp) {{
      checklistResetBtnHelp.addEventListener('click', clearAll);
    }}
  }}

  const REQUIRED_CHECK_IDS = [
    'beginner-1','beginner-2','beginner-3','beginner-4',
    'intermediate-1','intermediate-2','intermediate-3','intermediate-4',
    'live-1','live-2','live-3','live-4',
  ];
  const readChecklistState = () => {{
    try {{
      const raw = safeGetLS(LS_CHECK);
      const parsed = raw ? JSON.parse(raw) : {{}};
      return (parsed && typeof parsed === 'object') ? parsed : {{}};
    }} catch (_e) {{
      return {{}};
    }}
  }};
  const checklistPendingCount = () => {{
    const st = readChecklistState();
    return REQUIRED_CHECK_IDS.filter((id) => !st[id]).length;
  }};

  const LS_ALERT_ACK = 'aitrader_alert_ack_v1';
  const setChipStrong = (id, value) => {{
    const el = document.getElementById(id);
    if (!el) return;
    const strong = el.querySelector('strong');
    if (strong) strong.textContent = String(value ?? '-');
  }};
  const refreshSessionWidgets = () => {{
    const modeLabel = tradeMode === 'LIVE' ? (liveArmed ? '실거래 / 주문허용' : '실거래 / 주문차단') : '모의투자';
    setChipStrong('sessionModeChip', modeLabel);
    setChipStrong('sessionProfileChip', sessionProfileLabel || '-');
    setChipStrong('sessionDiagChip', (diagOk && diagUpdatedAt) ? diagUpdatedAt : '-');
    setChipStrong('sessionChecklistChip', checklistPendingCount());
  }};
  refreshSessionWidgets();
  window.addEventListener('checklist-updated', refreshSessionWidgets);
  const diagForms = Array.from(document.querySelectorAll(\"form[action='/diagnostics-run']\"));
  diagForms.forEach((f) => {{
    f.addEventListener('submit', refreshSessionWidgets);
  }});

  // Alert center acknowledge
  const getAlertAck = () => {{
    try {{
      const raw = safeGetLS(LS_ALERT_ACK);
      const parsed = raw ? JSON.parse(raw) : {{}};
      return (parsed && typeof parsed === 'object') ? parsed : {{}};
    }} catch (_e) {{
      return {{}};
    }}
  }};
  const setAlertAck = (v) => {{
    safeSetLS(LS_ALERT_ACK, JSON.stringify(v || {{}}));
  }};
  const applyAlertAck = () => {{
    const ack = getAlertAck();
    alertRows.forEach((row) => {{
      const key = row.dataset.alertKey || '';
      row.style.display = ack[key] ? 'none' : '';
    }});
  }};
  alertAckBtns.forEach((btn) => {{
    btn.addEventListener('click', () => {{
      const key = btn.dataset.alertKey || '';
      if (!key) return;
      const ack = getAlertAck();
      ack[key] = true;
      setAlertAck(ack);
      applyAlertAck();
    }});
  }});
  if (alertResetBtn) {{
    alertResetBtn.addEventListener('click', () => {{
      setAlertAck({{}});
      applyAlertAck();
    }});
  }}
  applyAlertAck();

  const guideModal = document.getElementById('guideModal');
  const guideOpenBtn = document.getElementById('guideOpenBtn');
  const guideCloseBtn = document.getElementById('guideCloseBtn');
  const guideBackdrop = document.getElementById('guideBackdrop');
  const modal = document.getElementById('helpModal');
  const openBtn = document.getElementById('helpOpenBtn');
  const closeBtn = document.getElementById('helpCloseBtn');
  const backdrop = document.getElementById('helpBackdrop');
  const configModal = document.getElementById('configModal');
  const configOpen = document.getElementById('configOpenBtn');
  const configClose = document.getElementById('configCloseBtn');
  const configCancel = document.getElementById('configCancelBtn');
  const configBackdrop = document.getElementById('configBackdrop');
  const configForm = document.getElementById('configForm');
  const cfgTradeMode = document.getElementById('cfg_trade_mode');
  const cfgCooldown = document.getElementById('cfg_trade_cooldown_sec');
  const cfgStaleAge = document.getElementById('cfg_stale_data_max_age_sec');
  const cfgTargetRisk = document.getElementById('cfg_target_risk_per_trade_pct');
  const cfgDailyLoss = document.getElementById('cfg_daily_loss_limit_pct');
  const cfgMaxSymbolLoss = document.getElementById('cfg_max_symbol_loss_pct');
  const cfgMaxPortfolioHeat = document.getElementById('cfg_max_portfolio_heat_pct');
  const cfgSignalConfirm = document.getElementById('cfg_signal_confirm_cycles');
  const cfgBarIntervalMinutes = document.getElementById('cfg_bar_interval_minutes');
  const cfgAtrLookback = document.getElementById('cfg_atr_exit_lookback_days');
  const cfgAtrStop = document.getElementById('cfg_atr_stop_mult');
  const cfgAtrTake = document.getElementById('cfg_atr_take_mult');
  const cfgAtrTrail = document.getElementById('cfg_atr_trailing_mult');
  const cfgMaxActivePositions = document.getElementById('cfg_max_active_positions');
  const cfgCandidateRefreshTopN = document.getElementById('cfg_candidate_refresh_top_n');
  const cfgCandidateRefreshMinutes = document.getElementById('cfg_candidate_refresh_minutes');
  const cfgIntradayReselectMinutes = document.getElementById('cfg_intraday_reselect_minutes');
  const cfgTrendSelectCount = document.getElementById('cfg_trend_select_count');
  const cfgTrendMinAvgTurnover20 = document.getElementById('cfg_trend_min_avg_turnover20_krw');
  const cfgTrendTurnoverRatio = document.getElementById('cfg_trend_turnover_ratio');
  const cfgTrendValueSpikeRatio = document.getElementById('cfg_trend_value_spike_ratio');
  const cfgTrendBreakoutBuffer = document.getElementById('cfg_trend_breakout_buffer');
  const cfgTrendMinAtr14 = document.getElementById('cfg_trend_min_atr14');
  const cfgTrendMaxAtr14 = document.getElementById('cfg_trend_max_atr14');
  const cfgTrendOverheatDay = document.getElementById('cfg_trend_overheat_day');
  const cfgTrendOverheat2Day = document.getElementById('cfg_trend_overheat_2day');
  const cfgTrendDailyRsiMin = document.getElementById('cfg_trend_daily_rsi_min');
  const cfgTrendDailyRsiMax = document.getElementById('cfg_trend_daily_rsi_max');
  const cfgTrendGapSkipUp = document.getElementById('cfg_trend_gap_skip_up');
  const cfgTrendGapSkipDown = document.getElementById('cfg_trend_gap_skip_down');
  const cfgTrendMaxChaseFromOpen = document.getElementById('cfg_trend_max_chase_from_open');
  const cfgTrendMaxSectorNames = document.getElementById('cfg_trend_max_sector_names');
  if (guideModal && guideOpenBtn && guideCloseBtn && guideBackdrop) {{
    const openGuide = () => {{
      guideModal.classList.add('show');
      guideModal.setAttribute('aria-hidden', 'false');
      document.body.style.overflow = 'hidden';
      drawCountdown();
    }};
    const closeGuide = () => {{
      guideModal.classList.remove('show');
      guideModal.setAttribute('aria-hidden', 'true');
      document.body.style.overflow = '';
      drawCountdown();
    }};
    guideOpenBtn.addEventListener('click', openGuide);
    guideCloseBtn.addEventListener('click', closeGuide);
    guideBackdrop.addEventListener('click', closeGuide);
    document.addEventListener('keydown', (e) => {{
      if (e.key === 'Escape') closeGuide();
    }});
  }}

  if (modal && openBtn && closeBtn && backdrop) {{
    const open = () => {{
      modal.classList.add('show');
      modal.setAttribute('aria-hidden', 'false');
      document.body.style.overflow = 'hidden';
      drawCountdown();
    }};
    const close = () => {{
      modal.classList.remove('show');
      modal.setAttribute('aria-hidden', 'true');
      document.body.style.overflow = '';
      drawCountdown();
    }};
    openBtn.addEventListener('click', open);
    closeBtn.addEventListener('click', close);
    backdrop.addEventListener('click', close);
    document.addEventListener('keydown', (e) => {{
      if (e.key === 'Escape') close();
    }});
  }}

  if (configModal && configOpen && configClose && configBackdrop) {{
    const openCfg = () => {{
      configModal.classList.add('show');
      configModal.setAttribute('aria-hidden', 'false');
      document.body.style.overflow = 'hidden';
      drawCountdown();
    }};
    const closeCfg = () => {{
      configModal.classList.remove('show');
      configModal.setAttribute('aria-hidden', 'true');
      document.body.style.overflow = '';
      drawCountdown();
    }};
    configOpen.addEventListener('click', openCfg);
    configClose.addEventListener('click', closeCfg);
    configBackdrop.addEventListener('click', closeCfg);
    if (configCancel) configCancel.addEventListener('click', closeCfg);
    document.addEventListener('keydown', (e) => {{
        if (e.key === 'Escape') closeCfg();
      }});
  }}
  if (startForm && tradeMode === 'LIVE' && !liveArmed) {{
    startForm.addEventListener('submit', (e) => {{
      e.preventDefault();
      window.alert('LIVE 모드지만 LIVE_ARMED가 OFF입니다. 설정에서 LIVE_ARMED를 켜세요.');
    }});
  }}
  if (liveModeForm) {{
    liveModeForm.addEventListener('submit', (e) => {{
      const ok = window.confirm('LIVE 모드로 전환합니다. 실제 주문이 발생할 수 있습니다. 계속할까요?');
      if (!ok) e.preventDefault();
    }});
  }}
  if (configForm) {{
    configForm.addEventListener('submit', (e) => {{
      const mode = (cfgTradeMode?.value || '').trim().toUpperCase();
      const cooldown = parseInt((cfgCooldown?.value || '').trim(), 10);
      const staleAge = parseInt((cfgStaleAge?.value || '').trim(), 10);
      const targetRisk = parseFloat((cfgTargetRisk?.value || '').trim());
      const dailyLoss = parseFloat((cfgDailyLoss?.value || '').trim());
      const maxSymbolLoss = parseFloat((cfgMaxSymbolLoss?.value || '').trim());
      const maxPortfolioHeat = parseFloat((cfgMaxPortfolioHeat?.value || '').trim());
      const signalConfirm = parseInt((cfgSignalConfirm?.value || '').trim(), 10);
      const barIntervalMinutes = parseInt((cfgBarIntervalMinutes?.value || '').trim(), 10);
      const atrLookback = parseInt((cfgAtrLookback?.value || '').trim(), 10);
      const atrStop = parseFloat((cfgAtrStop?.value || '').trim());
      const atrTake = parseFloat((cfgAtrTake?.value || '').trim());
      const atrTrail = parseFloat((cfgAtrTrail?.value || '').trim());
      const maxActivePositions = parseInt((cfgMaxActivePositions?.value || '').trim(), 10);
      const candidateRefreshTopN = parseInt((cfgCandidateRefreshTopN?.value || '').trim(), 10);
      const candidateRefreshMinutes = parseInt((cfgCandidateRefreshMinutes?.value || '').trim(), 10);
      const intradayReselectMinutes = parseInt((cfgIntradayReselectMinutes?.value || '').trim(), 10);
      const trendSelectCount = parseInt((cfgTrendSelectCount?.value || '').trim(), 10);
      const trendMinAvgTurnover20 = parseFloat((cfgTrendMinAvgTurnover20?.value || '').trim());
      const trendTurnoverRatio = parseFloat((cfgTrendTurnoverRatio?.value || '').trim());
      const trendValueSpikeRatio = parseFloat((cfgTrendValueSpikeRatio?.value || '').trim());
      const trendBreakoutBuffer = parseFloat((cfgTrendBreakoutBuffer?.value || '').trim());
      const trendMinAtr14 = parseFloat((cfgTrendMinAtr14?.value || '').trim());
      const trendMaxAtr14 = parseFloat((cfgTrendMaxAtr14?.value || '').trim());
      const trendOverheatDay = parseFloat((cfgTrendOverheatDay?.value || '').trim());
      const trendOverheat2Day = parseFloat((cfgTrendOverheat2Day?.value || '').trim());
      const trendDailyRsiMin = parseFloat((cfgTrendDailyRsiMin?.value || '').trim());
      const trendDailyRsiMax = parseFloat((cfgTrendDailyRsiMax?.value || '').trim());
      const trendGapSkipUp = parseFloat((cfgTrendGapSkipUp?.value || '').trim());
      const trendGapSkipDown = parseFloat((cfgTrendGapSkipDown?.value || '').trim());
      const trendMaxChaseFromOpen = parseFloat((cfgTrendMaxChaseFromOpen?.value || '').trim());
      const trendMaxSectorNames = parseInt((cfgTrendMaxSectorNames?.value || '').trim(), 10);
      const errors = [];
      if (!['DRY', 'LIVE'].includes(mode)) errors.push('TRADE_MODE: DRY 또는 LIVE');
      if (!Number.isFinite(cooldown) || cooldown < 1 || cooldown > 86400) errors.push('TRADE_COOLDOWN_SEC: 1~86400');
      if (!Number.isFinite(staleAge) || staleAge < 5 || staleAge > 3600) errors.push('STALE_DATA_MAX_AGE_SEC: 5~3600');
      if (!Number.isFinite(targetRisk) || targetRisk < 0.1 || targetRisk > 5.0) errors.push('TARGET_RISK_PER_TRADE_PCT: 0.1~5.0');
      if (!Number.isFinite(dailyLoss) || dailyLoss > -0.1 || dailyLoss < -20.0) errors.push('DAILY_LOSS_LIMIT_PCT: -20.0~-0.1');
      if (!Number.isFinite(maxSymbolLoss) || maxSymbolLoss > -0.1 || maxSymbolLoss < -30.0) errors.push('MAX_SYMBOL_LOSS_PCT: -30.0~-0.1');
      if (!Number.isFinite(maxPortfolioHeat) || maxPortfolioHeat < 1.0 || maxPortfolioHeat > 100.0) errors.push('MAX_PORTFOLIO_HEAT_PCT: 1.0~100.0');
      if (!Number.isFinite(signalConfirm) || signalConfirm < 1 || signalConfirm > 5) errors.push('SIGNAL_CONFIRM_CYCLES: 1~5');
      if (!Number.isFinite(barIntervalMinutes) || barIntervalMinutes < 1 || barIntervalMinutes > 60) errors.push('BAR_INTERVAL_MINUTES: 1~60');
      if (!Number.isFinite(atrLookback) || atrLookback < 5 || atrLookback > 60) errors.push('ATR_EXIT_LOOKBACK_DAYS: 5~60');
      if (!Number.isFinite(atrStop) || atrStop < 0.5 || atrStop > 6.0) errors.push('ATR_STOP_MULT: 0.5~6.0');
      if (!Number.isFinite(atrTake) || atrTake < 0.5 || atrTake > 8.0) errors.push('ATR_TAKE_MULT: 0.5~8.0');
      if (!Number.isFinite(atrTrail) || atrTrail < 0.5 || atrTrail > 6.0) errors.push('ATR_TRAILING_MULT: 0.5~6.0');
      if (!Number.isFinite(maxActivePositions) || maxActivePositions < 1 || maxActivePositions > 100) errors.push('MAX_ACTIVE_POSITIONS: 1~100');
      if (!Number.isFinite(candidateRefreshTopN) || candidateRefreshTopN < 1 || candidateRefreshTopN > 5000) errors.push('CANDIDATE_REFRESH_TOP_N: 1~5000');
      if (!Number.isFinite(candidateRefreshMinutes) || candidateRefreshMinutes < 1 || candidateRefreshMinutes > 1440) errors.push('CANDIDATE_REFRESH_MINUTES: 1~1440');
      if (!Number.isFinite(intradayReselectMinutes) || intradayReselectMinutes < 1 || intradayReselectMinutes > 240) errors.push('INTRADAY_RESELECT_MINUTES: 1~240');
      if (!Number.isFinite(trendSelectCount) || trendSelectCount < 1 || trendSelectCount > 100) errors.push('TREND_SELECT_COUNT: 1~100');
      if (!Number.isFinite(trendMinAvgTurnover20) || trendMinAvgTurnover20 < 0 || trendMinAvgTurnover20 > 100000000000000) errors.push('TREND_MIN_AVG_TURNOVER20_KRW: 0~100000000000000');
      if (!Number.isFinite(trendTurnoverRatio) || trendTurnoverRatio < 0.5 || trendTurnoverRatio > 10.0) errors.push('TREND_MIN_TURNOVER_RATIO_5_TO_20: 0.5~10.0');
      if (!Number.isFinite(trendValueSpikeRatio) || trendValueSpikeRatio < 0.5 || trendValueSpikeRatio > 10.0) errors.push('TREND_MIN_VALUE_SPIKE_RATIO: 0.5~10.0');
      if (!Number.isFinite(trendBreakoutBuffer) || trendBreakoutBuffer < 0.1 || trendBreakoutBuffer > 20.0) errors.push('TREND_BREAKOUT_BUFFER_PCT: 0.1~20.0');
      if (!Number.isFinite(trendMinAtr14) || trendMinAtr14 < 0.1 || trendMinAtr14 > 20.0) errors.push('TREND_MIN_ATR14_PCT: 0.1~20.0');
      if (!Number.isFinite(trendMaxAtr14) || trendMaxAtr14 < trendMinAtr14 || trendMaxAtr14 > 30.0) errors.push('TREND_MAX_ATR14_PCT: min~30.0');
      if (!Number.isFinite(trendOverheatDay) || trendOverheatDay < 1.0 || trendOverheatDay > 30.0) errors.push('TREND_OVERHEAT_DAY_PCT: 1.0~30.0');
      if (!Number.isFinite(trendOverheat2Day) || trendOverheat2Day < trendOverheatDay || trendOverheat2Day > 50.0) errors.push('TREND_OVERHEAT_2DAY_PCT: day~50.0');
      if (!Number.isFinite(trendDailyRsiMin) || trendDailyRsiMin < 0.0 || trendDailyRsiMin > 100.0) errors.push('TREND_DAILY_RSI_MIN: 0~100');
      if (!Number.isFinite(trendDailyRsiMax) || trendDailyRsiMax < trendDailyRsiMin || trendDailyRsiMax > 100.0) errors.push('TREND_DAILY_RSI_MAX: min~100');
      if (!Number.isFinite(trendGapSkipUp) || trendGapSkipUp < 0.0 || trendGapSkipUp > 30.0) errors.push('TREND_GAP_SKIP_UP_PCT: 0~30');
      if (!Number.isFinite(trendGapSkipDown) || trendGapSkipDown > 0.0 || trendGapSkipDown < -30.0) errors.push('TREND_GAP_SKIP_DOWN_PCT: -30~0');
      if (!Number.isFinite(trendMaxChaseFromOpen) || trendMaxChaseFromOpen < 0.0 || trendMaxChaseFromOpen > 30.0) errors.push('TREND_MAX_CHASE_FROM_OPEN_PCT: 0~30');
      if (!Number.isFinite(trendMaxSectorNames) || trendMaxSectorNames < 1 || trendMaxSectorNames > 10) errors.push('TREND_MAX_SECTOR_NAMES: 1~10');
      if (errors.length > 0) {{
        e.preventDefault();
        window.alert('설정 값 확인 필요\\n- ' + errors.join('\\n- '));
      }}
    }});
  }}

  const installBtn = document.getElementById('installAppBtn');
  const nativeInstallBtn = document.getElementById('nativeInstallBtn');
  const installGuideBtn = document.getElementById('installGuideBtn');
  const installHint = document.getElementById('installAppHint');
  const installTopbar = document.getElementById('installTopbar');
  const installTopbarMsg = document.getElementById('installTopbarMsg');
  const installTopbarSub = document.getElementById('installTopbarSub');
  const installTopbarPrimaryBtn = document.getElementById('installTopbarPrimaryBtn');
  const installTopbarGuideBtn = document.getElementById('installTopbarGuideBtn');
  const installHeroCard = document.getElementById('installHeroCard');
  const installHeroTitle = document.getElementById('installHeroTitle');
  const installHeroText = document.getElementById('installHeroText');
  const installHeroPrimaryBtn = document.getElementById('installHeroPrimaryBtn');
  const installHeroPwaBtn = document.getElementById('installHeroPwaBtn');
  const installHeroGuideBtn = document.getElementById('installHeroGuideBtn');
  const installHeroBadge = document.getElementById('installHeroBadge');
  const installServerValue = document.getElementById('installServerValue');
  const installServerText = document.getElementById('installServerText');
  const installServerOpenAppBtn = document.getElementById('installServerOpenAppBtn');
  const installServerCopyBtn = document.getElementById('installServerCopyBtn');
  const installServerGuideBtn = document.getElementById('installServerGuideBtn');
  const installServerQrBox = document.getElementById('installServerQrBox');
  const installServerQrImg = document.getElementById('installServerQrImg');
  const installServerQrNote = document.getElementById('installServerQrNote');
  const installConfig = {{
    iosTestflightUrl: {install_testflight_url_js},
    iosAppStoreUrl: {install_app_store_url_js},
    iosManifestUrl: {install_manifest_url_js},
    mobileServerUrl: {install_mobile_server_url_js},
    mobileServerLabel: {install_mobile_server_label_js},
    mobileAppScheme: {install_mobile_app_scheme_js},
  }};
  let deferredInstallPrompt = null;
  const isStandalone = (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) || !!(window.navigator && window.navigator.standalone);
  const ua = navigator.userAgent || '';
  const isIOS = /iPhone|iPad|iPod/i.test(ua);

  const buildManifestInstallHref = (rawUrl) => {{
    const value = `${{rawUrl || ''}}`.trim();
    if (!value) return '';
    if (value.startsWith('itms-services://')) return value;
    return `itms-services://?action=download-manifest&url=${{encodeURIComponent(value)}}`;
  }};

  const buildAppOpenHref = () => {{
    const mobileServerUrl = `${{installConfig.mobileServerUrl || ''}}`.trim();
    const scheme = (`${{installConfig.mobileAppScheme || 'aitrader'}}`.trim() || 'aitrader').replace(/:.*$/, '');
    if (!mobileServerUrl) return '';
    return `${{scheme}}://connect?server=${{encodeURIComponent(mobileServerUrl)}}`;
  }};

  const buildQrHref = (value) => {{
    const text = `${{value || ''}}`.trim();
    if (!text) return '';
    return `https://quickchart.io/qr?size=220&margin=1&text=${{encodeURIComponent(text)}}`;
  }};

  const describeServerReachability = (rawUrl) => {{
    const value = `${{rawUrl || ''}}`.trim();
    if (!value) return '서버 주소를 설정하면 이 연결이 같은 Wi-Fi용인지, 외부 HTTPS용인지 함께 안내합니다.';
    try {{
      const parsed = new URL(value);
      const host = parsed.hostname || '';
      const isHttps = parsed.protocol === 'https:';
      const isLocal =
        host === 'localhost' ||
        host === '127.0.0.1' ||
        /^10\\./.test(host) ||
        /^192\\.168\\./.test(host) ||
        /^172\\.(1[6-9]|2\\d|3[0-1])\\./.test(host);
      if (isHttps && !isLocal) return '외부 HTTPS 주소로 보입니다. TestFlight 설치 후 외부 네트워크에서도 접속 가능성이 높습니다.';
      if (!isHttps && isLocal) return '같은 Wi-Fi 또는 같은 사설망에서만 접속 가능한 LAN 주소입니다. iPhone과 Mac이 같은 네트워크에 있어야 합니다.';
      if (!isHttps) return 'HTTP 주소입니다. 내부망 테스트엔 적합하지만 외부 접속용으론 HTTPS 전환을 권장합니다.';
      return '연결 주소가 설정되었습니다. 실제 외부 접속 가능 여부는 방화벽, 라우터, 인증서 상태에 따라 달라질 수 있습니다.';
    }} catch (_err) {{
      return '서버 주소 형식을 다시 확인하세요. http:// 또는 https:// 형태를 권장합니다.';
    }}
  }};

  const getNativeInstallTarget = () => {{
    if (installConfig.iosTestflightUrl) {{
      return {{ label: 'TestFlight 설치', href: installConfig.iosTestflightUrl, kind: 'testflight' }};
    }}
    if (installConfig.iosManifestUrl) {{
      return {{ label: 'iPhone 앱 설치', href: buildManifestInstallHref(installConfig.iosManifestUrl), kind: 'manifest' }};
    }}
    if (installConfig.iosAppStoreUrl) {{
      return {{ label: 'App Store 열기', href: installConfig.iosAppStoreUrl, kind: 'appstore' }};
    }}
    return null;
  }};

  const openNativeInstall = () => {{
    const nativeTarget = getNativeInstallTarget();
    if (!nativeTarget) {{
      window.alert('아직 iPhone native 설치 링크가 설정되지 않았습니다. 현재는 \"홈 화면 설치\"를 사용하세요.');
      return;
    }}
    if (installHint) {{
      installHint.textContent = `${{nativeTarget.label}} 링크를 여는 중입니다. iPhone에서는 설치 또는 TestFlight 화면으로 이동합니다.`;
    }}
    window.location.href = nativeTarget.href;
  }};

  const showInstallGuide = () => {{
    const nativeTarget = getNativeInstallTarget();
    const mobileServerUrl = `${{installConfig.mobileServerUrl || ''}}`.trim();
    const isHttpsServer = mobileServerUrl.startsWith('https://');
    if (isIOS && nativeTarget && nativeTarget.kind === 'testflight') {{
      window.alert('이 iPhone에서는 \"지금 설치\"를 누른 뒤 TestFlight에서 앱을 설치하세요. 설치 후 홈 화면에서 AITRADER를 실행하고 서버 주소를 연결하면 됩니다.');
      return;
    }}
    if (isIOS && nativeTarget && nativeTarget.kind === 'manifest') {{
      window.alert('이 iPhone에서는 \"지금 설치\"를 눌러 배포 프로파일 설치 흐름으로 이동할 수 있습니다. 설치가 제한되면 Safari 설정과 배포 프로파일 신뢰 상태를 확인하세요.');
      return;
    }}
    if (isIOS && nativeTarget && nativeTarget.kind === 'appstore') {{
      window.alert('이 iPhone에서는 \"지금 설치\"를 누르면 App Store로 이동합니다. 설치 후 앱을 열어 서버 주소를 입력하면 됩니다.');
      return;
    }}
    if (isIOS) {{
      if (isHttpsServer) {{
        window.alert('Safari에서 HTTPS 주소를 열고 처음 보안 경고가 나오면 \"세부사항 보기\" 후 사이트 접근을 허용하세요. 그다음 공유 버튼 > \"홈 화면에 추가\"를 선택하면 앱처럼 사용할 수 있습니다.');
      }} else {{
        window.alert('Safari에서 공유 버튼을 누른 뒤 \"홈 화면에 추가\"를 선택하면 앱처럼 설치할 수 있습니다. 외부 접속이나 더 안정적인 설치를 원하면 HTTPS 주소 사용을 권장합니다.');
      }}
      return;
    }}
    window.alert('iPhone에서 이 페이지를 열면 native 설치 버튼(TestFlight/App Store/배포 링크) 또는 홈 화면 설치 버튼을 사용할 수 있습니다. 현재 HTTP 주소로 접속 중이면 자동으로 HTTPS로 이동합니다.');
  }};

  const updateInstallUi = () => {{
    const nativeTarget = getNativeInstallTarget();
    const mobileServerUrl = `${{installConfig.mobileServerUrl || ''}}`.trim();
    const mobileServerLabel = `${{installConfig.mobileServerLabel || 'AITRADER Server'}}`.trim() || 'AITRADER Server';
    const appOpenHref = buildAppOpenHref();
    if (nativeInstallBtn) {{
      if (nativeTarget) {{
        nativeInstallBtn.hidden = false;
        nativeInstallBtn.textContent = nativeTarget.label;
      }} else {{
        nativeInstallBtn.hidden = true;
      }}
    }}
    if (installTopbar) {{
      installTopbar.hidden = !(isIOS && !isStandalone);
    }}
    if (installHeroCard) {{
      installHeroCard.classList.toggle('visible', !!(isIOS && !isStandalone));
    }}
    if (installTopbarMsg) {{
      installTopbarMsg.textContent = nativeTarget ? 'iPhone에서 AITRADER 앱을 바로 설치할 수 있습니다.' : 'iPhone에서 AITRADER를 홈 화면 앱처럼 설치할 수 있습니다.';
    }}
    if (installTopbarSub) {{
      installTopbarSub.textContent = nativeTarget ? `${{nativeTarget.label}}와 홈 화면 설치를 모두 사용할 수 있습니다.` : '현재는 홈 화면 설치를 바로 사용할 수 있고, TestFlight 링크를 넣으면 native 설치도 표시됩니다.';
    }}
    if (installHeroTitle) {{
      installHeroTitle.textContent = nativeTarget ? 'AITRADER iPhone 앱 설치' : 'AITRADER 홈 화면 설치';
    }}
    if (installHeroText) {{
      installHeroText.textContent = nativeTarget
        ? '이 iPhone에서는 native 설치와 홈 화면 설치를 모두 사용할 수 있습니다. 가장 깔끔한 방법은 \"지금 설치\"입니다.'
        : '지금은 홈 화면 앱 설치를 사용할 수 있습니다. TestFlight 또는 App Store 링크를 연결하면 native 설치 버튼도 바로 열립니다.';
    }}
    if (installHeroPrimaryBtn) {{
      installHeroPrimaryBtn.textContent = nativeTarget ? (nativeTarget.label || '지금 설치') : '홈 화면 설치';
    }}
    if (installHeroBadge) {{
      installHeroBadge.textContent = nativeTarget ? `native: ${{nativeTarget.kind}}` : 'native 링크 없음';
    }}
    if (installServerValue) {{
      installServerValue.textContent = mobileServerUrl || '서버 주소를 아직 설정하지 않았습니다.';
    }}
    if (installServerText) {{
      installServerText.textContent = mobileServerUrl
        ? `${{mobileServerLabel}} 주소입니다. 앱 설치 후 이 값을 연결 주소로 입력하거나 복사해서 사용하세요. ${{describeServerReachability(mobileServerUrl)}}`
        : '설정 화면에서 MOBILE_SERVER_URL을 넣어두면 iPhone 설치 카드에서 바로 연결 주소를 확인할 수 있습니다.';
    }}
    if (installServerOpenAppBtn) {{
      installServerOpenAppBtn.hidden = !mobileServerUrl;
    }}
    if (installServerCopyBtn) {{
      installServerCopyBtn.hidden = !mobileServerUrl;
    }}
    if (installServerQrBox && installServerQrImg) {{
      const qrHref = buildQrHref(mobileServerUrl);
      installServerQrBox.classList.toggle('visible', !!qrHref);
      if (qrHref) {{
        installServerQrImg.src = qrHref;
      }} else {{
        installServerQrImg.removeAttribute('src');
      }}
    }}
    if (installServerQrNote) {{
      installServerQrNote.textContent = mobileServerUrl
        ? `QR 코드를 스캔하면 ${{mobileServerLabel}} 주소를 다른 기기에서 바로 열 수 있습니다. 설치된 iPhone 앱이 있다면 "설치된 앱 열기"도 사용할 수 있습니다.`
        : '서버 주소를 설정하면 QR 코드가 표시됩니다. 다른 기기에서 빠르게 서버 주소를 열거나 확인할 때 사용할 수 있습니다.';
    }}
    if (installHint) {{
      if (isStandalone) {{
        installHint.textContent = '이미 홈 화면 앱으로 설치되어 있습니다.';
      }} else if (isIOS && nativeTarget) {{
        installHint.textContent = '이 iPhone에서는 native 설치와 홈 화면 설치를 모두 사용할 수 있습니다.';
      }} else if (isIOS) {{
        installHint.textContent = '지금은 홈 화면 앱 설치를 사용할 수 있습니다. native 설치 버튼은 TestFlight 또는 배포 링크를 넣으면 활성화됩니다.';
      }} else if (nativeTarget) {{
        installHint.textContent = 'iPhone에서는 native 설치 버튼이 보이고, 다른 기기에서는 홈 화면 설치를 사용할 수 있습니다.';
      }}
    }}
  }};

  if (installHint && isStandalone) {{
    installHint.textContent = '이미 홈 화면 앱으로 설치되어 있습니다.';
  }}

  if ('serviceWorker' in navigator) {{
    window.addEventListener('load', () => {{
      navigator.serviceWorker.getRegistrations()
        .then((regs) => Promise.all(regs.map((reg) => reg.unregister())))
        .catch(() => null)
        .then(() => (window.caches ? caches.keys().then((keys) => Promise.all(keys.map((key) => caches.delete(key)))) : null))
        .catch(() => null)
        .then(() => navigator.serviceWorker.register('/sw.js'))
        .catch(() => null);
    }});
  }}

  window.addEventListener('beforeinstallprompt', (event) => {{
    event.preventDefault();
    deferredInstallPrompt = event;
    if (installHint) installHint.textContent = '이 기기에서 앱 설치 프롬프트를 바로 열 수 있습니다.';
  }});

  if (installBtn) {{
    installBtn.addEventListener('click', async () => {{
      if (isStandalone) {{
        window.alert('이미 홈 화면 앱으로 설치되어 있습니다.');
        return;
      }}
      if (deferredInstallPrompt) {{
        deferredInstallPrompt.prompt();
        try {{
          await deferredInstallPrompt.userChoice;
        }} catch (_err) {{}}
        deferredInstallPrompt = null;
        if (installHint) installHint.textContent = '설치 선택이 완료되면 홈 화면에서 앱처럼 실행할 수 있습니다.';
        return;
      }}
      const isSecure = window.isSecureContext || location.hostname === 'localhost' || location.hostname === '127.0.0.1';
      if (isIOS) {{
        window.alert('Safari에서 공유 버튼을 누른 뒤 \"홈 화면에 추가\"를 선택하면 앱처럼 설치할 수 있습니다.');
        return;
      }}
      if (!isSecure) {{
        window.alert('현재 주소는 설치 프롬프트가 제한될 수 있습니다. 휴대폰 브라우저 메뉴의 \"홈 화면에 추가\"를 사용하거나 HTTPS 주소에서 접속하세요.');
        return;
      }}
      window.alert('브라우저 메뉴에서 \"앱 설치\" 또는 \"홈 화면에 추가\"를 선택하세요.');
    }});
  }}
  if (nativeInstallBtn) {{
    nativeInstallBtn.addEventListener('click', openNativeInstall);
  }}
  if (installGuideBtn) {{
    installGuideBtn.addEventListener('click', showInstallGuide);
  }}
  if (installTopbarPrimaryBtn) {{
    installTopbarPrimaryBtn.addEventListener('click', () => {{
      const nativeTarget = getNativeInstallTarget();
      if (nativeTarget) {{
        openNativeInstall();
        return;
      }}
      if (installBtn) installBtn.click();
    }});
  }}
  if (installTopbarGuideBtn) {{
    installTopbarGuideBtn.addEventListener('click', showInstallGuide);
  }}
  if (installHeroPrimaryBtn) {{
    installHeroPrimaryBtn.addEventListener('click', () => {{
      const nativeTarget = getNativeInstallTarget();
      if (nativeTarget) {{
        openNativeInstall();
        return;
      }}
      if (installBtn) installBtn.click();
    }});
  }}
  if (installHeroPwaBtn) {{
    installHeroPwaBtn.addEventListener('click', () => {{
      if (installBtn) installBtn.click();
    }});
  }}
  if (installHeroGuideBtn) {{
    installHeroGuideBtn.addEventListener('click', showInstallGuide);
  }}
  if (installServerCopyBtn) {{
    installServerCopyBtn.addEventListener('click', async () => {{
      const mobileServerUrl = `${{installConfig.mobileServerUrl || ''}}`.trim();
      if (!mobileServerUrl) {{
        window.alert('아직 서버 연결 주소가 설정되지 않았습니다. 설정 화면에서 MOBILE_SERVER_URL을 먼저 입력하세요.');
        return;
      }}
      try {{
        if (navigator.clipboard && navigator.clipboard.writeText) {{
          await navigator.clipboard.writeText(mobileServerUrl);
        }}
        if (installServerText) installServerText.textContent = '서버 주소를 복사했습니다. iPhone 앱 설치 후 연결 주소로 붙여넣으세요.';
      }} catch (_err) {{
        window.alert(`서버 주소: ${{mobileServerUrl}}`);
      }}
    }});
  }}
  if (installServerOpenAppBtn) {{
    installServerOpenAppBtn.addEventListener('click', () => {{
      const mobileServerUrl = `${{installConfig.mobileServerUrl || ''}}`.trim();
      const appOpenHref = buildAppOpenHref();
      if (!mobileServerUrl || !appOpenHref) {{
        window.alert('먼저 MOBILE_SERVER_URL을 설정해야 앱으로 서버 주소를 넘길 수 있습니다.');
        return;
      }}
      window.location.href = appOpenHref;
    }});
  }}
  if (installServerGuideBtn) {{
    installServerGuideBtn.addEventListener('click', () => {{
      const mobileServerUrl = `${{installConfig.mobileServerUrl || ''}}`.trim();
      if (mobileServerUrl) {{
        window.alert(`설치 후 앱을 열고 서버 주소에 아래 값을 입력하세요.\\n\\n${{mobileServerUrl}}\\n\\n같은 Wi-Fi 또는 외부 접속 가능한 HTTPS 주소가 필요합니다.`);
        return;
      }}
      window.alert('설치 후 앱에서 연결 주소를 입력해야 합니다. 먼저 설정 화면에서 MOBILE_SERVER_URL을 저장해두면 이 화면에서 바로 복사할 수 있습니다.');
    }});
  }}
  updateInstallUi();
  scheduleMasonry();
}})();
</script>
</div></body></html>"""
            self._html(page)
            return

        self._html("Not Found", status=404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        settings = self._settings_for_access()
        if path == "/access-unlock":
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length).decode("utf-8") if length > 0 else ""
            form = parse_qs(raw, keep_blank_values=True)
            submitted_key = str((form.get("access_key", [""])[0] or "")).strip()
            expected_key = str(settings.web_access_key or "").strip()
            if not settings.web_access_enabled:
                self._redirect("/")
                return
            if not expected_key:
                self._render_access_gate(settings, error="접근 보호는 켜져 있지만 WEB_ACCESS_KEY가 비어 있습니다. PC에서 먼저 설정해 주세요.")
                return
            if submitted_key != expected_key:
                self._render_access_gate(settings, error="접근 키가 맞지 않습니다. 다시 확인해 주세요.")
                return
            devices = _cleanup_trusted_web_devices(
                _load_trusted_web_devices(),
                ttl_days=settings.web_trusted_device_days,
                max_devices=settings.web_max_trusted_devices,
            )
            token = secrets.token_urlsafe(24)
            devices[token] = {
                "label": "trusted-mobile",
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "created_ts": f"{time.time():.3f}",
                "last_seen_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "last_seen_ts": f"{time.time():.3f}",
                "last_ip": self._client_ip(),
                "user_agent": str(self.headers.get("User-Agent", "") or "")[:240],
            }
            devices = _cleanup_trusted_web_devices(
                devices,
                ttl_days=settings.web_trusted_device_days,
                max_devices=settings.web_max_trusted_devices,
            )
            _save_trusted_web_devices(devices)
            self.send_response(HTTPStatus.SEE_OTHER)
            self._set_trusted_device_cookie(token, max_age_days=settings.web_trusted_device_days)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if not self._is_authorized_request(settings):
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/access")
            self.end_headers()
            return
        if path == "/config-save":
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length).decode("utf-8") if length > 0 else ""
            form = parse_qs(raw, keep_blank_values=True)
            trade_mode_raw = (form.get("TRADE_MODE", [""])[0] or "").strip().upper()
            trade_mode = trade_mode_raw if trade_mode_raw in {"DRY", "LIVE"} else "DRY"
            updates = {
                "KIWOOM_BASE_URL": (form.get("KIWOOM_BASE_URL", [""])[0] or "").strip(),
                "ACCOUNT_NO": (form.get("ACCOUNT_NO", [""])[0] or "").strip(),
                "PRICE_PATH": (form.get("PRICE_PATH", [""])[0] or "").strip(),
                "ORDER_PATH": (form.get("ORDER_PATH", [""])[0] or "").strip(),
                "SLACK_WEBHOOK_URL": (form.get("SLACK_WEBHOOK_URL", [""])[0] or "").strip(),
                "SLACK_EVENT_KEYWORDS": (form.get("SLACK_EVENT_KEYWORDS", [""])[0] or "").strip(),
                "TRADE_COOLDOWN_SEC": (form.get("TRADE_COOLDOWN_SEC", [""])[0] or "").strip(),
                "STALE_DATA_MAX_AGE_SEC": (form.get("STALE_DATA_MAX_AGE_SEC", [""])[0] or "").strip(),
                "TARGET_RISK_PER_TRADE_PCT": (form.get("TARGET_RISK_PER_TRADE_PCT", [""])[0] or "").strip(),
                "DAILY_LOSS_LIMIT_PCT": (form.get("DAILY_LOSS_LIMIT_PCT", [""])[0] or "").strip(),
                "MAX_SYMBOL_LOSS_PCT": (form.get("MAX_SYMBOL_LOSS_PCT", [""])[0] or "").strip(),
                "MAX_PORTFOLIO_HEAT_PCT": (form.get("MAX_PORTFOLIO_HEAT_PCT", [""])[0] or "").strip(),
                "SIGNAL_CONFIRM_CYCLES": (form.get("SIGNAL_CONFIRM_CYCLES", [""])[0] or "").strip(),
                "BAR_INTERVAL_MINUTES": (form.get("BAR_INTERVAL_MINUTES", [""])[0] or "").strip(),
                "DECISION_ON_BAR_CLOSE_ONLY": "1" if form.get("DECISION_ON_BAR_CLOSE_ONLY", [""])[0] else "0",
                "MARKET_STATUS_FILTER_ENABLED": "1" if form.get("MARKET_STATUS_FILTER_ENABLED", [""])[0] else "0",
                "ENABLE_BEARISH_EXCEPTION": "1" if form.get("ENABLE_BEARISH_EXCEPTION", [""])[0] else "0",
                "ATR_EXIT_LOOKBACK_DAYS": (form.get("ATR_EXIT_LOOKBACK_DAYS", [""])[0] or "").strip(),
                "ATR_STOP_MULT": (form.get("ATR_STOP_MULT", [""])[0] or "").strip(),
                "ATR_TAKE_MULT": (form.get("ATR_TAKE_MULT", [""])[0] or "").strip(),
                "ATR_TRAILING_MULT": (form.get("ATR_TRAILING_MULT", [""])[0] or "").strip(),
                "MAX_ACTIVE_POSITIONS": (form.get("MAX_ACTIVE_POSITIONS", [""])[0] or "").strip(),
                "CANDIDATE_REFRESH_TOP_N": (form.get("CANDIDATE_REFRESH_TOP_N", [""])[0] or "").strip(),
                "CANDIDATE_REFRESH_MINUTES": (form.get("CANDIDATE_REFRESH_MINUTES", [""])[0] or "").strip(),
                "INTRADAY_RESELECT_ENABLED": "1" if form.get("INTRADAY_RESELECT_ENABLED", [""])[0] else "0",
                "INTRADAY_RESELECT_MINUTES": (form.get("INTRADAY_RESELECT_MINUTES", [""])[0] or "").strip(),
                "TREND_SELECT_COUNT": (form.get("TREND_SELECT_COUNT", [""])[0] or "").strip(),
                "TREND_MIN_AVG_TURNOVER20_KRW": (form.get("TREND_MIN_AVG_TURNOVER20_KRW", [""])[0] or "").strip(),
                "TREND_MIN_TURNOVER_RATIO_5_TO_20": (form.get("TREND_MIN_TURNOVER_RATIO_5_TO_20", [""])[0] or "").strip(),
                "TREND_MIN_VALUE_SPIKE_RATIO": (form.get("TREND_MIN_VALUE_SPIKE_RATIO", [""])[0] or "").strip(),
                "TREND_BREAKOUT_BUFFER_PCT": (form.get("TREND_BREAKOUT_BUFFER_PCT", [""])[0] or "").strip(),
                "TREND_MIN_ATR14_PCT": (form.get("TREND_MIN_ATR14_PCT", [""])[0] or "").strip(),
                "TREND_MAX_ATR14_PCT": (form.get("TREND_MAX_ATR14_PCT", [""])[0] or "").strip(),
                "TREND_OVERHEAT_DAY_PCT": (form.get("TREND_OVERHEAT_DAY_PCT", [""])[0] or "").strip(),
                "TREND_OVERHEAT_2DAY_PCT": (form.get("TREND_OVERHEAT_2DAY_PCT", [""])[0] or "").strip(),
                "TREND_DAILY_RSI_MIN": (form.get("TREND_DAILY_RSI_MIN", [""])[0] or "").strip(),
                "TREND_DAILY_RSI_MAX": (form.get("TREND_DAILY_RSI_MAX", [""])[0] or "").strip(),
                "TREND_GAP_SKIP_UP_PCT": (form.get("TREND_GAP_SKIP_UP_PCT", [""])[0] or "").strip(),
                "TREND_GAP_SKIP_DOWN_PCT": (form.get("TREND_GAP_SKIP_DOWN_PCT", [""])[0] or "").strip(),
                "TREND_MAX_CHASE_FROM_OPEN_PCT": (form.get("TREND_MAX_CHASE_FROM_OPEN_PCT", [""])[0] or "").strip(),
                "TREND_MAX_SECTOR_NAMES": (form.get("TREND_MAX_SECTOR_NAMES", [""])[0] or "").strip(),
                "SYMBOL_SECTOR_MAP": (form.get("SYMBOL_SECTOR_MAP", [""])[0] or "").strip(),
                "SECTOR_AUTO_MAP_ENABLED": "1" if form.get("SECTOR_AUTO_MAP_ENABLED", [""])[0] else "0",
                "TRADE_MODE": trade_mode,
                "LIVE_ARMED": "1" if (trade_mode == "LIVE" and form.get("LIVE_ARMED", [""])[0]) else "0",
                "SLACK_ENABLED": "1" if form.get("SLACK_ENABLED", [""])[0] else "0",
                "HOURLY_MARKET_REPORT_ENABLED": "1" if form.get("HOURLY_MARKET_REPORT_ENABLED", [""])[0] else "0",
                "COMPARE_WARN_WIN_RATE_GAP_PCT": (form.get("COMPARE_WARN_WIN_RATE_GAP_PCT", [""])[0] or "").strip(),
                "COMPARE_WARN_PNL_GAP_KRW": (form.get("COMPARE_WARN_PNL_GAP_KRW", [""])[0] or "").strip(),
                "COMPARE_WARN_EXPECTANCY_GAP_KRW": (form.get("COMPARE_WARN_EXPECTANCY_GAP_KRW", [""])[0] or "").strip(),
                "COMPARE_WARN_HOLD_GAP_DAYS": (form.get("COMPARE_WARN_HOLD_GAP_DAYS", [""])[0] or "").strip(),
                "IOS_TESTFLIGHT_URL": (form.get("IOS_TESTFLIGHT_URL", [""])[0] or "").strip(),
                "IOS_APP_STORE_URL": (form.get("IOS_APP_STORE_URL", [""])[0] or "").strip(),
                "IOS_MANIFEST_URL": (form.get("IOS_MANIFEST_URL", [""])[0] or "").strip(),
                "MOBILE_SERVER_URL": (form.get("MOBILE_SERVER_URL", [""])[0] or "").strip(),
                "MOBILE_SERVER_LABEL": (form.get("MOBILE_SERVER_LABEL", [""])[0] or "").strip(),
                "MOBILE_APP_SCHEME": (form.get("MOBILE_APP_SCHEME", [""])[0] or "").strip(),
                "WEB_ACCESS_ENABLED": "1" if form.get("WEB_ACCESS_ENABLED", [""])[0] else "0",
                "WEB_ACCESS_KEY": (form.get("WEB_ACCESS_KEY", [""])[0] or "").strip(),
                "WEB_TRUSTED_DEVICE_DAYS": (form.get("WEB_TRUSTED_DEVICE_DAYS", [""])[0] or "").strip(),
                "WEB_MAX_TRUSTED_DEVICES": (form.get("WEB_MAX_TRUSTED_DEVICES", [""])[0] or "").strip(),
            }
            save_runtime_overrides(updates)
            started, _ = _restart_bot_with_retry()
            self._redirect("/?config=applied" if started else "/?config=failed")
            return
        if path == "/trusted-device-remove":
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length).decode("utf-8") if length > 0 else ""
            form = parse_qs(raw, keep_blank_values=True)
            token = str((form.get("token", [""])[0] or "")).strip()
            if token:
                devices = _cleanup_trusted_web_devices(
                    _load_trusted_web_devices(),
                    ttl_days=settings.web_trusted_device_days,
                    max_devices=max(settings.web_max_trusted_devices + 4, settings.web_max_trusted_devices),
                )
                devices.pop(token, None)
                _save_trusted_web_devices(devices)
                current_token = self._read_trusted_device_token()
                if current_token == token:
                    self.send_response(HTTPStatus.SEE_OTHER)
                    self._clear_trusted_device_cookie()
                    self.send_header("Location", "/access")
                    self.end_headers()
                    return
            self._redirect("/?trusted_device=removed")
            return
        if path == "/trusted-device-rename":
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length).decode("utf-8") if length > 0 else ""
            form = parse_qs(raw, keep_blank_values=True)
            token = str((form.get("token", [""])[0] or "")).strip()
            label = str((form.get("label", [""])[0] or "")).strip()
            if token:
                devices = _cleanup_trusted_web_devices(
                    _load_trusted_web_devices(),
                    ttl_days=settings.web_trusted_device_days,
                    max_devices=max(settings.web_max_trusted_devices + 4, settings.web_max_trusted_devices),
                )
                meta = devices.get(token)
                if meta is not None:
                    meta["label"] = label or "모바일 기기"
                    devices[token] = meta
                    _save_trusted_web_devices(devices)
            self._redirect("/?trusted_device=renamed")
            return
        if path == "/diagnostics-run":
            diagnostics.run()
            self._redirect("/?diag=done")
            return
        if path == "/simulation-run":
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length).decode("utf-8") if length > 0 else ""
                form = parse_qs(raw, keep_blank_values=True)
                profile = str((form.get("profile", ["daily_selection"])[0] or "daily_selection")).strip().lower()
                if profile not in _simulation_report_catalog():
                    profile = "daily_selection"
                window_days = max(5, int(_to_float((form.get("window_days", ["20"])[0] or "20")) or 20))
                seed_n = max(50, int(_to_float((form.get("seed_n", ["2000"])[0] or "2000")) or 2000))
                top_n = max(1, int(_to_float((form.get("top_n", ["800"])[0] or "800")) or 800))
                data_fetch_limit = max(60, int(_to_float((form.get("data_fetch_limit", ["260"])[0] or "260")) or 260))
                max_hold_days = max(1, int(_to_float((form.get("max_hold_days", ["2"])[0] or "2")) or 2))
                relaxed_selected_entry = "relaxed_selected_entry" in form
                selected_continuation_probe = "selected_continuation_probe" in form
                rank_weights_raw = str((form.get("rank_weights", ["0.5,0.3,0.2"])[0] or "0.5,0.3,0.2")).strip()
                rank_weights: list[float] = []
                for chunk in rank_weights_raw.split(","):
                    try:
                        value = float((chunk or "").strip())
                    except Exception:
                        value = 0.0
                    if value > 0:
                        rank_weights.append(value)
                if not rank_weights:
                    rank_weights = [0.5, 0.3, 0.2]
                compare_warn_win_rate_gap_pct = max(
                    0.0,
                    _to_float((form.get("compare_warn_win_rate_gap_pct", [""])[0] or "") or settings.compare_warn_win_rate_gap_pct),
                )
                compare_warn_pnl_gap_krw = max(
                    0.0,
                    _to_float((form.get("compare_warn_pnl_gap_krw", [""])[0] or "") or settings.compare_warn_pnl_gap_krw),
                )
                compare_warn_expectancy_gap_krw = max(
                    0.0,
                    _to_float((form.get("compare_warn_expectancy_gap_krw", [""])[0] or "") or settings.compare_warn_expectancy_gap_krw),
                )
                compare_warn_hold_gap_days = max(
                    0.0,
                    _to_float((form.get("compare_warn_hold_gap_days", [""])[0] or "") or settings.compare_warn_hold_gap_days),
                )
                existing_run_config = _load_simulation_run_config()
                profiles = dict(existing_run_config.get("profiles") or {}) if isinstance(existing_run_config.get("profiles"), dict) else {}
                histories = dict(existing_run_config.get("profile_histories") or {}) if isinstance(existing_run_config.get("profile_histories"), dict) else {}
                profile_run_config = {
                    "profile": profile,
                    "window_days": window_days,
                    "seed_n": seed_n,
                    "top_n": top_n,
                    "data_fetch_limit": data_fetch_limit,
                    "max_hold_days": max_hold_days,
                    "target_day": str((form.get("target_day", [""])[0] or "")).strip(),
                    "rank_weights": ",".join(f"{x:g}" for x in rank_weights),
                    "relaxed_selected_entry": relaxed_selected_entry,
                    "selected_continuation_probe": selected_continuation_probe,
                    "compare_warn_win_rate_gap_pct": compare_warn_win_rate_gap_pct,
                    "compare_warn_pnl_gap_krw": compare_warn_pnl_gap_krw,
                    "compare_warn_expectancy_gap_krw": compare_warn_expectancy_gap_krw,
                    "compare_warn_hold_gap_days": compare_warn_hold_gap_days,
                    "requested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                profile_history = list(histories.get(profile) or []) if isinstance(histories.get(profile), list) else []
                profile_history.append(dict(profile_run_config))
                profile_history = profile_history[-12:]
                profile_run_config["history"] = profile_history
                profiles[profile] = dict(profile_run_config)
                histories[profile] = list(profile_history)
                run_config = dict(existing_run_config)
                run_config.update(profile_run_config)
                run_config["profiles"] = profiles
                run_config["profile_histories"] = histories
                _save_simulation_run_config(run_config)
                if profile == "rolling_rank":
                    generate_rolling_rank_study(window_days=window_days, seed_n=seed_n, data_fetch_limit=data_fetch_limit)
                elif profile == "short_horizon":
                    generate_short_horizon_rank_study(window_days=window_days, seed_n=seed_n, data_fetch_limit=data_fetch_limit)
                elif profile == "daily_selection":
                    generate_daily_selection_portfolio_report(
                        window_days=window_days,
                        seed_n=seed_n,
                        max_hold_days=max_hold_days,
                        relaxed_selected_entry=relaxed_selected_entry,
                        selected_continuation_probe=selected_continuation_probe,
                        data_fetch_limit=data_fetch_limit,
                    )
                elif profile == "rank_weighted":
                    generate_rank_weighted_portfolio_study(
                        window_days=window_days,
                        seed_n=seed_n,
                        rank_weights=rank_weights,
                        data_fetch_limit=data_fetch_limit,
                    )
                elif profile == "intraday_replay":
                    generate_intraday_selected_replay_report(
                        window_days=window_days,
                        target_day=str((form.get("target_day", [""])[0] or "")).strip(),
                    )
                else:
                    generate_short_term_trade_report(top_n=top_n, seed_n=seed_n, data_fetch_limit=data_fetch_limit)
                self._redirect(f"/?simulation=done&sim_profile={profile}")
            except Exception as exc:
                self._redirect(f"/?simulation=failed&sim_profile={profile if 'profile' in locals() else 'short_term'}&reason={str(exc)[:120]}")
            return
        if path == "/mode-set":
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length).decode("utf-8") if length > 0 else ""
            form = parse_qs(raw, keep_blank_values=True)
            mode = (form.get("mode", ["DRY"])[0] or "DRY").strip().upper()
            if mode not in {"DRY", "LIVE"}:
                mode = "DRY"
            arm = mode == "LIVE"
            save_runtime_overrides(
                {
                    "TRADE_MODE": mode,
                    "LIVE_ARMED": "1" if arm else "0",
                    "DRY_RUN": "false" if mode == "LIVE" else "true",
                }
            )
            started, _ = _restart_bot_with_retry()
            self._redirect("/?mode=applied" if started else "/?mode=failed")
            return
        if path == "/start":
            controller.start()
            self._redirect("/")
            return
        if path == "/stop":
            controller.stop()
            self._redirect("/")
            return
        if path == "/market-refresh":
            market_vibe.get(force=True)
            self._redirect("/")
            return
        if path == "/global-refresh":
            global_market.get(force=True)
            self._redirect("/")
            return
        self._html("Not Found", status=404)


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def _create_ssl_context(current_settings) -> ssl.SSLContext | None:
    certfile = Path(str(getattr(current_settings, "web_ssl_certfile", "") or "")).expanduser()
    keyfile = Path(str(getattr(current_settings, "web_ssl_keyfile", "") or "")).expanduser()
    if not certfile.exists() or not keyfile.exists():
        logging.warning(
            "HTTPS requested but certificate files are missing. cert=%s key=%s",
            certfile,
            keyfile,
        )
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(certfile), keyfile=str(keyfile))
    return context


def run_web_server() -> None:
    current_settings = load_settings()
    host = os.getenv("WEB_HOST", "127.0.0.1")
    port = int(os.getenv("WEB_PORT", "8080"))
    https_enabled = bool(getattr(current_settings, "web_https_enabled", False))
    listen_port = int(getattr(current_settings, "web_https_port", port) or port) if https_enabled else port
    if _port_in_use(host, listen_port):
        logging.warning(
            "Web control already appears to be running at %s://%s:%s . "
            "If you intended to restart it, stop the existing process first or change the configured port.",
            "https" if https_enabled else "http",
            host,
            listen_port,
        )
        return
    server = ReusableThreadingHTTPServer((host, listen_port), Handler)
    if https_enabled:
        ssl_context = _create_ssl_context(current_settings)
        if ssl_context is not None:
            server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
            logging.info("Web control running at https://%s:%s", host, listen_port)
        else:
            logging.warning("Falling back to HTTP because HTTPS certificate setup is incomplete.")
            logging.info("Web control running at http://%s:%s", host, listen_port)
    else:
        logging.info("Web control running at http://%s:%s", host, listen_port)
    companion_http_server: ReusableThreadingHTTPServer | None = None
    if https_enabled and port != listen_port:
        if _port_in_use(host, port):
            logging.warning(
                "HTTP companion listener could not start because http://%s:%s is already in use.",
                host,
                port,
            )
        else:
            companion_http_server = ReusableThreadingHTTPServer((host, port), Handler)
            threading.Thread(target=companion_http_server.serve_forever, daemon=True).start()
            logging.info("HTTP companion server running at http://%s:%s while HTTPS stays at https://%s:%s", host, port, host, listen_port)
    started, message = controller.start()
    if started:
        logging.info("Auto-start bot on server boot: %s", message)
    else:
        logging.warning("Auto-start bot skipped: %s", message)
    server.serve_forever()


if __name__ == "__main__":
    run_web_server()
