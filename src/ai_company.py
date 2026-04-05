from __future__ import annotations

import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from bot_runtime import BotState, run_bot
from config import load_settings


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


@dataclass
class AgentOutput:
    agent: str
    summary: str
    payload: dict[str, Any]


class BaseAgent:
    name = "base"

    def execute(self, state: BotState, context: dict[str, Any]) -> AgentOutput:
        raise NotImplementedError


def _manager_order(context: dict[str, Any], agent_name: str, default: str) -> str:
    orders = context.get("work_orders")
    if isinstance(orders, dict):
        value = orders.get(agent_name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


class ManagerLearningStore:
    def __init__(
        self,
        path: str = "data/manager_learning_state.json",
        ledger_path: str = "data/ledger.json",
    ) -> None:
        self.path = Path(path)
        self.ledger_path = Path(ledger_path)
        self._alpha_cfg = {
            "min": 0.08,
            "max": 0.40,
            "scale": {
                "trend": 1.05,
                "scalping": 1.15,
                "defensive": 0.85,
            },
        }
        try:
            settings = load_settings()
            a_min = _clamp(_safe_float(getattr(settings, "manager_reason_ema_alpha_min", 0.08), 0.08), 0.05, 0.45)
            a_max = _clamp(_safe_float(getattr(settings, "manager_reason_ema_alpha_max", 0.40), 0.40), 0.06, 0.50)
            if a_max <= a_min:
                a_max = min(0.50, a_min + 0.05)
            self._alpha_cfg = {
                "min": a_min,
                "max": a_max,
                "scale": {
                    "trend": _clamp(_safe_float(getattr(settings, "manager_reason_ema_scale_trend", 1.05), 1.05), 0.60, 1.60),
                    "scalping": _clamp(_safe_float(getattr(settings, "manager_reason_ema_scale_scalping", 1.15), 1.15), 0.60, 1.80),
                    "defensive": _clamp(_safe_float(getattr(settings, "manager_reason_ema_scale_defensive", 0.85), 0.85), 0.50, 1.40),
                },
            }
        except Exception:
            pass
        self._cache = self._load()

    def _default_state(self) -> dict[str, Any]:
        return {
            "sleeve_bias": {
                "trend": 1.0,
                "scalping": 1.0,
                "defensive": 1.0,
            },
            "last_total_return_pct": 0.0,
            "last_realized_pnl": 0.0,
            "last_processed_trade_index": 0,
            "sleeve_realized_totals": {
                "trend": 0.0,
                "scalping": 0.0,
                "defensive": 0.0,
            },
            "time_bucket_stats": {
                "opening": {"sells": 0, "wins": 0, "realized": 0.0},
                "regular": {"sells": 0, "wins": 0, "realized": 0.0},
                "after": {"sells": 0, "wins": 0, "realized": 0.0},
                "off": {"sells": 0, "wins": 0, "realized": 0.0},
            },
            "reason_code_stats": {},
            "reason_code_ema": {},
            "reason_code_ema_alpha": {},
            "daily_outlook": {
                "label": "INSUFFICIENT_DATA",
                "market_quality_score": 0.0,
                "expected_profit_per_trade_krw": 0.0,
                "expected_daily_profit_krw": 0.0,
                "expected_daily_profit_rate_pct": 0.0,
                "expected_trades_per_day": 0.0,
                "active_sell_days": 0,
                "historical_sell_trades": 0,
                "confidence": 0.0,
                "risk_level": "LOW",
            },
            "daily_outlook_history": {},
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }

    def _load(self) -> dict[str, Any]:
        try:
            if not self.path.exists():
                return self._default_state()
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return self._default_state()
            base = self._default_state()
            base.update(payload)
            return base
        except Exception:
            return self._default_state()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cache["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self.path.write_text(json.dumps(self._cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def snapshot(self) -> dict[str, Any]:
        return dict(self._cache)

    def _bias(self) -> dict[str, float]:
        raw = dict(self._cache.get("sleeve_bias") or {})
        return {
            "trend": _safe_float(raw.get("trend"), 1.0),
            "scalping": _safe_float(raw.get("scalping"), 1.0),
            "defensive": _safe_float(raw.get("defensive"), 1.0),
        }

    @staticmethod
    def _detect_sleeve(row: dict[str, Any]) -> str:
        explicit = str(row.get("ai_sleeve") or "").strip().lower()
        if explicit in {"trend", "scalping", "defensive"}:
            return explicit

        merged = " ".join(
            [
                str(row.get("entry_mode") or ""),
                str(row.get("strategy_profile") or ""),
                str(row.get("setup_state") or ""),
                str(row.get("sentiment_class") or ""),
            ]
        ).upper()

        if "SCALP" in merged:
            return "scalping"
        if any(key in merged for key in ["DEFENSIVE", "RISK_OFF", "BEARISH", "CAPITAL_PRESERVATION"]):
            return "defensive"
        return "trend"

    @staticmethod
    def _bucket_from_trade_ts(ts_value: str) -> str:
        raw = str(ts_value or "").strip()
        if not raw:
            return "off"
        try:
            dt = datetime.fromisoformat(raw)
        except Exception:
            try:
                dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
            except Exception:
                return "off"

        hhmm = dt.hour * 60 + dt.minute
        if 9 * 60 <= hhmm < 10 * 60:
            return "opening"
        if 10 * 60 <= hhmm < 15 * 60 + 20:
            return "regular"
        if 15 * 60 + 20 <= hhmm < 16 * 60 + 30:
            return "after"
        return "off"

    def _collect_sleeve_realized_delta(self) -> dict[str, Any]:
        totals = {
            "trend": 0.0,
            "scalping": 0.0,
            "defensive": 0.0,
        }
        delta = {
            "trend": 0.0,
            "scalping": 0.0,
            "defensive": 0.0,
        }
        bucket_delta = {
            "opening": {"sells": 0, "wins": 0, "realized": 0.0},
            "regular": {"sells": 0, "wins": 0, "realized": 0.0},
            "after": {"sells": 0, "wins": 0, "realized": 0.0},
            "off": {"sells": 0, "wins": 0, "realized": 0.0},
        }
        reason_totals: dict[str, dict[str, float]] = {}
        reason_delta: dict[str, dict[str, float]] = {}
        sell_days: set[str] = set()
        start_idx = int(_safe_float(self._cache.get("last_processed_trade_index"), 0.0))
        end_idx = start_idx

        try:
            if not self.ledger_path.exists():
                return {
                    "totals": totals,
                    "delta": delta,
                    "bucket_delta": bucket_delta,
                    "reason_totals": reason_totals,
                    "reason_delta": reason_delta,
                    "active_sell_days": 0,
                    "historical_sell_trades": 0,
                    "new_sells": 0,
                    "start_idx": start_idx,
                    "end_idx": end_idx,
                }

            payload = json.loads(self.ledger_path.read_text(encoding="utf-8"))
            trades = list(payload.get("trades") or []) if isinstance(payload, dict) else []
            end_idx = len(trades)

            for row in trades:
                if not isinstance(row, dict):
                    continue
                if str(row.get("side") or "") != "SELL":
                    continue
                sleeve = self._detect_sleeve(row)
                pnl = _safe_float(row.get("realized_pnl"), 0.0)
                if sleeve in totals:
                    totals[sleeve] += pnl
                ts_raw = str(row.get("ts") or "")
                day_key = ts_raw.split("T")[0].split(" ")[0].strip()
                if day_key:
                    sell_days.add(day_key)
                reason = str(row.get("ai_sleeve_reason") or "RSN_UNKNOWN").strip() or "RSN_UNKNOWN"
                if reason not in reason_totals:
                    reason_totals[reason] = {"sells": 0.0, "wins": 0.0, "realized": 0.0}
                reason_totals[reason]["sells"] += 1.0
                reason_totals[reason]["realized"] += pnl
                if pnl > 0:
                    reason_totals[reason]["wins"] += 1.0

            new_rows = trades[start_idx:]
            new_sell_count = 0
            for row in new_rows:
                if not isinstance(row, dict):
                    continue
                if str(row.get("side") or "") != "SELL":
                    continue
                new_sell_count += 1
                sleeve = self._detect_sleeve(row)
                pnl = _safe_float(row.get("realized_pnl"), 0.0)
                if sleeve in delta:
                    delta[sleeve] += pnl
                bucket = self._bucket_from_trade_ts(str(row.get("ts") or ""))
                if bucket in bucket_delta:
                    bucket_delta[bucket]["sells"] += 1
                    bucket_delta[bucket]["realized"] += pnl
                    if pnl > 0:
                        bucket_delta[bucket]["wins"] += 1
                reason = str(row.get("ai_sleeve_reason") or "RSN_UNKNOWN").strip() or "RSN_UNKNOWN"
                if reason not in reason_delta:
                    reason_delta[reason] = {"sells": 0.0, "wins": 0.0, "realized": 0.0}
                reason_delta[reason]["sells"] += 1.0
                reason_delta[reason]["realized"] += pnl
                if pnl > 0:
                    reason_delta[reason]["wins"] += 1.0

            return {
                "totals": totals,
                "delta": delta,
                "bucket_delta": bucket_delta,
                "reason_totals": reason_totals,
                "reason_delta": reason_delta,
                "active_sell_days": len(sell_days),
                "historical_sell_trades": int(sum(_safe_float((x or {}).get("sells"), 0.0) for x in reason_totals.values())),
                "new_sells": int(new_sell_count),
                "start_idx": start_idx,
                "end_idx": end_idx,
            }
        except Exception:
            return {
                "totals": totals,
                "delta": delta,
                "bucket_delta": bucket_delta,
                "reason_totals": reason_totals,
                "reason_delta": reason_delta,
                "active_sell_days": len(sell_days),
                "historical_sell_trades": int(sum(_safe_float((x or {}).get("sells"), 0.0) for x in reason_totals.values())),
                "new_sells": 0,
                "start_idx": start_idx,
                "end_idx": end_idx,
            }

    @staticmethod
    def _wilson_lower_bound(wins: int, trials: int, z: float = 1.96) -> float:
        if trials <= 0:
            return 0.0
        p = max(0.0, min(1.0, float(wins) / float(trials)))
        z2 = z * z
        denom = 1.0 + z2 / trials
        center = p + z2 / (2.0 * trials)
        margin = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * trials)) / trials)
        return max(0.0, min(1.0, (center - margin) / denom))

    @staticmethod
    def _target_sleeve_from_reason(reason_code: str) -> str:
        code_u = str(reason_code or "").upper()
        if "SCALP" in code_u:
            return "scalping"
        if "DEFENSIVE" in code_u or "RISK_OFF" in code_u or "BEARISH" in code_u:
            return "defensive"
        return "trend"

    def _dynamic_ema_alpha(self, delta_avg: float, hist_sells: int, prev_ema: float, target_sleeve: str) -> float:
        # More history -> slower updates. Higher volatility -> slower updates.
        # Sign flip vs previous EMA -> slightly faster adaptation.
        sample_conf = _clamp(float(hist_sells) / 20.0, 0.0, 1.0)
        vol_norm = _clamp(abs(delta_avg) / 20000.0, 0.0, 1.0)
        alpha = 0.35 - (0.20 * sample_conf) - (0.10 * vol_norm)
        if prev_ema * delta_avg < 0:
            alpha += 0.05
        scale_map = dict(self._alpha_cfg.get("scale") or {})
        sleeve_scale = _safe_float(scale_map.get(str(target_sleeve or "").lower()), 1.0)
        alpha *= sleeve_scale
        a_min = _safe_float(self._alpha_cfg.get("min"), 0.08)
        a_max = _safe_float(self._alpha_cfg.get("max"), 0.40)
        if a_max <= a_min:
            a_max = a_min + 0.05
        return round(_clamp(alpha, a_min, a_max), 4)

    def update_from_cycle(self, state: BotState, by_name: dict[str, AgentOutput]) -> dict[str, Any]:
        bias = self._bias()
        current_return = _safe_float(state.total_return_pct, 0.0)
        last_return = _safe_float(self._cache.get("last_total_return_pct"), 0.0)
        pnl_delta = current_return - last_return
        current_realized = _safe_float(state.realized_pnl, 0.0)
        last_realized = _safe_float(self._cache.get("last_realized_pnl"), 0.0)
        realized_delta = current_realized - last_realized
        sleeve_attr = self._collect_sleeve_realized_delta()

        risk_payload = (by_name.get("risk_guard").payload if by_name.get("risk_guard") else {})
        market_payload = (by_name.get("market_analysis").payload if by_name.get("market_analysis") else {})
        trend_payload = (by_name.get("invest_trend").payload if by_name.get("invest_trend") else {})
        scalp_payload = (by_name.get("invest_scalping").payload if by_name.get("invest_scalping") else {})
        def_payload = (by_name.get("invest_defensive").payload if by_name.get("invest_defensive") else {})

        risk_level = str(risk_payload.get("risk_level") or "LOW")
        market_confidence = _clamp(_safe_float(market_payload.get("confidence"), 0.0), 0.0, 1.0)
        trend_signal = str(trend_payload.get("signal") or "WAIT")
        scalp_signal = str(scalp_payload.get("signal") or "STANDBY")
        def_posture = str(def_payload.get("posture") or "BUFFER")

        journal_rows = list(state.order_journal or [])
        filled_rows = [
            row
            for row in journal_rows
            if str(row.get("status") or "") in {"FILLED_SIM", "FILLED_LOCAL"}
        ]
        recent_filled = filled_rows[-12:]
        buy_fills = sum(1 for row in recent_filled if str(row.get("side") or "") == "BUY")
        sell_fills = sum(1 for row in recent_filled if str(row.get("side") or "") == "SELL")

        reasons: list[str] = []
        effective_delta = realized_delta if abs(realized_delta) > 0.0 else pnl_delta

        if effective_delta >= 0.03:
            if trend_signal == "ACCUMULATE":
                bias["trend"] += 0.03
                reasons.append("reward_trend")
            if scalp_signal == "TRADE":
                bias["scalping"] += 0.03
                reasons.append("reward_scalping")
            if not reasons:
                bias["defensive"] += 0.02
                reasons.append("reward_defensive")
        elif effective_delta <= -0.03:
            if trend_signal == "ACCUMULATE":
                bias["trend"] -= 0.04
                reasons.append("penalize_trend")
            if scalp_signal == "TRADE":
                bias["scalping"] -= 0.04
                reasons.append("penalize_scalping")
            bias["defensive"] += 0.03
            reasons.append("boost_defensive")

        if sell_fills >= 2 and realized_delta > 0:
            bias["trend"] += 0.02
            bias["scalping"] += 0.01
            reasons.append("realized_win_batch")
        elif sell_fills >= 2 and realized_delta < 0:
            bias["trend"] -= 0.02
            bias["scalping"] -= 0.02
            bias["defensive"] += 0.02
            reasons.append("realized_loss_batch")

        if buy_fills > sell_fills + 2 and effective_delta < 0:
            bias["defensive"] += 0.02
            reasons.append("overtrading_guard")

        sleeve_delta = dict(sleeve_attr.get("delta") or {})
        for sleeve in ("trend", "scalping", "defensive"):
            pnl = _safe_float(sleeve_delta.get(sleeve), 0.0)
            if pnl > 0:
                bias[sleeve] += 0.03
                reasons.append(f"attr_gain_{sleeve}")
            elif pnl < 0:
                bias[sleeve] -= 0.03
                reasons.append(f"attr_loss_{sleeve}")

        # Reason-code expectancy signal: apply sleeve-level reinforcement from
        # standardized reason performance, but only when sample size is sufficient.
        reason_delta = dict(sleeve_attr.get("reason_delta") or {})
        reason_totals = dict(sleeve_attr.get("reason_totals") or {})
        reason_ema = dict(self._cache.get("reason_code_ema") or {})
        reason_ema_alpha = {
            str(k): round(_safe_float(v, 0.0), 4)
            for k, v in dict(self._cache.get("reason_code_ema_alpha") or {}).items()
        }
        reason_signal_summary: dict[str, float] = {"trend": 0.0, "scalping": 0.0, "defensive": 0.0}

        # Update EMA with recent reason performance (delta window).
        for code, row in reason_delta.items():
            if not isinstance(row, dict):
                continue
            sells = int(_safe_float(row.get("sells"), 0.0))
            if sells <= 0:
                continue
            delta_avg = _safe_float(row.get("realized"), 0.0) / max(1, sells)
            hist_row = dict(reason_totals.get(code) or {})
            hist_sells = int(_safe_float(hist_row.get("sells"), 0.0))
            prev_ema = _safe_float(reason_ema.get(code), delta_avg)
            target_sleeve = self._target_sleeve_from_reason(str(code))
            alpha = self._dynamic_ema_alpha(delta_avg, hist_sells, prev_ema, target_sleeve)
            reason_ema_alpha[str(code)] = alpha
            reason_ema[code] = round((alpha * delta_avg) + ((1.0 - alpha) * prev_ema), 6)

        for code, row in reason_delta.items():
            if not isinstance(row, dict):
                continue
            sells = int(_safe_float(row.get("sells"), 0.0))
            if sells <= 0:
                continue
            avg_realized = _safe_float(row.get("realized"), 0.0) / max(1, sells)
            ema_realized = _safe_float(reason_ema.get(code), avg_realized)
            blended_realized = (0.6 * avg_realized) + (0.4 * ema_realized)

            hist_row = dict(reason_totals.get(code) or {})
            hist_sells = int(_safe_float(hist_row.get("sells"), 0.0))
            hist_wins = int(_safe_float(hist_row.get("wins"), 0.0))
            win_lb = self._wilson_lower_bound(hist_wins, hist_sells)
            sample_conf = _clamp(float(hist_sells) / 20.0, 0.0, 1.0)

            target_sleeve = self._target_sleeve_from_reason(str(code))

            # Convert expectancy into bounded adjustment with confidence weighting.
            if hist_sells >= 3:
                quality_boost = 0.75 + win_lb
                raw_adj = (blended_realized / 20000.0) * sample_conf * quality_boost
                adj = _clamp(raw_adj, -0.03, 0.03)
                if abs(adj) >= 0.005:
                    bias[target_sleeve] += adj
                    reason_signal_summary[target_sleeve] = round(
                        _safe_float(reason_signal_summary.get(target_sleeve), 0.0) + adj,
                        4,
                    )
                    if adj > 0:
                        reasons.append(f"reason_expectancy_gain_{target_sleeve}")
                    else:
                        reasons.append(f"reason_expectancy_loss_{target_sleeve}")

        if risk_level in {"HIGH", "CRITICAL"}:
            bias["trend"] -= 0.02
            bias["scalping"] -= 0.02
            bias["defensive"] += 0.03
            reasons.append("risk_shift")

        if def_posture == "HEDGE":
            bias["defensive"] += 0.01
            reasons.append("hedge_bias")

        for key in ("trend", "scalping", "defensive"):
            bias[key] = round(_clamp(_safe_float(bias.get(key), 1.0), 0.60, 1.80), 4)

        self._cache["sleeve_bias"] = bias
        self._cache["last_total_return_pct"] = current_return
        self._cache["last_realized_pnl"] = current_realized
        self._cache["last_processed_trade_index"] = int(sleeve_attr.get("end_idx") or 0)
        self._cache["sleeve_realized_totals"] = {
            "trend": round(_safe_float((sleeve_attr.get("totals") or {}).get("trend"), 0.0), 4),
            "scalping": round(_safe_float((sleeve_attr.get("totals") or {}).get("scalping"), 0.0), 4),
            "defensive": round(_safe_float((sleeve_attr.get("totals") or {}).get("defensive"), 0.0), 4),
        }
        bucket_stats = dict(self._cache.get("time_bucket_stats") or {})
        for bucket in ["opening", "regular", "after", "off"]:
            cur = dict(bucket_stats.get(bucket) or {"sells": 0, "wins": 0, "realized": 0.0})
            add = dict((sleeve_attr.get("bucket_delta") or {}).get(bucket) or {})
            cur["sells"] = int(cur.get("sells") or 0) + int(add.get("sells") or 0)
            cur["wins"] = int(cur.get("wins") or 0) + int(add.get("wins") or 0)
            cur["realized"] = round(_safe_float(cur.get("realized"), 0.0) + _safe_float(add.get("realized"), 0.0), 4)
            bucket_stats[bucket] = cur
        self._cache["time_bucket_stats"] = bucket_stats
        self._cache["reason_code_ema"] = reason_ema
        self._cache["reason_code_ema_alpha"] = reason_ema_alpha
        reason_totals = dict(sleeve_attr.get("reason_totals") or {})
        normalized_reason_stats: dict[str, dict[str, float]] = {}
        for reason, row in reason_totals.items():
            if not isinstance(row, dict):
                continue
            sells = int(_safe_float(row.get("sells"), 0.0))
            wins = int(_safe_float(row.get("wins"), 0.0))
            realized = round(_safe_float(row.get("realized"), 0.0), 4)
            win_rate = round((wins / sells) * 100.0, 2) if sells > 0 else 0.0
            avg_realized = round(realized / sells, 4) if sells > 0 else 0.0
            win_rate_lb = round(self._wilson_lower_bound(wins, sells) * 100.0, 2) if sells > 0 else 0.0
            confidence = round(_clamp(float(sells) / 20.0, 0.0, 1.0), 4)
            ema_expectancy = round(_safe_float(reason_ema.get(reason), avg_realized), 4)
            blended_expectancy = round((0.6 * avg_realized) + (0.4 * ema_expectancy), 4)
            expectancy_score = round(blended_expectancy * confidence, 4)
            normalized_reason_stats[str(reason)] = {
                "sells": sells,
                "wins": wins,
                "realized": realized,
                "win_rate_pct": win_rate,
                "avg_realized": avg_realized,
                "win_rate_lb_pct": win_rate_lb,
                "confidence": confidence,
                "ema_expectancy": ema_expectancy,
                "ema_alpha": _safe_float(reason_ema_alpha.get(str(reason)), 0.0),
                "blended_expectancy": blended_expectancy,
                "expectancy_score": expectancy_score,
            }

        weighted_trades = 0.0
        weighted_expectancy = 0.0
        weighted_confidence = 0.0
        weighted_win_lb = 0.0
        for row in normalized_reason_stats.values():
            sells = max(0.0, _safe_float(row.get("sells"), 0.0))
            if sells <= 0:
                continue
            confidence = _clamp(_safe_float(row.get("confidence"), 0.0), 0.0, 1.0)
            blended = _safe_float(row.get("blended_expectancy"), 0.0)
            win_lb_ratio = _clamp(_safe_float(row.get("win_rate_lb_pct"), 0.0) / 100.0, 0.0, 1.0)
            w = sells * (0.5 + 0.5 * confidence)
            weighted_trades += w
            weighted_expectancy += blended * w
            weighted_confidence += confidence * w
            weighted_win_lb += win_lb_ratio * w

        expected_profit_per_trade = (weighted_expectancy / weighted_trades) if weighted_trades > 0 else 0.0
        hist_sell_trades = int(sleeve_attr.get("historical_sell_trades") or 0)
        active_sell_days = max(0, int(sleeve_attr.get("active_sell_days") or 0))
        expected_trades_per_day = (float(hist_sell_trades) / float(active_sell_days)) if active_sell_days > 0 else 0.0
        expected_daily_profit = expected_profit_per_trade * expected_trades_per_day

        capital_base = max(_safe_float(state.equity, 0.0), _safe_float(state.cash_balance, 0.0), 0.0)
        expected_daily_profit_rate_pct = 0.0
        if capital_base >= 1000.0:
            expected_daily_profit_rate_pct = (expected_daily_profit / capital_base) * 100.0
        expected_daily_profit_rate_pct = _clamp(expected_daily_profit_rate_pct, -15.0, 15.0)

        avg_confidence = (weighted_confidence / weighted_trades) if weighted_trades > 0 else 0.0
        avg_win_lb = (weighted_win_lb / weighted_trades) if weighted_trades > 0 else 0.0
        sample_quality = _clamp(float(hist_sell_trades) / 30.0, 0.0, 1.0)
        risk_mult = {
            "LOW": 1.00,
            "MEDIUM": 0.92,
            "HIGH": 0.78,
            "CRITICAL": 0.60,
        }.get(risk_level, 0.90)
        quality_score = 100.0 * (
            (0.35 * avg_confidence)
            + (0.30 * avg_win_lb)
            + (0.20 * sample_quality)
            + (0.15 * market_confidence)
        ) * risk_mult
        quality_score = round(_clamp(quality_score, 0.0, 99.0), 2)

        outlook_label = "INSUFFICIENT_DATA"
        if hist_sell_trades >= 5 and active_sell_days >= 2:
            if quality_score >= 65.0 and expected_daily_profit_rate_pct >= 0.20:
                outlook_label = "POSITIVE"
            elif quality_score >= 45.0 and expected_daily_profit_rate_pct >= 0.0:
                outlook_label = "NEUTRAL"
            else:
                outlook_label = "CAUTIOUS"

        daily_outlook = {
            "label": outlook_label,
            "market_quality_score": quality_score,
            "expected_profit_per_trade_krw": round(expected_profit_per_trade, 2),
            "expected_daily_profit_krw": round(expected_daily_profit, 2),
            "expected_daily_profit_rate_pct": round(expected_daily_profit_rate_pct, 4),
            "expected_trades_per_day": round(expected_trades_per_day, 3),
            "active_sell_days": active_sell_days,
            "historical_sell_trades": hist_sell_trades,
            "confidence": round(_clamp(avg_confidence, 0.0, 1.0), 4),
            "risk_level": risk_level,
        }

        today_key = datetime.now().date().isoformat()
        daily_outlook_history = dict(self._cache.get("daily_outlook_history") or {})
        daily_outlook_history[today_key] = {
            **daily_outlook,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        if len(daily_outlook_history) > 180:
            keep_keys = sorted(daily_outlook_history.keys())[-180:]
            daily_outlook_history = {k: daily_outlook_history[k] for k in keep_keys}
        self._cache["reason_code_stats"] = normalized_reason_stats
        self._cache["daily_outlook"] = daily_outlook
        self._cache["daily_outlook_history"] = daily_outlook_history
        self._save()

        return {
            "sleeve_bias": bias,
            "pnl_delta_pct": round(pnl_delta, 4),
            "realized_delta": round(realized_delta, 4),
            "sleeve_realized_delta": {
                "trend": round(_safe_float((sleeve_attr.get("delta") or {}).get("trend"), 0.0), 4),
                "scalping": round(_safe_float((sleeve_attr.get("delta") or {}).get("scalping"), 0.0), 4),
                "defensive": round(_safe_float((sleeve_attr.get("delta") or {}).get("defensive"), 0.0), 4),
            },
            "sleeve_realized_totals": self._cache.get("sleeve_realized_totals") or {},
            "time_bucket_delta": sleeve_attr.get("bucket_delta") or {},
            "time_bucket_stats": self._cache.get("time_bucket_stats") or {},
            "reason_code_delta": sleeve_attr.get("reason_delta") or {},
            "reason_code_stats": self._cache.get("reason_code_stats") or {},
            "daily_outlook": self._cache.get("daily_outlook") or {},
            "daily_outlook_history": self._cache.get("daily_outlook_history") or {},
            "reason_signal_summary": reason_signal_summary,
            "new_sell_trades": int(sleeve_attr.get("new_sells") or 0),
            "buy_fills": int(buy_fills),
            "sell_fills": int(sell_fills),
            "reasons": reasons,
            "risk_level": risk_level,
        }


class PerformanceFeedbackAgent(BaseAgent):
    name = "performance_feedback"

    def __init__(self, learning_store: ManagerLearningStore) -> None:
        self.learning_store = learning_store

    def execute(self, state: BotState, context: dict[str, Any]) -> AgentOutput:
        manager_order = _manager_order(
            context,
            self.name,
            "Assess recent outcomes and adjust sleeve biases to improve risk-adjusted return.",
        )
        snap = self.learning_store.snapshot()
        sleeve_bias = dict(snap.get("sleeve_bias") or {})
        reason_stats = dict(snap.get("reason_code_stats") or {})
        ranked = [
            (code, dict(row))
            for code, row in reason_stats.items()
            if isinstance(row, dict) and int(_safe_float(row.get("sells"), 0.0)) > 0
        ]
        ranked.sort(key=lambda x: _safe_float(x[1].get("avg_realized"), 0.0), reverse=True)
        top_reason = ranked[0][0] if ranked else ""
        worst_reason = ranked[-1][0] if ranked else ""
        summary = "bias trend={trend:.2f} scalp={scalping:.2f} def={defensive:.2f} order=active".format(
            trend=_safe_float(sleeve_bias.get("trend"), 1.0),
            scalping=_safe_float(sleeve_bias.get("scalping"), 1.0),
            defensive=_safe_float(sleeve_bias.get("defensive"), 1.0),
        )
        return AgentOutput(
            agent=self.name,
            summary=summary,
            payload={
                "manager_order": manager_order,
                "sleeve_bias": {
                    "trend": _safe_float(sleeve_bias.get("trend"), 1.0),
                    "scalping": _safe_float(sleeve_bias.get("scalping"), 1.0),
                    "defensive": _safe_float(sleeve_bias.get("defensive"), 1.0),
                },
                "top_reason_code": top_reason,
                "worst_reason_code": worst_reason,
                "reason_code_stats": reason_stats,
                "daily_outlook": dict(snap.get("daily_outlook") or {}),
                "daily_outlook_history": dict(snap.get("daily_outlook_history") or {}),
                "updated_at": str(snap.get("updated_at") or ""),
            },
        )


class MarketAnalysisAgent(BaseAgent):
    name = "market_analysis"

    def execute(self, state: BotState, context: dict[str, Any]) -> AgentOutput:
        manager_order = _manager_order(context, self.name, "Re-evaluate regime and market-flow quality.")
        regime = str(state.market_regime or "UNKNOWN")
        confidence = _safe_float(state.regime_confidence, 0.0)
        phase = str(state.session_phase or "OFF_HOURS")
        flow = str(state.market_flow_summary or "-")
        summary = f"regime={regime} conf={confidence:.2f} phase={phase}"
        return AgentOutput(
            agent=self.name,
            summary=f"{summary} order=active",
            payload={
                "manager_order": manager_order,
                "regime": regime,
                "confidence": confidence,
                "phase": phase,
                "market_flow": flow,
                "vi_summary": str(state.vi_summary or "-"),
            },
        )


class InvestmentStrategyAgent(BaseAgent):
    name = "investment_strategy"

    def execute(self, state: BotState, context: dict[str, Any]) -> AgentOutput:
        manager_order = _manager_order(
            context,
            self.name,
            "Refresh action hint for best risk-adjusted return.",
        )
        mode = str(state.trade_mode or "DRY").upper()
        profile = str(state.session_profile or "CAPITAL_PRESERVATION")
        selected_symbol = str(state.selected_symbol or "")
        selection_score = _safe_float(state.selection_score, 0.0)
        action_hint = "HOLD"
        if selected_symbol and selection_score >= 0.5 and str(state.last_action or "").upper() in {"BUY", "SELL"}:
            action_hint = str(state.last_action).upper()
        elif selected_symbol and selection_score >= 0.7:
            action_hint = "READY_TO_BUY"
        summary = f"mode={mode} profile={profile} action_hint={action_hint}"
        return AgentOutput(
            agent=self.name,
            summary=f"{summary} order=active",
            payload={
                "manager_order": manager_order,
                "trade_mode": mode,
                "session_profile": profile,
                "selected_symbol": selected_symbol,
                "selection_score": selection_score,
                "strategy_reference": str(state.strategy_reference or ""),
                "action_hint": action_hint,
            },
        )


class RiskGuardAgent(BaseAgent):
    name = "risk_guard"

    def execute(self, state: BotState, context: dict[str, Any]) -> AgentOutput:
        manager_order = _manager_order(context, self.name, "Stress-check risk controls and halt conditions.")
        heat = _safe_float(state.portfolio_heat_pct, 0.0)
        max_heat = _safe_float(state.max_portfolio_heat_pct, 0.0)
        stale = bool(state.stale_data_active)
        halt = bool(state.risk_halt_active)

        risk_level = "LOW"
        if halt or stale:
            risk_level = "CRITICAL"
        elif max_heat > 0 and heat >= max_heat * 0.9:
            risk_level = "HIGH"
        elif heat >= 50.0:
            risk_level = "MEDIUM"

        summary = f"risk={risk_level} heat={heat:.1f}% stale={stale} halt={halt}"
        return AgentOutput(
            agent=self.name,
            summary=f"{summary} order=active",
            payload={
                "manager_order": manager_order,
                "risk_level": risk_level,
                "portfolio_heat_pct": heat,
                "max_portfolio_heat_pct": max_heat,
                "stale_data_active": stale,
                "stale_data_reason": str(state.stale_data_reason or ""),
                "risk_halt_active": halt,
                "risk_halt_reason": str(state.risk_halt_reason or ""),
            },
        )


class CapitalAllocationAgent(BaseAgent):
    name = "capital_allocation"

    def execute(self, state: BotState, context: dict[str, Any]) -> AgentOutput:
        manager_order = _manager_order(
            context,
            self.name,
            "Rebalance strategy sleeves toward highest expected return under risk limits.",
        )
        by_name: dict[str, AgentOutput] = dict(context.get("by_name") or {})
        risk_payload = (by_name.get("risk_guard").payload if by_name.get("risk_guard") else {})
        market_payload = (by_name.get("market_analysis").payload if by_name.get("market_analysis") else {})
        invest_payload = (by_name.get("investment_strategy").payload if by_name.get("investment_strategy") else {})
        feedback_payload = (by_name.get("performance_feedback").payload if by_name.get("performance_feedback") else {})

        risk_level = str(risk_payload.get("risk_level", "LOW"))
        regime = str(market_payload.get("regime", "UNKNOWN"))
        confidence = _safe_float(market_payload.get("confidence", 0.0), 0.0)
        action_hint = str(invest_payload.get("action_hint", "HOLD"))

        weights = {
            "trend": 0.45,
            "scalping": 0.30,
            "defensive": 0.25,
        }

        if risk_level == "CRITICAL":
            weights = {"trend": 0.10, "scalping": 0.05, "defensive": 0.85}
        elif risk_level == "HIGH":
            weights = {"trend": 0.20, "scalping": 0.15, "defensive": 0.65}
        elif risk_level == "MEDIUM":
            weights = {"trend": 0.30, "scalping": 0.25, "defensive": 0.45}
        elif regime == "BULLISH" and confidence >= 0.6:
            weights = {"trend": 0.55, "scalping": 0.30, "defensive": 0.15}
        elif regime == "BEARISH":
            weights = {"trend": 0.15, "scalping": 0.10, "defensive": 0.75}

        if action_hint == "READY_TO_BUY":
            weights["trend"] = _clamp(weights["trend"] + 0.05, 0.0, 0.8)
            weights["defensive"] = _clamp(weights["defensive"] - 0.05, 0.0, 0.9)

        sleeve_bias = dict(feedback_payload.get("sleeve_bias") or {})
        weights["trend"] *= _safe_float(sleeve_bias.get("trend"), 1.0)
        weights["scalping"] *= _safe_float(sleeve_bias.get("scalping"), 1.0)
        weights["defensive"] *= _safe_float(sleeve_bias.get("defensive"), 1.0)

        total = sum(weights.values()) or 1.0
        for key in list(weights.keys()):
            weights[key] = round(weights[key] / total, 4)

        summary = "alloc trend={trend:.0%} scalp={scalping:.0%} def={defensive:.0%}".format(**weights)
        return AgentOutput(
            agent=self.name,
            summary=f"{summary} order=active",
            payload={
                "manager_order": manager_order,
                "weights": weights,
                "risk_level": risk_level,
                "regime": regime,
                "confidence": confidence,
                "action_hint": action_hint,
                "sleeve_bias": {
                    "trend": round(_safe_float(sleeve_bias.get("trend"), 1.0), 4),
                    "scalping": round(_safe_float(sleeve_bias.get("scalping"), 1.0), 4),
                    "defensive": round(_safe_float(sleeve_bias.get("defensive"), 1.0), 4),
                },
            },
        )


class TrendInvestAgent(BaseAgent):
    name = "invest_trend"

    def execute(self, state: BotState, context: dict[str, Any]) -> AgentOutput:
        manager_order = _manager_order(
            context,
            self.name,
            "Seek trend continuation entries with strict quality threshold.",
        )
        by_name: dict[str, AgentOutput] = dict(context.get("by_name") or {})
        alloc = (by_name.get("capital_allocation").payload if by_name.get("capital_allocation") else {})
        weights = dict(alloc.get("weights") or {})
        budget = _safe_float(weights.get("trend", 0.0), 0.0)
        score = _safe_float(state.selection_score, 0.0)
        signal = "ACCUMULATE" if (score >= 0.70 and budget >= 0.25) else "WAIT"
        summary = f"signal={signal} budget={budget:.0%} score={score:.2f}"
        return AgentOutput(
            agent=self.name,
            summary=f"{summary} order=active",
            payload={
                "manager_order": manager_order,
                "signal": signal,
                "budget_weight": budget,
                "selection_score": score,
                "symbol": str(state.selected_symbol or ""),
            },
        )


class ScalpingInvestAgent(BaseAgent):
    name = "invest_scalping"

    def execute(self, state: BotState, context: dict[str, Any]) -> AgentOutput:
        manager_order = _manager_order(
            context,
            self.name,
            "Exploit short-horizon opportunities only when risk permits.",
        )
        by_name: dict[str, AgentOutput] = dict(context.get("by_name") or {})
        alloc = (by_name.get("capital_allocation").payload if by_name.get("capital_allocation") else {})
        risk = (by_name.get("risk_guard").payload if by_name.get("risk_guard") else {})
        weights = dict(alloc.get("weights") or {})
        budget = _safe_float(weights.get("scalping", 0.0), 0.0)
        risk_level = str(risk.get("risk_level", "LOW"))
        signal = "TRADE" if (budget >= 0.2 and risk_level in {"LOW", "MEDIUM"}) else "STANDBY"
        summary = f"signal={signal} budget={budget:.0%} risk={risk_level}"
        return AgentOutput(
            agent=self.name,
            summary=f"{summary} order=active",
            payload={
                "manager_order": manager_order,
                "signal": signal,
                "budget_weight": budget,
                "risk_level": risk_level,
                "reference_action": str(state.last_action or "HOLD"),
            },
        )


class DefensiveInvestAgent(BaseAgent):
    name = "invest_defensive"

    def execute(self, state: BotState, context: dict[str, Any]) -> AgentOutput:
        manager_order = _manager_order(
            context,
            self.name,
            "Preserve capital and hedge downside during elevated risk.",
        )
        by_name: dict[str, AgentOutput] = dict(context.get("by_name") or {})
        alloc = (by_name.get("capital_allocation").payload if by_name.get("capital_allocation") else {})
        risk = (by_name.get("risk_guard").payload if by_name.get("risk_guard") else {})
        weights = dict(alloc.get("weights") or {})
        budget = _safe_float(weights.get("defensive", 0.0), 0.0)
        risk_level = str(risk.get("risk_level", "LOW"))
        posture = "HEDGE" if risk_level in {"HIGH", "CRITICAL"} else "BUFFER"
        summary = f"posture={posture} budget={budget:.0%} risk={risk_level}"
        return AgentOutput(
            agent=self.name,
            summary=f"{summary} order=active",
            payload={
                "manager_order": manager_order,
                "posture": posture,
                "budget_weight": budget,
                "risk_level": risk_level,
                "cash_balance": _safe_float(state.cash_balance, 0.0),
            },
        )


class ExecutionAgent(BaseAgent):
    name = "execution"

    def execute(self, state: BotState, context: dict[str, Any]) -> AgentOutput:
        manager_order = _manager_order(context, self.name, "Maintain runtime health and execution continuity.")
        bot_thread: threading.Thread | None = context.get("bot_thread")
        alive = bool(bot_thread and bot_thread.is_alive())
        running = bool(state.running)
        loop_count = int(state.loop_count)
        order_count = int(state.order_count)
        last_error = str(state.last_error or "")
        health = "OK" if (alive and running) else "DEGRADED"
        if last_error:
            health = "ERROR"
        summary = f"health={health} running={running} thread_alive={alive} loops={loop_count}"
        return AgentOutput(
            agent=self.name,
            summary=f"{summary} order=active",
            payload={
                "manager_order": manager_order,
                "health": health,
                "thread_alive": alive,
                "running": running,
                "loop_count": loop_count,
                "order_count": order_count,
                "last_error": last_error,
            },
        )


class ReportingAgent(BaseAgent):
    name = "reporting"

    def __init__(self, report_path: str) -> None:
        self.report_path = Path(report_path)

    def execute(self, state: BotState, context: dict[str, Any]) -> AgentOutput:
        outputs: list[AgentOutput] = list(context.get("agent_outputs") or [])
        report_kind = str(context.get("report_kind") or "hourly")
        triggers = list(context.get("triggers") or [])
        learning = dict(context.get("learning") or {})
        daily_outlook = dict(learning.get("daily_outlook") or {})
        now = datetime.now().isoformat(timespec="seconds")
        snapshot = {
            "timestamp": now,
            "report_kind": report_kind,
            "triggers": triggers,
            "state": {
                "trade_mode": state.trade_mode,
                "market_regime": state.market_regime,
                "session_phase": state.session_phase,
                "selected_symbol": state.selected_symbol,
                "last_action": state.last_action,
                "position_qty": state.position_qty,
                "equity": _safe_float(state.equity),
                "cash_balance": _safe_float(state.cash_balance),
                "total_pnl": _safe_float(state.total_pnl),
                "total_return_pct": _safe_float(state.total_return_pct),
            },
            "learning": learning,
            "agent_outputs": [
                {
                    "agent": o.agent,
                    "summary": o.summary,
                    "payload": o.payload,
                }
                for o in outputs
            ],
        }
        self._append_report(snapshot)

        one_line = " | ".join(f"{o.agent}:{o.summary}" for o in outputs)
        trigger_part = f" triggers={','.join(triggers)}" if triggers else ""
        outlook_label = str(daily_outlook.get("label") or "INSUFFICIENT_DATA")
        outlook_rate = _safe_float(daily_outlook.get("expected_daily_profit_rate_pct"), 0.0)
        quality_score = _safe_float(daily_outlook.get("market_quality_score"), 0.0)
        summary = (
            f"manager_report kind={report_kind}{trigger_part} ts={now} "
            f"symbol={state.selected_symbol or '-'} outlook={outlook_label} exp_day={outlook_rate:+.2f}% quality={quality_score:.1f} {one_line}"
        ).strip()
        return AgentOutput(agent=self.name, summary=summary, payload=snapshot)

    def _append_report(self, snapshot: dict[str, Any]) -> None:
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        existing: list[dict[str, Any]] = []
        try:
            if self.report_path.exists():
                parsed = json.loads(self.report_path.read_text(encoding="utf-8"))
                if isinstance(parsed, list):
                    existing = parsed[-500:]
        except Exception:
            existing = []
        existing.append(snapshot)
        self.report_path.write_text(json.dumps(existing[-500:], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class OrderPolicyAgent(BaseAgent):
    name = "order_policy"

    def execute(self, state: BotState, context: dict[str, Any]) -> AgentOutput:
        manager_order = _manager_order(
            context,
            self.name,
            "Gate new orders to maximize risk-adjusted profit and avoid bad entries.",
        )
        by_name: dict[str, AgentOutput] = dict(context.get("by_name") or {})
        risk_payload = (by_name.get("risk_guard").payload if by_name.get("risk_guard") else {})
        alloc_payload = (by_name.get("capital_allocation").payload if by_name.get("capital_allocation") else {})
        trend_payload = (by_name.get("invest_trend").payload if by_name.get("invest_trend") else {})
        scalping_payload = (by_name.get("invest_scalping").payload if by_name.get("invest_scalping") else {})
        defensive_payload = (by_name.get("invest_defensive").payload if by_name.get("invest_defensive") else {})

        risk_level = str(risk_payload.get("risk_level", "LOW"))
        alloc_weights = dict(alloc_payload.get("weights") or {})
        trend_signal = str(trend_payload.get("signal", "WAIT"))
        scalping_signal = str(scalping_payload.get("signal", "STANDBY"))
        defensive_posture = str(defensive_payload.get("posture", "BUFFER"))

        allow = True
        order_limit_factor = 1.0
        reason = "consensus_ok"

        if risk_level == "CRITICAL":
            allow = False
            order_limit_factor = 0.0
            reason = "risk_critical"
        elif risk_level == "HIGH":
            allow = True
            order_limit_factor = 0.4
            reason = "risk_high_reduce"
        elif trend_signal == "WAIT" and scalping_signal == "STANDBY":
            allow = False
            order_limit_factor = 0.0
            reason = "no_entry_consensus"
        elif defensive_posture == "HEDGE" and _safe_float(alloc_weights.get("defensive", 0.0), 0.0) >= 0.7:
            allow = True
            order_limit_factor = 0.3
            reason = "defensive_bias"

        policy = "ALLOW" if allow else "BLOCK"
        summary = f"policy={policy} limit_factor={order_limit_factor:.2f} reason={reason}"
        return AgentOutput(
            agent=self.name,
            summary=f"{summary} order=active",
            payload={
                "manager_order": manager_order,
                "policy": policy,
                "allow_new_orders": allow,
                "order_limit_factor": order_limit_factor,
                "reason": reason,
                "risk_level": risk_level,
            },
        )


class ManagerSlackNotifier:
    def __init__(self, enabled: bool, webhook_url: str, timeout_sec: float = 6.0) -> None:
        self.enabled = bool(enabled)
        self.webhook_url = str(webhook_url or "").strip()
        self.timeout_sec = max(2.0, float(timeout_sec))

    def send_report(self, report_output: AgentOutput) -> None:
        if not self.enabled:
            return
        if not self.webhook_url:
            logging.warning("MANAGER_SLACK disabled: missing webhook URL")
            return

        payload = report_output.payload if isinstance(report_output.payload, dict) else {}
        state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
        learning = payload.get("learning") if isinstance(payload.get("learning"), dict) else {}
        outlook = learning.get("daily_outlook") if isinstance(learning.get("daily_outlook"), dict) else {}
        symbol = str(state.get("selected_symbol") or "-")
        regime = str(state.get("market_regime") or "UNKNOWN")
        ret = _safe_float(state.get("total_return_pct"), 0.0)
        exp_rate = _safe_float(outlook.get("expected_daily_profit_rate_pct"), 0.0)
        quality = _safe_float(outlook.get("market_quality_score"), 0.0)
        label = str(outlook.get("label") or "INSUFFICIENT_DATA")

        kind = str(payload.get("report_kind") or "hourly").upper()
        text = (
            f"[Manager {kind}] symbol={symbol} regime={regime} return={ret:.2f}% "
            f"outlook={label} exp_day={exp_rate:+.2f}% quality={quality:.1f}\n{report_output.summary}"
        )
        try:
            resp = requests.post(
                self.webhook_url,
                json={"text": text},
                timeout=self.timeout_sec,
            )
            if resp.status_code >= 300:
                logging.warning("MANAGER_SLACK failed status=%s body=%s", resp.status_code, (resp.text or "")[:180])
        except Exception as exc:
            logging.warning("MANAGER_SLACK error: %s", exc)


class ManagerAgent:
    def __init__(
        self,
        *,
        report_interval_seconds: int = 3600,
        cycle_seconds: int = 20,
        report_path: str = "data/hourly_manager_reports.json",
        slack_enabled: bool = False,
        slack_webhook_url: str = "",
        event_report_cooldown_seconds: int = 120,
    ) -> None:
        self.report_interval_seconds = max(60, int(report_interval_seconds))
        self.cycle_seconds = max(5, int(cycle_seconds))
        self.learning_store = ManagerLearningStore()
        self.core_agents: list[BaseAgent] = [
            MarketAnalysisAgent(),
            InvestmentStrategyAgent(),
            RiskGuardAgent(),
            ExecutionAgent(),
        ]
        self.feedback_agent = PerformanceFeedbackAgent(self.learning_store)
        self.capital_agent = CapitalAllocationAgent()
        self.invest_agents: list[BaseAgent] = [
            TrendInvestAgent(),
            ScalpingInvestAgent(),
            DefensiveInvestAgent(),
        ]
        self.order_policy_agent = OrderPolicyAgent()
        self.reporting_agent = ReportingAgent(report_path=report_path)
        self.slack_notifier = ManagerSlackNotifier(enabled=slack_enabled, webhook_url=slack_webhook_url)
        self.event_report_cooldown_seconds = max(10, int(event_report_cooldown_seconds))

    @staticmethod
    def _build_work_orders(
        state: BotState,
        prev_vector: dict[str, Any],
        triggers: list[str],
        latest_learning: dict[str, Any],
    ) -> dict[str, str]:
        urgency = "normal"
        risk_level = str(prev_vector.get("risk_level") or "LOW")
        if risk_level in {"HIGH", "CRITICAL"} or ("risk_change" in triggers):
            urgency = "high"
        if "policy_change" in triggers:
            urgency = "high"

        phase_map = {
            "OPENING_FOCUS": "opening",
            "REGULAR_SESSION": "regular",
            "AFTER_MARKET": "after",
        }
        current_bucket = phase_map.get(str(state.session_phase or "").upper(), "off")
        bucket_stats = dict(latest_learning.get("time_bucket_stats") or {})
        cur_bucket_stats = dict(bucket_stats.get(current_bucket) or {})
        bucket_realized = _safe_float(cur_bucket_stats.get("realized"), 0.0)
        bucket_sells = int(cur_bucket_stats.get("sells") or 0)
        if bucket_sells >= 3 and bucket_realized < 0:
            urgency = "high"

        reason_stats = dict(latest_learning.get("reason_code_stats") or {})
        worst_reason = ""
        worst_avg = 0.0
        for reason, row in reason_stats.items():
            if not isinstance(row, dict):
                continue
            sells = int(_safe_float(row.get("sells"), 0.0))
            avg_realized = _safe_float(row.get("blended_expectancy"), _safe_float(row.get("avg_realized"), 0.0))
            if sells < 3:
                continue
            if (worst_reason == "") or (avg_realized < worst_avg):
                worst_reason = str(reason)
                worst_avg = avg_realized
        if worst_reason and worst_avg < 0:
            urgency = "high"

        bucket_hint = f"bucket={current_bucket} realized={bucket_realized:+.0f} sells={bucket_sells}"
        reason_hint = f"worst_reason={worst_reason}:{worst_avg:+.1f}" if worst_reason else "worst_reason=NA"

        prefix = f"[{urgency}]"
        symbol = str(state.selected_symbol or "current_target")
        return {
            "market_analysis": f"{prefix} Refresh regime/flow for {symbol} and detect edge shifts. {bucket_hint} {reason_hint}",
            "investment_strategy": f"{prefix} Update action hint targeting higher expected return with controlled risk. {bucket_hint} {reason_hint}",
            "risk_guard": f"{prefix} Re-check heat, stale data, and halt guards before next decisions. {bucket_hint} {reason_hint}",
            "execution": f"{prefix} Keep runtime healthy and report execution degradation immediately.",
            "performance_feedback": f"{prefix} Learn from recent return/risk outcomes and tune sleeve biases. {bucket_hint} {reason_hint}",
            "capital_allocation": f"{prefix} Rebalance trend/scalping/defensive sleeves for risk-adjusted performance. {bucket_hint} {reason_hint}",
            "invest_trend": f"{prefix} Focus on quality trend setups; avoid weak momentum entries. {bucket_hint} {reason_hint}",
            "invest_scalping": f"{prefix} Hunt short-horizon trades only when spread/risk profile is acceptable. {bucket_hint} {reason_hint}",
            "invest_defensive": f"{prefix} Preserve capital and maintain downside buffer during stress. {bucket_hint} {reason_hint}",
            "order_policy": f"{prefix} Enforce ALLOW/BLOCK gate to avoid low-conviction or high-risk orders. {bucket_hint} {reason_hint}",
        }

    @staticmethod
    def _extract_event_vector(state: BotState, by_name: dict[str, AgentOutput]) -> dict[str, Any]:
        market = by_name.get("market_analysis").payload if by_name.get("market_analysis") else {}
        invest = by_name.get("investment_strategy").payload if by_name.get("investment_strategy") else {}
        risk = by_name.get("risk_guard").payload if by_name.get("risk_guard") else {}
        alloc = by_name.get("capital_allocation").payload if by_name.get("capital_allocation") else {}
        policy = by_name.get("order_policy").payload if by_name.get("order_policy") else {}
        weights = dict(alloc.get("weights") or {})
        return {
            "symbol": str(state.selected_symbol or ""),
            "regime": str(market.get("regime") or "UNKNOWN"),
            "phase": str(market.get("phase") or "OFF_HOURS"),
            "risk_level": str(risk.get("risk_level") or "LOW"),
            "policy": str(policy.get("policy") or "ALLOW"),
            "policy_reason": str(policy.get("reason") or ""),
            "action_hint": str(invest.get("action_hint") or "HOLD"),
            "trend_w": round(_safe_float(weights.get("trend"), 0.0), 2),
            "scalp_w": round(_safe_float(weights.get("scalping"), 0.0), 2),
            "def_w": round(_safe_float(weights.get("defensive"), 0.0), 2),
        }

    @staticmethod
    def _diff_triggers(prev_vector: dict[str, Any], curr_vector: dict[str, Any]) -> list[str]:
        if not prev_vector:
            return ["startup"]
        mapping = {
            "symbol": "symbol_change",
            "regime": "regime_change",
            "phase": "phase_change",
            "risk_level": "risk_change",
            "policy": "policy_change",
            "policy_reason": "policy_reason_change",
            "action_hint": "action_hint_change",
            "trend_w": "allocation_change",
            "scalp_w": "allocation_change",
            "def_w": "allocation_change",
        }
        triggers: list[str] = []
        for key, trigger in mapping.items():
            if prev_vector.get(key) != curr_vector.get(key) and trigger not in triggers:
                triggers.append(trigger)
        return triggers

    def run(self, stop_event: threading.Event, state: BotState, bot_thread: threading.Thread) -> None:
        next_report_at = time.time()
        last_event_report_at = 0.0
        prev_vector: dict[str, Any] = {}
        latest_learning: dict[str, Any] = {}
        logging.info(
            "MANAGER_AGENT online cycle=%ss report_interval=%ss",
            self.cycle_seconds,
            self.report_interval_seconds,
        )

        while not stop_event.is_set():
            outputs: list[AgentOutput] = []
            by_name: dict[str, AgentOutput] = {}
            warmup_triggers = ["startup"] if not prev_vector else []
            work_orders = self._build_work_orders(state, prev_vector, warmup_triggers, latest_learning)
            base_context: dict[str, Any] = {
                "bot_thread": bot_thread,
                "by_name": by_name,
                "work_orders": work_orders,
            }

            for agent in self.core_agents:
                try:
                    output = agent.execute(state, base_context)
                except Exception as exc:
                    output = AgentOutput(
                        agent=agent.name,
                        summary=f"error={exc}",
                        payload={"error": str(exc)},
                    )
                outputs.append(output)
                by_name[output.agent] = output

            try:
                feedback_output = self.feedback_agent.execute(
                    state,
                    {"by_name": by_name, "bot_thread": bot_thread, "work_orders": work_orders},
                )
            except Exception as exc:
                feedback_output = AgentOutput(
                    agent=self.feedback_agent.name,
                    summary=f"error={exc}",
                    payload={"error": str(exc)},
                )
            outputs.append(feedback_output)
            by_name[feedback_output.agent] = feedback_output

            try:
                alloc_output = self.capital_agent.execute(
                    state,
                    {"by_name": by_name, "bot_thread": bot_thread, "work_orders": work_orders},
                )
            except Exception as exc:
                alloc_output = AgentOutput(
                    agent=self.capital_agent.name,
                    summary=f"error={exc}",
                    payload={"error": str(exc)},
                )
            outputs.append(alloc_output)
            by_name[alloc_output.agent] = alloc_output

            invest_context: dict[str, Any] = {
                "bot_thread": bot_thread,
                "by_name": by_name,
                "work_orders": work_orders,
            }
            for agent in self.invest_agents:
                try:
                    output = agent.execute(state, invest_context)
                except Exception as exc:
                    output = AgentOutput(
                        agent=agent.name,
                        summary=f"error={exc}",
                        payload={"error": str(exc)},
                    )
                outputs.append(output)
                by_name[output.agent] = output

            try:
                policy_output = self.order_policy_agent.execute(
                    state,
                    {"by_name": by_name, "bot_thread": bot_thread, "work_orders": work_orders},
                )
            except Exception as exc:
                policy_output = AgentOutput(
                    agent=self.order_policy_agent.name,
                    summary=f"error={exc}",
                    payload={"error": str(exc)},
                )
            outputs.append(policy_output)
            by_name[policy_output.agent] = policy_output

            learn_result = self.learning_store.update_from_cycle(state, by_name)
            latest_learning = learn_result
            if learn_result.get("reasons"):
                logging.info(
                    "MANAGER_LEARN delta=%.4f realized_delta=%.2f sleeve_delta=%s reason_signal=%s new_sells=%s fills(b=%s,s=%s) reasons=%s bias=%s",
                    _safe_float(learn_result.get("pnl_delta_pct"), 0.0),
                    _safe_float(learn_result.get("realized_delta"), 0.0),
                    learn_result.get("sleeve_realized_delta"),
                    learn_result.get("reason_signal_summary"),
                    int(learn_result.get("new_sell_trades") or 0),
                    int(learn_result.get("buy_fills") or 0),
                    int(learn_result.get("sell_fills") or 0),
                    ",".join(list(learn_result.get("reasons") or [])),
                    learn_result.get("sleeve_bias"),
                )

            curr_vector = self._extract_event_vector(state, by_name)
            triggers = self._diff_triggers(prev_vector, curr_vector)
            if triggers:
                logging.info("MANAGER_EVENT triggers=%s vector=%s", ",".join(triggers), curr_vector)
                if (time.time() - last_event_report_at) >= self.event_report_cooldown_seconds:
                    event_report = self.reporting_agent.execute(
                        state,
                        {
                            "agent_outputs": outputs,
                            "report_kind": "event",
                            "triggers": triggers,
                            "learning": latest_learning,
                        },
                    )
                    self.slack_notifier.send_report(event_report)
                    last_event_report_at = time.time()
            prev_vector = curr_vector

            if time.time() >= next_report_at:
                report_output = self.reporting_agent.execute(
                    state,
                    {
                        "agent_outputs": outputs,
                        "report_kind": "hourly",
                        "triggers": [],
                        "learning": latest_learning,
                    },
                )
                logging.info("MANAGER_HOURLY_REPORT %s", report_output.summary)
                self.slack_notifier.send_report(report_output)
                next_report_at = time.time() + self.report_interval_seconds

            if (not bot_thread.is_alive()) and state.last_error:
                logging.error("MANAGER_DETECTED_STOP last_error=%s", state.last_error)
                break

            stop_event.wait(timeout=self.cycle_seconds)


def run_ai_company(
    stop_event: threading.Event,
    state: BotState,
    *,
    report_interval_seconds: int = 3600,
    cycle_seconds: int = 20,
    report_path: str = "data/hourly_manager_reports.json",
    manager_slack_enabled: bool = False,
    manager_slack_webhook_url: str = "",
    event_report_cooldown_seconds: int = 120,
) -> None:
    bot_thread = threading.Thread(target=run_bot, args=(stop_event, state), daemon=True, name="bot-runtime")
    bot_thread.start()

    manager = ManagerAgent(
        report_interval_seconds=report_interval_seconds,
        cycle_seconds=cycle_seconds,
        report_path=report_path,
        slack_enabled=manager_slack_enabled,
        slack_webhook_url=manager_slack_webhook_url,
        event_report_cooldown_seconds=event_report_cooldown_seconds,
    )
    manager.run(stop_event, state, bot_thread)

    stop_event.set()
    bot_thread.join(timeout=5)
