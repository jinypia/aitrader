from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from bot_runtime import BotState, run_bot


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


@dataclass
class AgentOutput:
    agent: str
    summary: str
    payload: dict[str, Any]


class BaseAgent:
    name = "base"

    def execute(self, state: BotState, context: dict[str, Any]) -> AgentOutput:
        raise NotImplementedError


class MarketAnalysisAgent(BaseAgent):
    name = "market_analysis"

    def execute(self, state: BotState, context: dict[str, Any]) -> AgentOutput:
        regime = str(state.market_regime or "UNKNOWN")
        confidence = _safe_float(state.regime_confidence, 0.0)
        phase = str(state.session_phase or "OFF_HOURS")
        flow = str(state.market_flow_summary or "-")
        summary = f"regime={regime} conf={confidence:.2f} phase={phase}"
        return AgentOutput(
            agent=self.name,
            summary=summary,
            payload={
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
            summary=summary,
            payload={
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
            summary=summary,
            payload={
                "risk_level": risk_level,
                "portfolio_heat_pct": heat,
                "max_portfolio_heat_pct": max_heat,
                "stale_data_active": stale,
                "stale_data_reason": str(state.stale_data_reason or ""),
                "risk_halt_active": halt,
                "risk_halt_reason": str(state.risk_halt_reason or ""),
            },
        )


class ExecutionAgent(BaseAgent):
    name = "execution"

    def execute(self, state: BotState, context: dict[str, Any]) -> AgentOutput:
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
            summary=summary,
            payload={
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
        now = datetime.now().isoformat(timespec="seconds")
        snapshot = {
            "timestamp": now,
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
        summary = f"manager_report ts={now} symbol={state.selected_symbol or '-'} {one_line}".strip()
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


class ManagerAgent:
    def __init__(
        self,
        *,
        report_interval_seconds: int = 3600,
        cycle_seconds: int = 20,
        report_path: str = "data/hourly_manager_reports.json",
    ) -> None:
        self.report_interval_seconds = max(60, int(report_interval_seconds))
        self.cycle_seconds = max(5, int(cycle_seconds))
        self.agents: list[BaseAgent] = [
            MarketAnalysisAgent(),
            InvestmentStrategyAgent(),
            RiskGuardAgent(),
            ExecutionAgent(),
        ]
        self.reporting_agent = ReportingAgent(report_path=report_path)

    def run(self, stop_event: threading.Event, state: BotState, bot_thread: threading.Thread) -> None:
        next_report_at = time.time()
        logging.info(
            "MANAGER_AGENT online cycle=%ss report_interval=%ss",
            self.cycle_seconds,
            self.report_interval_seconds,
        )

        while not stop_event.is_set():
            outputs: list[AgentOutput] = []
            context: dict[str, Any] = {"bot_thread": bot_thread}
            for agent in self.agents:
                try:
                    output = agent.execute(state, context)
                except Exception as exc:
                    output = AgentOutput(
                        agent=agent.name,
                        summary=f"error={exc}",
                        payload={"error": str(exc)},
                    )
                outputs.append(output)

            if time.time() >= next_report_at:
                report_output = self.reporting_agent.execute(state, {"agent_outputs": outputs})
                logging.info("MANAGER_HOURLY_REPORT %s", report_output.summary)
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
) -> None:
    bot_thread = threading.Thread(target=run_bot, args=(stop_event, state), daemon=True, name="bot-runtime")
    bot_thread.start()

    manager = ManagerAgent(
        report_interval_seconds=report_interval_seconds,
        cycle_seconds=cycle_seconds,
        report_path=report_path,
    )
    manager.run(stop_event, state, bot_thread)

    stop_event.set()
    bot_thread.join(timeout=5)
