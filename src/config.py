from __future__ import annotations

import os
import json
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()

RUNTIME_CONFIG_PATH = Path(os.getenv("RUNTIME_CONFIG_PATH", "data/runtime_config.json"))
_RUNTIME_CACHE_MTIME_NS: int | None = None
_RUNTIME_CACHE_DATA: dict[str, str] = {}


def _to_bool(value: str, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    base_url: str
    app_key: str
    secret_key: str
    symbol: str
    poll_seconds: int
    dry_run: bool
    max_daily_orders: int
    position_size: int
    buy_drop_pct: float
    sell_rise_pct: float
    price_path: str
    price_field: str
    price_api_id: str
    price_history_api_id: str
    market_regime_api_id: str
    investor_flow_api_id: str
    after_close_investor_flow_api_id: str
    vi_trigger_api_id: str
    order_path: str
    order_buy_api_id: str
    order_sell_api_id: str
    dmst_stex_tp: str
    trde_tp: str
    order_unit_price: str
    account_no: str
    account_api_path: str
    account_lookup_api_id: str
    account_cash_api_id: str
    account_holdings_api_id: str
    account_query_type: str
    account_refresh_sec: int
    initial_cash: float
    ledger_path: str
    selection_history_path: str
    simulate_on_dry_run: bool
    auto_select_enabled: bool
    max_active_positions: int
    candidate_symbols: str
    defensive_symbols: str
    symbol_sector_map: str
    sector_auto_map_enabled: bool
    sector_cache_path: str
    candidate_refresh_enabled: bool
    candidate_refresh_once_daily: bool
    candidate_refresh_minutes: int
    candidate_refresh_top_n: int
    candidate_refresh_min_score: float
    intraday_reselect_enabled: bool
    intraday_reselect_minutes: int
    universe_symbols: str
    auto_universe_enabled: bool
    auto_universe_source_url: str
    auto_universe_refresh_minutes: int
    universe_batch_size: int
    rebalance_minutes: int
    momentum_lookback_days: int
    min_momentum_pct: float
    signal_confirm_cycles: int
    adaptive_edge_enabled: bool
    adaptive_edge_lookback_days: int
    adaptive_edge_strength: float
    regime_switch_confirm_cycles: int
    regime_switch_min_confidence: float
    sizing_vol_lookback_days: int
    atr_exit_lookback_days: int
    atr_stop_mult: float
    atr_take_mult: float
    atr_trailing_mult: float
    target_risk_per_trade_pct: float
    daily_loss_limit_pct: float
    trade_cooldown_sec: int
    stale_data_max_age_sec: int
    stop_loss_pct: float
    take_profit_pct: float
    trailing_stop_pct: float
    min_entry_score: float
    min_entry_momentum_pct: float
    take_profit_partial_ratio: float
    min_entry_score_map: str
    min_entry_momentum_map: str
    take_profit_partial_ratio_map: str
    auto_param_tuning_enabled: bool
    auto_param_tuning_strength: float
    trade_mode: str
    live_armed: bool
    max_symbol_loss_pct: float
    max_portfolio_heat_pct: float
    slack_enabled: bool
    slack_webhook_url: str
    slack_event_keywords: str
    slack_bot_token: str
    slack_channel_id: str
    slack_attach_web_capture: bool
    slack_capture_url: str
    slack_capture_width: int
    slack_capture_height: int
    morning_brief_enabled: bool
    morning_brief_hour: int
    morning_brief_minute: int
    market_brief_times: str
    market_brief_history_path: str
    selection_change_slack_enabled: bool
    hourly_market_report_enabled: bool
    morning_news_enabled: bool
    morning_news_queries: str
    morning_news_limit: int
    us_mock_enabled: bool
    us_mock_symbols: str
    us_mock_benchmark_symbol: str
    us_mock_initial_cash: float
    us_mock_lookback_days: int
    us_mock_top_n: int
    us_mock_report_hour: int
    us_mock_report_minute: int
    us_mock_buy_drop_pct: float
    us_mock_sell_rise_pct: float
    us_mock_signal_confirm_cycles: int
    bar_interval_minutes: int
    decision_on_bar_close_only: bool
    trend_select_count: int
    trend_min_avg_turnover20_krw: float
    trend_min_turnover_ratio_5_to_20: float
    trend_min_value_spike_ratio: float
    trend_breakout_near_high_pct: float
    trend_min_atr14_pct: float
    trend_max_atr14_pct: float
    trend_breakout_buffer_pct: float
    trend_overheat_day_pct: float
    trend_overheat_2day_pct: float
    trend_gap_skip_up_pct: float
    trend_gap_skip_down_pct: float
    trend_daily_rsi_min: float
    trend_daily_rsi_max: float
    trend_max_chase_from_open_pct: float
    enable_bearish_exception: bool
    bearish_exception_trigger_pct: float
    bearish_exception_max_market_drop_pct: float
    bearish_exception_max_vol_pct: float
    trend_capital_per_name_pct: float
    trend_risk_per_trade_pct: float
    trend_max_sector_names: int
    strategy_mode: str
    market_timezone: str
    enable_krx_session_gates: bool
    premarket_brief_start: str
    premarket_brief_end: str
    opening_focus_start: str
    opening_focus_end: str
    regular_session_start: str
    regular_session_end: str
    after_market_start: str
    after_market_end: str
    allow_after_market_sell: bool
    market_shock_drop_pct: float
    vkospi_spike_proxy_pct: float
    manual_market_alert: str
    market_status_filter_enabled: bool
    market_policy_caution_risk_score: float
    market_policy_halt_risk_score: float
    market_policy_caution_confirm_extra: int
    market_policy_halt_confirm_extra: int
    market_policy_caution_entry_score_boost: float
    market_policy_halt_entry_score_boost: float
    market_policy_caution_entry_momentum_boost_pct: float
    market_policy_halt_entry_momentum_boost_pct: float
    market_policy_scalping_min_volume_boost: float
    manager_reason_ema_alpha_min: float
    manager_reason_ema_alpha_max: float
    manager_reason_ema_scale_trend: float
    manager_reason_ema_scale_scalping: float
    manager_reason_ema_scale_defensive: float
    compare_warn_win_rate_gap_pct: float
    compare_warn_pnl_gap_krw: float
    compare_warn_expectancy_gap_krw: float
    compare_warn_hold_gap_days: float
    ios_testflight_url: str
    ios_app_store_url: str
    ios_manifest_url: str
    mobile_server_url: str
    mobile_server_label: str
    mobile_app_scheme: str
    web_https_enabled: bool
    web_https_port: int
    web_ssl_certfile: str
    web_ssl_keyfile: str
    web_access_enabled: bool
    web_access_key: str
    web_trusted_device_days: int
    web_max_trusted_devices: int
    # Scalping parameters
    scalping_rsi_entry_min: float
    scalping_rsi_entry_max: float
    scalping_rsi_exit_min: float
    scalping_rsi_exit_max: float
    scalping_volume_spike_ratio: float
    scalping_profit_target_pct: float
    scalping_stop_loss_pct: float
    scalping_max_hold_bars: int
    scalping_min_trend_strength: float
    scalping_min_volume_ratio: float


def load_settings() -> Settings:
    overrides = _load_runtime_overrides()

    def _get(name: str, default: str = "") -> str:
        if name in overrides:
            return str(overrides.get(name, default))
        return os.getenv(name, default)

    raw_trade_mode = _get("TRADE_MODE", "").strip().upper()
    if raw_trade_mode in {"DRY", "LIVE"}:
        trade_mode = raw_trade_mode
    else:
        trade_mode = "DRY" if _to_bool(_get("DRY_RUN", "true"), True) else "LIVE"

    return Settings(
        base_url=_get("KIWOOM_BASE_URL", "https://api.kiwoom.com").rstrip("/"),
        app_key=_get("KIWOOM_APP_KEY", "").strip(),
        secret_key=_get("KIWOOM_SECRET_KEY", "").strip(),
        symbol=_get("SYMBOL", "005930").strip(),
        poll_seconds=int(_get("POLL_SECONDS", "20")),
        dry_run=(trade_mode != "LIVE"),
        max_daily_orders=int(_get("MAX_DAILY_ORDERS", "3")),
        position_size=int(_get("POSITION_SIZE", "1")),
        buy_drop_pct=float(_get("BUY_DROP_PCT", "-0.8")),
        sell_rise_pct=float(_get("SELL_RISE_PCT", "1.2")),
        price_path=_get("PRICE_PATH", "").strip(),
        price_field=_get("PRICE_FIELD", "cur_prc").strip(),
        price_api_id=_get("PRICE_API_ID", "ka10007").strip(),
        price_history_api_id=_get("PRICE_HISTORY_API_ID", "ka10005").strip(),
        market_regime_api_id=_get("MARKET_REGIME_API_ID", "ka20003").strip(),
        investor_flow_api_id=_get("INVESTOR_FLOW_API_ID", "ka10059").strip(),
        after_close_investor_flow_api_id=_get("AFTER_CLOSE_INVESTOR_FLOW_API_ID", "ka10063").strip(),
        vi_trigger_api_id=_get("VI_TRIGGER_API_ID", "ka10054").strip(),
        order_path=_get("ORDER_PATH", "").strip(),
        order_buy_api_id=_get("ORDER_BUY_API_ID", "kt10000").strip(),
        order_sell_api_id=_get("ORDER_SELL_API_ID", "kt10001").strip(),
        dmst_stex_tp=_get("DMST_STEX_TP", "KRX").strip(),
        trde_tp=_get("TRDE_TP", "3").strip(),
        order_unit_price=_get("ORDER_UNIT_PRICE", "").strip(),
        account_no=_get("ACCOUNT_NO", "").strip(),
        account_api_path=_get("ACCOUNT_API_PATH", "/api/dostk/acnt").strip(),
        account_lookup_api_id=_get("ACCOUNT_LOOKUP_API_ID", "ka00001").strip(),
        account_cash_api_id=_get("ACCOUNT_CASH_API_ID", "kt00001").strip(),
        account_holdings_api_id=_get("ACCOUNT_HOLDINGS_API_ID", "kt00017").strip(),
        account_query_type=_get("ACCOUNT_QUERY_TYPE", "1").strip(),
        account_refresh_sec=int(_get("ACCOUNT_REFRESH_SEC", "300")),
        initial_cash=float(_get("INITIAL_CASH", "10000000")),
        ledger_path=_get("LEDGER_PATH", "data/ledger.json").strip(),
        selection_history_path=_get("SELECTION_HISTORY_PATH", "data/selection_history.json").strip(),
        simulate_on_dry_run=_to_bool(_get("SIMULATE_ON_DRY_RUN", "true"), True),
        auto_select_enabled=_to_bool(_get("AUTO_SELECT_ENABLED", "true"), True),
        max_active_positions=int(_get("MAX_ACTIVE_POSITIONS", "3")),
        candidate_symbols=_get(
            "CANDIDATE_SYMBOLS",
            "005930,000660,035420,005380,068270,035720,051910,207940",
        ).strip(),
        defensive_symbols=_get("DEFENSIVE_SYMBOLS", "005930,055550,105560,316140").strip(),
        symbol_sector_map=_get("SYMBOL_SECTOR_MAP", "").strip(),
        sector_auto_map_enabled=_to_bool(_get("SECTOR_AUTO_MAP_ENABLED", "true"), True),
        sector_cache_path=_get("SECTOR_CACHE_PATH", "data/sector_map_cache.json").strip(),
        candidate_refresh_enabled=_to_bool(_get("CANDIDATE_REFRESH_ENABLED", "true"), True),
        candidate_refresh_once_daily=_to_bool(_get("CANDIDATE_REFRESH_ONCE_DAILY", "true"), True),
        candidate_refresh_minutes=int(_get("CANDIDATE_REFRESH_MINUTES", "120")),
        candidate_refresh_top_n=int(_get("CANDIDATE_REFRESH_TOP_N", "12")),
        candidate_refresh_min_score=float(_get("CANDIDATE_REFRESH_MIN_SCORE", "0.0")),
        intraday_reselect_enabled=_to_bool(_get("INTRADAY_RESELECT_ENABLED", "false"), False),
        intraday_reselect_minutes=max(1, int(_get("INTRADAY_RESELECT_MINUTES", "10"))),
        universe_symbols=_get(
            "UNIVERSE_SYMBOLS",
            "005930,000660,035420,005380,068270,035720,051910,207940,066570,034730,003670,096770,012330,028260,105560,055550,316140",
        ).strip(),
        auto_universe_enabled=_to_bool(_get("AUTO_UNIVERSE_ENABLED", "false"), False),
        auto_universe_source_url=_get(
            "AUTO_UNIVERSE_SOURCE_URL",
            "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13",
        ).strip(),
        auto_universe_refresh_minutes=int(_get("AUTO_UNIVERSE_REFRESH_MINUTES", "720")),
        universe_batch_size=int(_get("UNIVERSE_BATCH_SIZE", "400")),
        rebalance_minutes=int(_get("REBALANCE_MINUTES", "60")),
        momentum_lookback_days=int(_get("MOMENTUM_LOOKBACK_DAYS", "20")),
        min_momentum_pct=float(_get("MIN_MOMENTUM_PCT", "1.0")),
        signal_confirm_cycles=int(_get("SIGNAL_CONFIRM_CYCLES", "2")),
        adaptive_edge_enabled=_to_bool(_get("ADAPTIVE_EDGE_ENABLED", "true"), True),
        adaptive_edge_lookback_days=int(_get("ADAPTIVE_EDGE_LOOKBACK_DAYS", "30")),
        adaptive_edge_strength=float(_get("ADAPTIVE_EDGE_STRENGTH", "1.0")),
        regime_switch_confirm_cycles=int(_get("REGIME_SWITCH_CONFIRM_CYCLES", "2")),
        regime_switch_min_confidence=float(_get("REGIME_SWITCH_MIN_CONFIDENCE", "0.60")),
        sizing_vol_lookback_days=int(_get("SIZING_VOL_LOOKBACK_DAYS", "20")),
        atr_exit_lookback_days=int(_get("ATR_EXIT_LOOKBACK_DAYS", "14")),
        atr_stop_mult=float(_get("ATR_STOP_MULT", "2.2")),
        atr_take_mult=float(_get("ATR_TAKE_MULT", "3.0")),
        atr_trailing_mult=float(_get("ATR_TRAILING_MULT", "1.8")),
        target_risk_per_trade_pct=float(_get("TARGET_RISK_PER_TRADE_PCT", "0.80")),
        daily_loss_limit_pct=float(_get("DAILY_LOSS_LIMIT_PCT", "-2.0")),
        trade_cooldown_sec=int(_get("TRADE_COOLDOWN_SEC", "300")),
        stale_data_max_age_sec=int(_get("STALE_DATA_MAX_AGE_SEC", "120")),
        stop_loss_pct=float(_get("STOP_LOSS_PCT", "-3.0")),
        take_profit_pct=float(_get("TAKE_PROFIT_PCT", "5.0")),
        trailing_stop_pct=float(_get("TRAILING_STOP_PCT", "2.0")),
        min_entry_score=float(_get("MIN_ENTRY_SCORE", "0.5")),
        min_entry_momentum_pct=float(_get("MIN_ENTRY_MOMENTUM_PCT", "0.2")),
        take_profit_partial_ratio=float(_get("TAKE_PROFIT_PARTIAL_RATIO", "0.5")),
        min_entry_score_map=_get("MIN_ENTRY_SCORE_MAP", "").strip(),
        min_entry_momentum_map=_get("MIN_ENTRY_MOMENTUM_MAP", "").strip(),
        take_profit_partial_ratio_map=_get("TAKE_PROFIT_PARTIAL_RATIO_MAP", "").strip(),
        auto_param_tuning_enabled=_to_bool(_get("AUTO_PARAM_TUNING_ENABLED", "true"), True),
        auto_param_tuning_strength=float(_get("AUTO_PARAM_TUNING_STRENGTH", "1.0")),
        trade_mode=trade_mode,
        live_armed=_to_bool(_get("LIVE_ARMED", "false"), False),
        max_symbol_loss_pct=float(_get("MAX_SYMBOL_LOSS_PCT", "-4.0")),
        max_portfolio_heat_pct=float(_get("MAX_PORTFOLIO_HEAT_PCT", "35.0")),
        slack_enabled=_to_bool(_get("SLACK_ENABLED", "false"), False),
        slack_webhook_url=_get("SLACK_WEBHOOK_URL", "").strip(),
        slack_event_keywords=_get("SLACK_EVENT_KEYWORDS", "REGIME_SHIFT,RISK_EXIT,ORDER RESULT,Startup error").strip(),
        slack_bot_token=_get("SLACK_BOT_TOKEN", "").strip(),
        slack_channel_id=_get("SLACK_CHANNEL_ID", "").strip(),
        slack_attach_web_capture=_to_bool(_get("SLACK_ATTACH_WEB_CAPTURE", "false"), False),
        slack_capture_url=_get("SLACK_CAPTURE_URL", "http://127.0.0.1:8080/").strip(),
        slack_capture_width=int(_get("SLACK_CAPTURE_WIDTH", "1600")),
        slack_capture_height=int(_get("SLACK_CAPTURE_HEIGHT", "1100")),
        morning_brief_enabled=_to_bool(_get("MORNING_BRIEF_ENABLED", "true"), True),
        morning_brief_hour=int(_get("MORNING_BRIEF_HOUR", "8")),
        morning_brief_minute=int(_get("MORNING_BRIEF_MINUTE", "0")),
        market_brief_times=_get(
            "MARKET_BRIEF_TIMES",
            f"{int(_get('MORNING_BRIEF_HOUR', '8')):02d}:{int(_get('MORNING_BRIEF_MINUTE', '0')):02d},12:00,16:00,21:00",
        ).strip(),
        market_brief_history_path=_get("MARKET_BRIEF_HISTORY_PATH", "data/market_brief_history.json").strip(),
        selection_change_slack_enabled=_to_bool(_get("SELECTION_CHANGE_SLACK_ENABLED", "true"), True),
        hourly_market_report_enabled=_to_bool(_get("HOURLY_MARKET_REPORT_ENABLED", "true"), True),
        morning_news_enabled=_to_bool(_get("MORNING_NEWS_ENABLED", "true"), True),
        morning_news_queries=_get(
            "MORNING_NEWS_QUERIES",
            "KOSPI OR KOSDAQ market today,KRX stock market today,Korea stock market today,US stock market overnight today,Fed OR CPI market today,semiconductor Korea today",
        ).strip(),
        morning_news_limit=max(5, int(_get("MORNING_NEWS_LIMIT", "8"))),
        us_mock_enabled=_to_bool(_get("US_MOCK_ENABLED", "false"), False),
        us_mock_symbols=_get(
            "US_MOCK_SYMBOLS",
            "SPY,QQQ,AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA",
        ).strip(),
        us_mock_benchmark_symbol=_get("US_MOCK_BENCHMARK_SYMBOL", "SPY").strip().upper(),
        us_mock_initial_cash=float(_get("US_MOCK_INITIAL_CASH", "1000000")),
        us_mock_lookback_days=int(_get("US_MOCK_LOOKBACK_DAYS", "120")),
        us_mock_top_n=int(_get("US_MOCK_TOP_N", "4")),
        us_mock_report_hour=int(_get("US_MOCK_REPORT_HOUR", "8")),
        us_mock_report_minute=int(_get("US_MOCK_REPORT_MINUTE", "0")),
        us_mock_buy_drop_pct=float(_get("US_MOCK_BUY_DROP_PCT", "-0.9")),
        us_mock_sell_rise_pct=float(_get("US_MOCK_SELL_RISE_PCT", "1.4")),
        us_mock_signal_confirm_cycles=int(_get("US_MOCK_SIGNAL_CONFIRM_CYCLES", "2")),
        bar_interval_minutes=max(1, int(_get("BAR_INTERVAL_MINUTES", "2"))),
        decision_on_bar_close_only=_to_bool(_get("DECISION_ON_BAR_CLOSE_ONLY", "true"), True),
        trend_select_count=max(1, int(_get("TREND_SELECT_COUNT", "5"))),
        trend_min_avg_turnover20_krw=float(_get("TREND_MIN_AVG_TURNOVER20_KRW", "5000000000")),
        trend_min_turnover_ratio_5_to_20=float(_get("TREND_MIN_TURNOVER_RATIO_5_TO_20", "1.20")),
        trend_min_value_spike_ratio=float(_get("TREND_MIN_VALUE_SPIKE_RATIO", "1.10")),
        trend_breakout_near_high_pct=float(_get("TREND_BREAKOUT_NEAR_HIGH_PCT", "97.0")),
        trend_min_atr14_pct=float(_get("TREND_MIN_ATR14_PCT", "1.5")),
        trend_max_atr14_pct=float(_get("TREND_MAX_ATR14_PCT", "8.0")),
        trend_breakout_buffer_pct=float(_get("TREND_BREAKOUT_BUFFER_PCT", "3.0")),
        trend_overheat_day_pct=float(_get("TREND_OVERHEAT_DAY_PCT", "18.0")),
        trend_overheat_2day_pct=float(_get("TREND_OVERHEAT_2DAY_PCT", "25.0")),
        trend_gap_skip_up_pct=float(_get("TREND_GAP_SKIP_UP_PCT", "6.0")),
        trend_gap_skip_down_pct=float(_get("TREND_GAP_SKIP_DOWN_PCT", "-3.0")),
        trend_daily_rsi_min=float(_get("TREND_DAILY_RSI_MIN", "55.0")),
        trend_daily_rsi_max=float(_get("TREND_DAILY_RSI_MAX", "78.0")),
        trend_max_chase_from_open_pct=float(_get("TREND_MAX_CHASE_FROM_OPEN_PCT", "8.0")),
        enable_bearish_exception=_to_bool(_get("ENABLE_BEARISH_EXCEPTION", "false"), False),
        bearish_exception_trigger_pct=float(_get("BEARISH_EXCEPTION_TRIGGER_PCT", "-0.4")),
        bearish_exception_max_market_drop_pct=float(_get("BEARISH_EXCEPTION_MAX_MARKET_DROP_PCT", "-9.0")),
        bearish_exception_max_vol_pct=float(_get("BEARISH_EXCEPTION_MAX_VOL_PCT", "3.2")),
        trend_capital_per_name_pct=float(_get("TREND_CAPITAL_PER_NAME_PCT", "12.0")),
        trend_risk_per_trade_pct=float(_get("TREND_RISK_PER_TRADE_PCT", "0.40")),
        trend_max_sector_names=max(1, int(_get("TREND_MAX_SECTOR_NAMES", "2"))),
        strategy_mode=_get("STRATEGY_MODE", "AUTO").strip().upper(),
        market_timezone=_get("MARKET_TIMEZONE", "Asia/Seoul").strip() or "Asia/Seoul",
        enable_krx_session_gates=_to_bool(_get("ENABLE_KRX_SESSION_GATES", "true"), True),
        premarket_brief_start=_get("PREMARKET_BRIEF_START", "07:00").strip(),
        premarket_brief_end=_get("PREMARKET_BRIEF_END", "08:30").strip(),
        opening_focus_start=_get("OPENING_FOCUS_START", "08:50").strip(),
        opening_focus_end=_get("OPENING_FOCUS_END", "09:15").strip(),
        regular_session_start=_get("REGULAR_SESSION_START", "09:00").strip(),
        regular_session_end=_get("REGULAR_SESSION_END", "15:30").strip(),
        after_market_start=_get("AFTER_MARKET_START", "16:00").strip(),
        after_market_end=_get("AFTER_MARKET_END", "20:00").strip(),
        allow_after_market_sell=_to_bool(_get("ALLOW_AFTER_MARKET_SELL", "true"), True),
        market_shock_drop_pct=float(_get("MARKET_SHOCK_DROP_PCT", "-2.0")),
        vkospi_spike_proxy_pct=float(_get("VKOSPI_SPIKE_PROXY_PCT", "3.8")),
        manual_market_alert=_get("MANUAL_MARKET_ALERT", "").strip(),
        market_status_filter_enabled=_to_bool(_get("MARKET_STATUS_FILTER_ENABLED", "false"), False),
        market_policy_caution_risk_score=float(_get("MARKET_POLICY_CAUTION_RISK_SCORE", "45.0")),
        market_policy_halt_risk_score=float(_get("MARKET_POLICY_HALT_RISK_SCORE", "72.0")),
        market_policy_caution_confirm_extra=max(0, int(_get("MARKET_POLICY_CAUTION_CONFIRM_EXTRA", "1"))),
        market_policy_halt_confirm_extra=max(0, int(_get("MARKET_POLICY_HALT_CONFIRM_EXTRA", "2"))),
        market_policy_caution_entry_score_boost=float(_get("MARKET_POLICY_CAUTION_ENTRY_SCORE_BOOST", "0.08")),
        market_policy_halt_entry_score_boost=float(_get("MARKET_POLICY_HALT_ENTRY_SCORE_BOOST", "0.20")),
        market_policy_caution_entry_momentum_boost_pct=float(_get("MARKET_POLICY_CAUTION_ENTRY_MOMENTUM_BOOST_PCT", "0.20")),
        market_policy_halt_entry_momentum_boost_pct=float(_get("MARKET_POLICY_HALT_ENTRY_MOMENTUM_BOOST_PCT", "0.60")),
        market_policy_scalping_min_volume_boost=float(_get("MARKET_POLICY_SCALPING_MIN_VOLUME_BOOST", "0.20")),
        manager_reason_ema_alpha_min=float(_get("MANAGER_REASON_EMA_ALPHA_MIN", "0.08")),
        manager_reason_ema_alpha_max=float(_get("MANAGER_REASON_EMA_ALPHA_MAX", "0.40")),
        manager_reason_ema_scale_trend=float(_get("MANAGER_REASON_EMA_SCALE_TREND", "1.05")),
        manager_reason_ema_scale_scalping=float(_get("MANAGER_REASON_EMA_SCALE_SCALPING", "1.15")),
        manager_reason_ema_scale_defensive=float(_get("MANAGER_REASON_EMA_SCALE_DEFENSIVE", "0.85")),
        compare_warn_win_rate_gap_pct=float(_get("COMPARE_WARN_WIN_RATE_GAP_PCT", "20.0")),
        compare_warn_pnl_gap_krw=float(_get("COMPARE_WARN_PNL_GAP_KRW", "100000")),
        compare_warn_expectancy_gap_krw=float(_get("COMPARE_WARN_EXPECTANCY_GAP_KRW", "10000")),
        compare_warn_hold_gap_days=float(_get("COMPARE_WARN_HOLD_GAP_DAYS", "1.0")),
        ios_testflight_url=_get("IOS_TESTFLIGHT_URL", "").strip(),
        ios_app_store_url=_get("IOS_APP_STORE_URL", "").strip(),
        ios_manifest_url=_get("IOS_MANIFEST_URL", "").strip(),
        mobile_server_url=_get("MOBILE_SERVER_URL", "").strip(),
        mobile_server_label=_get("MOBILE_SERVER_LABEL", "AITRADER Server").strip(),
        mobile_app_scheme=_get("MOBILE_APP_SCHEME", "aitrader").strip(),
        web_https_enabled=_to_bool(_get("WEB_HTTPS_ENABLED", "true"), True),
        web_https_port=max(1, int(_get("WEB_HTTPS_PORT", "8443"))),
        web_ssl_certfile=_get("WEB_SSL_CERTFILE", "data/certs/web-local.crt").strip(),
        web_ssl_keyfile=_get("WEB_SSL_KEYFILE", "data/certs/web-local.key").strip(),
        web_access_enabled=_to_bool(_get("WEB_ACCESS_ENABLED", "false"), False),
        web_access_key=_get("WEB_ACCESS_KEY", "").strip(),
        web_trusted_device_days=max(1, int(_get("WEB_TRUSTED_DEVICE_DAYS", "30"))),
        web_max_trusted_devices=max(1, int(_get("WEB_MAX_TRUSTED_DEVICES", "1"))),
        # Scalping parameters
        scalping_rsi_entry_min=float(_get("SCALPING_RSI_ENTRY_MIN", "30.0")),
        scalping_rsi_entry_max=float(_get("SCALPING_RSI_ENTRY_MAX", "70.0")),
        scalping_rsi_exit_min=float(_get("SCALPING_RSI_EXIT_MIN", "25.0")),
        scalping_rsi_exit_max=float(_get("SCALPING_RSI_EXIT_MAX", "75.0")),
        scalping_volume_spike_ratio=float(_get("SCALPING_VOLUME_SPIKE_RATIO", "2.0")),
        scalping_profit_target_pct=float(_get("SCALPING_PROFIT_TARGET_PCT", "0.8")),
        scalping_stop_loss_pct=float(_get("SCALPING_STOP_LOSS_PCT", "-0.5")),
        scalping_max_hold_bars=int(_get("SCALPING_MAX_HOLD_BARS", "6")),
        scalping_min_trend_strength=float(_get("SCALPING_MIN_TREND_STRENGTH", "0.1")),
        scalping_min_volume_ratio=float(_get("SCALPING_MIN_VOLUME_RATIO", "1.5")),
    )


def parse_symbol_list(raw: str, *, fallback: str = "") -> list[str]:
    symbols: list[str] = []
    for part in str(raw or fallback or "").split(","):
        sym = part.strip()
        if sym:
            symbols.append(sym)
    return list(dict.fromkeys(symbols))


def selection_universe_symbols(settings: Settings) -> list[str]:
    """
    Build the stock-selection universe without treating candidate/defensive
    lists as hard boundaries. `universe_symbols` is the primary source; the
    legacy lists are only merged in as additional seeds/fallbacks.
    """
    merged: list[str] = []
    merged.extend(parse_symbol_list(settings.universe_symbols, fallback=settings.symbol))
    merged.extend(parse_symbol_list(settings.candidate_symbols))
    merged.extend(parse_symbol_list(settings.defensive_symbols))
    if settings.symbol:
        merged.append(str(settings.symbol).strip())
    unique = list(dict.fromkeys([sym for sym in merged if sym]))
    # Guardrail: when auto-universe is enabled, avoid a single-symbol fallback
    # caused by empty overrides (e.g., UNIVERSE/CANDIDATE/DEFENSIVE all blank).
    if bool(getattr(settings, "auto_universe_enabled", False)) and len(unique) <= 1:
        seed = parse_symbol_list(
            "005930,000660,035420,005380,068270,035720,051910,207940,066570,034730,003670,096770,012330,028260,105560,055550,316140"
        )
        unique = list(dict.fromkeys(unique + seed))
    return unique


def _load_runtime_overrides() -> dict[str, str]:
    global _RUNTIME_CACHE_DATA, _RUNTIME_CACHE_MTIME_NS
    if not RUNTIME_CONFIG_PATH.exists():
        _RUNTIME_CACHE_MTIME_NS = None
        _RUNTIME_CACHE_DATA = {}
        return {}
    try:
        mtime_ns = RUNTIME_CONFIG_PATH.stat().st_mtime_ns
    except Exception:
        return {}
    if _RUNTIME_CACHE_MTIME_NS == mtime_ns:
        return dict(_RUNTIME_CACHE_DATA)
    try:
        data = json.loads(RUNTIME_CONFIG_PATH.read_text())
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
    _RUNTIME_CACHE_MTIME_NS = mtime_ns
    _RUNTIME_CACHE_DATA = dict(out)
    return out


def save_runtime_overrides(updates: dict[str, str]) -> None:
    global _RUNTIME_CACHE_MTIME_NS
    base = _load_runtime_overrides()
    normalized_updates: dict[str, str] = {}
    for k, v in updates.items():
        key = str(k).strip().upper()
        if not key:
            continue
        value = str(v).strip()
        normalized_updates[key] = value
        if value == "":
            base.pop(key, None)
        else:
            base[key] = value
    RUNTIME_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_CONFIG_PATH.write_text(json.dumps(base, ensure_ascii=False, indent=2))
    _save_env_updates(normalized_updates)
    _RUNTIME_CACHE_MTIME_NS = None


def _save_env_updates(updates: dict[str, str]) -> None:
    if not updates:
        return

    env_path = Path(".env")
    if env_path.exists():
        lines = env_path.read_text().splitlines()
    else:
        lines = []

    key_to_idx: dict[str, int] = {}
    env_key_re = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
    for i, line in enumerate(lines):
        m = env_key_re.match(line)
        if not m:
            continue
        key = m.group(1).strip().upper()
        if key and key not in key_to_idx:
            key_to_idx[key] = i

    for key, value in updates.items():
        clean_value = str(value).replace("\n", " ").replace("\r", " ").strip()
        new_line = f"{key}={clean_value}"
        if key in key_to_idx:
            lines[key_to_idx[key]] = new_line
        else:
            lines.append(new_line)
            key_to_idx[key] = len(lines) - 1

    out = "\n".join(lines)
    if out:
        out += "\n"
    env_path.write_text(out)
