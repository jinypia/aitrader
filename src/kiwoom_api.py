from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dt_time
from typing import Any
from zoneinfo import ZoneInfo

import requests
import os
import re
import time

from config import Settings


@dataclass
class TokenInfo:
    access_token: str
    expires_dt: str | None


class KiwoomAPI:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = str(settings.base_url).rstrip("/")
        self.session = requests.Session()
        self.token_info: TokenInfo | None = None
        self._daily_bars_cache: dict[tuple[str, int], tuple[float, list[dict[str, float]]]] = {}
        self._intraday_bars_cache: dict[tuple[str, int, int], tuple[float, list[dict[str, float]]]] = {}

    def login(self) -> TokenInfo:
        base_urls = [self.base_url]
        # Fallbacks for environments where mock/live DNS may intermittently fail.
        current = self.base_url.lower()
        if "mockapi.kiwoom.com" in current:
            base_urls.append("https://api.kiwoom.com")
        elif "api.kiwoom.com" in current:
            base_urls.append("https://mockapi.kiwoom.com")
        # Keep ordering stable while de-duplicating.
        base_urls = list(dict.fromkeys([str(x).rstrip("/") for x in base_urls if str(x).strip()]))
        payload = {
            "grant_type": "client_credentials",
            "appkey": self.settings.app_key,
            "secretkey": self.settings.secret_key,
        }
        max_retry = int(os.getenv("KIWOOM_429_RETRY", "2"))
        backoff = float(os.getenv("KIWOOM_429_BACKOFF_SEC", "0.6"))
        errors: list[str] = []
        for base_url in base_urls:
            url = f"{base_url}/oauth2/token"
            response = None
            try:
                for attempt in range(max_retry + 1):
                    response = self.session.post(url, json=payload, timeout=10)
                    if response.status_code != 429:
                        break
                    if attempt < max_retry:
                        time.sleep(backoff * (2**attempt))
                assert response is not None
                response.raise_for_status()
                try:
                    data = response.json()
                except Exception as exc:
                    body = (response.text or "").strip()
                    content_type = str(response.headers.get("content-type") or "").strip()
                    raise RuntimeError(
                        "Kiwoom login returned a non-JSON response. "
                        f"status={response.status_code} content_type={content_type or '-'} "
                        f"body={(body[:300] if body else '<empty>')}"
                    ) from exc

                token = data.get("token") or data.get("access_token")
                if not token:
                    raise RuntimeError(f"Token missing in login response: {data}")

                # Stick to the endpoint that successfully authenticated.
                self.base_url = base_url
                self.token_info = TokenInfo(
                    access_token=token,
                    expires_dt=data.get("expires_dt") or data.get("expires_in"),
                )
                return self.token_info
            except Exception as exc:
                errors.append(f"{base_url}: {exc}")
                continue
        raise RuntimeError(
            "Kiwoom login failed for all candidate endpoints. "
            + " | ".join(errors[-3:])
        )

    @staticmethod
    def _token_invalid_payload(data: Any) -> bool:
        if not isinstance(data, dict):
            return False
        code = str(data.get("return_code", "")).strip()
        msg = str(
            data.get("return_msg")
            or data.get("message")
            or data.get("msg")
            or ""
        )
        if code in {"3", "03"}:
            return True
        msg_low = msg.lower()
        return ("token" in msg_low) and ("invalid" in msg_low or "유효하지" in msg)

    def _auth_headers(self) -> dict[str, str]:
        if not self.token_info:
            self.login()
        assert self.token_info is not None
        return {"Authorization": f"Bearer {self.token_info.access_token}"}

    @staticmethod
    def _to_float(raw: object) -> float:
        text = str(raw or "").replace(",", "").replace("+", "").strip()
        if not text:
            return 0.0
        try:
            return float(text)
        except Exception:
            return 0.0

    @staticmethod
    def _to_int(raw: object) -> int:
        text = str(raw or "").replace(",", "").replace("+", "").strip()
        if not text:
            return 0
        try:
            return int(float(text))
        except Exception:
            return 0

    @staticmethod
    def _walk_nodes(raw: Any):
        yield raw
        if isinstance(raw, dict):
            for value in raw.values():
                yield from KiwoomAPI._walk_nodes(value)
        elif isinstance(raw, list):
            for value in raw:
                yield from KiwoomAPI._walk_nodes(value)

    @staticmethod
    def _first_number(data: Any, keys: tuple[str, ...], *, absolute: bool = False) -> float:
        lower_keys = tuple(k.lower() for k in keys)
        for node in KiwoomAPI._walk_nodes(data):
            if not isinstance(node, dict):
                continue
            for key, value in node.items():
                key_low = str(key).lower()
                if key_low in lower_keys:
                    num = KiwoomAPI._to_float_abs(value) if absolute else KiwoomAPI._to_float(value)
                    if abs(num) > 1e-12:
                        return float(num)
        return 0.0

    @staticmethod
    def _extract_account_numbers(data: Any) -> list[str]:
        out: list[str] = []
        for node in KiwoomAPI._walk_nodes(data):
            if isinstance(node, dict):
                for key, value in node.items():
                    key_low = str(key).lower()
                    if "acct" not in key_low:
                        continue
                    if isinstance(value, list):
                        for item in value:
                            text = str(item or "").strip()
                            if text.isdigit():
                                out.append(text)
                    else:
                        for part in re.split(r"[\s,;/]+", str(value or "").strip()):
                            if part.isdigit():
                                out.append(part)
        return list(dict.fromkeys(x for x in out if x))

    def _resolve_account_no(self, account_no: str = "") -> str:
        acct = str(account_no or self.settings.account_no or "").strip()
        if acct:
            return acct
        acct_list = self.get_account_numbers()
        if acct_list:
            return str(acct_list[0])
        raise RuntimeError("No Kiwoom account number available.")

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        max_retry = int(os.getenv("KIWOOM_429_RETRY", "2"))
        backoff = float(os.getenv("KIWOOM_429_BACKOFF_SEC", "0.6"))
        timeout_retry = int(os.getenv("KIWOOM_TIMEOUT_RETRY", "1"))

        for token_retry in range(2):
            req_headers = self._auth_headers()
            if headers:
                req_headers.update(headers)

            response = None
            for attempt in range(max_retry + 1):
                try:
                    response = self.session.request(
                        method=method.upper(),
                        url=url,
                        params=params,
                        json=json_body,
                        headers=req_headers,
                        timeout=10,
                    )
                except requests.Timeout:
                    if attempt < max(timeout_retry, max_retry):
                        time.sleep(backoff * (2**attempt))
                        continue
                    raise

                if response.status_code != 429:
                    break
                if attempt < max_retry:
                    time.sleep(backoff * (2**attempt))

            assert response is not None

            payload = None
            try:
                payload = response.json()
            except Exception:
                payload = None

            if self._token_invalid_payload(payload):
                # Token expired/invalid in API payload. Re-login once and retry.
                self.login()
                continue

            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                body = response.text[:500]
                raise requests.HTTPError(
                    f"{exc}; url={url}; status={response.status_code}; body={body}"
                ) from exc

            if isinstance(payload, dict):
                return payload
            return response.json()

        raise RuntimeError("Request failed after token refresh retry.")

    @staticmethod
    def _to_float_abs(raw: object) -> float:
        text = str(raw or "").replace(",", "").replace("+", "").strip()
        if not text:
            return 0.0
        sign = -1.0 if text.startswith("-") else 1.0
        text = text.lstrip("-")
        try:
            return abs(float(text) * sign)
        except Exception:
            return 0.0

    @staticmethod
    def _to_int_abs(raw: object) -> int:
        text = str(raw or "").replace(",", "").replace("+", "").strip()
        if not text:
            return 0
        text = text.lstrip("-")
        try:
            return abs(int(float(text)))
        except Exception:
            return 0

    def get_quote(self, symbol: str) -> dict[str, float | int]:
        if not self.settings.price_path:
            raise RuntimeError("PRICE_PATH is empty. Fill it from Kiwoom REST guide first.")
        headers = {"api-id": self.settings.price_api_id}
        data = self.request(
            "POST",
            self.settings.price_path,
            json_body={"stk_cd": symbol},
            headers=headers,
        )
        raw = data
        for key in self.settings.price_field.split("."):
            if not isinstance(raw, dict) or key not in raw:
                raise RuntimeError(
                    f"PRICE_FIELD path '{self.settings.price_field}' not found in response: {data}"
                )
            raw = raw[key]
        price = self._to_float_abs(raw)
        vol_keys = ("trde_qty", "acml_vol", "vol", "trade_qty", "deal_qty")
        volume = 0
        if isinstance(data, dict):
            for k in vol_keys:
                if k in data:
                    volume = self._to_int_abs(data.get(k))
                    if volume > 0:
                        break
            if volume <= 0:
                for _, v in data.items():
                    if isinstance(v, dict):
                        for k in vol_keys:
                            if k in v:
                                volume = self._to_int_abs(v.get(k))
                                if volume > 0:
                                    break
                        if volume > 0:
                            break
        return {"price": price, "volume": int(max(0, volume))}

    def get_last_price(self, symbol: str) -> float:
        q = self.get_quote(symbol)
        return float(q.get("price", 0.0))

    def get_account_numbers(self) -> list[str]:
        headers = {"api-id": self.settings.account_lookup_api_id}
        data = self.request(
            "POST",
            self.settings.account_api_path,
            json_body={},
            headers=headers,
        )
        return self._extract_account_numbers(data)

    def get_account_cash(self, account_no: str = "") -> dict[str, Any]:
        acct = self._resolve_account_no(account_no)
        headers = {"api-id": self.settings.account_cash_api_id}
        payload = {"acctNo": acct, "qry_tp": self.settings.account_query_type}
        data = self.request(
            "POST",
            self.settings.account_api_path,
            json_body=payload,
            headers=headers,
        )
        return data

    def get_account_holdings(self, account_no: str = "") -> dict[str, Any]:
        acct = self._resolve_account_no(account_no)
        headers = {"api-id": self.settings.account_holdings_api_id}
        payload = {"acctNo": acct, "qry_tp": self.settings.account_query_type}
        data = self.request(
            "POST",
            self.settings.account_api_path,
            json_body=payload,
            headers=headers,
        )
        return data

    def get_account_snapshot(self, account_no: str = "") -> dict[str, Any]:
        acct = self._resolve_account_no(account_no)
        source = "mock" if "mockapi" in self.base_url.lower() else "live"
        cash_payload: dict[str, Any] = {}
        holdings_payload: dict[str, Any] = {}
        cash_error = ""
        holdings_error = ""
        try:
            cash_payload = self.get_account_cash(acct)
        except Exception as exc:
            cash_error = str(exc)
        try:
            holdings_payload = self.get_account_holdings(acct)
        except Exception as exc:
            holdings_error = str(exc)
        if isinstance(cash_payload, dict):
            return_code = int(self._to_int(cash_payload.get("return_code")))
            if return_code != 0 and not cash_error:
                cash_error = str(cash_payload.get("return_msg") or f"return_code={return_code}")
        if isinstance(holdings_payload, dict):
            return_code = int(self._to_int(holdings_payload.get("return_code")))
            if return_code != 0 and not holdings_error:
                holdings_error = str(holdings_payload.get("return_msg") or f"return_code={return_code}")

        positions: list[dict[str, Any]] = []
        for node in self._walk_nodes(holdings_payload):
            if not isinstance(node, dict):
                continue
            symbol = str(
                node.get("stk_cd")
                or node.get("jongmok_cd")
                or node.get("item_cd")
                or node.get("symbol")
                or ""
            ).strip()
            if not symbol:
                continue
            qty = 0
            for key in ("rmnd_qty", "hldg_qty", "qty", "jan_qty", "own_qty", "bal_qty"):
                if key in node:
                    qty = max(qty, self._to_int_abs(node.get(key)))
            if qty <= 0:
                continue
            avg_price = 0.0
            for key in ("pchs_avg_pric", "avg_pric", "buy_pric", "pchs_pric", "avg_price"):
                if key in node:
                    avg_price = max(avg_price, self._to_float_abs(node.get(key)))
            market_value = 0.0
            for key in ("evlt_amt", "eval_amt", "mkt_val", "balance_amt"):
                if key in node:
                    market_value = max(market_value, self._to_float_abs(node.get(key)))
            unrealized = 0.0
            for key in ("evlt_pfls", "evlt_pfls_amt", "unrealized_pnl", "eval_pnl"):
                if key in node:
                    unrealized = self._to_float(node.get(key))
                    break
            return_pct = 0.0
            for key in ("prft_rt", "evltv_prft_rt", "yield_rt", "return_pct"):
                if key in node:
                    return_pct = self._to_float(node.get(key))
                    break
            positions.append(
                {
                    "symbol": symbol,
                    "name": str(node.get("stk_nm") or node.get("stk_name") or node.get("name") or "").strip(),
                    "qty": qty,
                    "avg_price": avg_price,
                    "market_value": market_value,
                    "unrealized_pnl": unrealized,
                    "return_pct": return_pct,
                }
            )
        dedup_positions = list({row["symbol"]: row for row in positions}.values())
        dedup_positions.sort(key=lambda row: (int(row.get("qty", 0)), str(row.get("symbol", ""))), reverse=True)

        cash_balance = self._first_number(
            cash_payload,
            (
                "ord_psbl_cash",
                "ord_psbl_amt",
                "ord_alow_amt",
                "dnca_tot_amt",
                "cash_balance",
                "cash",
                "tot_dnca",
                "entr",
                "d1_entra",
                "d2_entra",
                "pymn_alow_amt",
            ),
            absolute=True,
        )
        equity = self._first_number(
            holdings_payload,
            ("tot_evlt_amt", "tot_asst_amt", "est_asst", "equity", "eval_total_amt"),
            absolute=True,
        )
        total_pnl = self._first_number(
            holdings_payload,
            ("tot_evlt_pfls_amt", "tot_pfls", "total_pnl", "eval_total_pnl"),
            absolute=False,
        )
        total_return_pct = self._first_number(
            holdings_payload,
            ("tot_prft_rt", "tot_evltv_prft_rt", "total_return_pct", "return_pct"),
            absolute=False,
        )
        unrealized_pnl = self._first_number(
            holdings_payload,
            ("evlt_pfls_amt", "evlt_pfls", "unrealized_pnl", "eval_pnl"),
            absolute=False,
        )
        realized_pnl = self._first_number(
            holdings_payload,
            ("rlzt_pfls", "realized_pnl", "tot_rlzt_pfls"),
            absolute=False,
        )
        if abs(equity) < 1e-12 and dedup_positions:
            equity = cash_balance + sum(max(0.0, float(row.get("market_value", 0.0))) for row in dedup_positions)
        if abs(equity) < 1e-12 and abs(cash_balance) > 1e-12:
            equity = cash_balance
        if abs(total_pnl) < 1e-12 and dedup_positions:
            total_pnl = sum(float(row.get("unrealized_pnl", 0.0)) for row in dedup_positions) + realized_pnl
        positions_summary = ", ".join(
            f"{str(row.get('symbol'))}:{int(row.get('qty', 0))}@{float(row.get('avg_price', 0.0)):.1f}"
            for row in dedup_positions
            if int(row.get("qty", 0)) > 0
        )
        snapshot = {
            "account_no": acct,
            "source": source,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "cash_balance": cash_balance,
            "equity": equity,
            "unrealized_pnl": unrealized_pnl,
            "realized_pnl": realized_pnl,
            "total_pnl": total_pnl,
            "total_return_pct": total_return_pct,
            "active_positions": len(dedup_positions),
            "position_qty": sum(int(row.get("qty", 0)) for row in dedup_positions),
            "position_symbol": str(dedup_positions[0].get("symbol", "")) if dedup_positions else "",
            "positions_summary": positions_summary,
            "positions": dedup_positions,
            "cash_error": cash_error,
            "holdings_error": holdings_error,
        }
        return snapshot

    def place_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: int,
    ) -> dict[str, Any]:
        if not self.settings.order_path:
            raise RuntimeError("ORDER_PATH is empty. Fill it from Kiwoom REST guide first.")

        api_id = (
            self.settings.order_buy_api_id
            if side.upper() == "BUY"
            else self.settings.order_sell_api_id
        )

        payload = {
            "dmst_stex_tp": self.settings.dmst_stex_tp,
            "stk_cd": symbol,
            "ord_qty": str(quantity),
            "trde_tp": self.settings.trde_tp,
        }
        if self.settings.order_unit_price:
            payload["ord_uv"] = self.settings.order_unit_price

        return self.request(
            "POST",
            self.settings.order_path,
            json_body=payload,
            headers={"api-id": api_id},
        )

    def get_daily_closes(self, symbol: str, *, limit: int = 60) -> list[float]:
        bars = self.get_daily_bars(symbol, limit=limit)
        return [float(row.get("close", 0.0)) for row in bars if float(row.get("close", 0.0)) > 0]

    def _is_regular_market_open(self) -> bool:
        try:
            tz_name = str(getattr(self.settings, "market_timezone", "Asia/Seoul") or "Asia/Seoul")
            now_local = datetime.now(ZoneInfo(tz_name))
            if now_local.weekday() >= 5:
                return False
            cur_t = now_local.time()

            start_raw = str(getattr(self.settings, "regular_session_start", "09:00") or "09:00")
            end_raw = str(getattr(self.settings, "regular_session_end", "15:30") or "15:30")

            def _parse_hhmm(raw: str, default: str) -> dt_time:
                text = str(raw or "").strip() or default
                try:
                    h, m = text.split(":", 1)
                    return dt_time(hour=max(0, min(23, int(h))), minute=max(0, min(59, int(m))))
                except Exception:
                    dh, dm = default.split(":", 1)
                    return dt_time(hour=int(dh), minute=int(dm))

            start_t = _parse_hhmm(start_raw, "09:00")
            end_t = _parse_hhmm(end_raw, "15:30")
            return start_t <= cur_t <= end_t
        except Exception:
            # Conservative fallback for KRX
            now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
            return now_kst.weekday() < 5 and dt_time(9, 0) <= now_kst.time() <= dt_time(15, 30)

    def get_daily_bars(self, symbol: str, *, limit: int = 60) -> list[dict[str, float]]:
        normalized_symbol = str(symbol or "").strip()
        normalized_limit = max(1, int(limit))
        cache_ttl_sec = max(10.0, float(os.getenv("KIWOOM_DAILY_BARS_CACHE_TTL_SEC", "90")))
        cache_key = (normalized_symbol, normalized_limit)
        now = time.time()
        cached = self._daily_bars_cache.get(cache_key)
        if cached is not None:
            cached_at, cached_rows = cached
            if (now - cached_at) <= cache_ttl_sec:
                return [dict(row) for row in cached_rows]

        headers = {"api-id": self.settings.price_history_api_id}
        today = datetime.now().strftime("%Y%m%d")
        data = self.request(
            "POST",
            self.settings.price_path,
            json_body={"stk_cd": normalized_symbol, "date": today},
            headers=headers,
        )
        rows = data.get("stk_ddwkmm") or []
        bars: list[dict[str, float]] = []

        def _pick_num(row: dict[str, Any], keys: tuple[str, ...]) -> float:
            for key in keys:
                if key in row:
                    val = self._to_float_abs(row.get(key))
                    if val > 0:
                        return val
            return 0.0

        def _pick_int(row: dict[str, Any], keys: tuple[str, ...]) -> int:
            for key in keys:
                if key in row:
                    val = self._to_int_abs(row.get(key))
                    if val > 0:
                        return val
            return 0

        for row in rows:
            close = _pick_num(row, ("close_pric", "cur_prc", "close", "cls_prc"))
            if close <= 0:
                continue
            volume = float(_pick_int(row, ("acml_vol", "trde_qty", "vol", "trade_qty", "deal_qty")))
            value = _pick_num(row, ("acml_tr_pbmn", "trde_prica", "trade_value", "deal_amt", "acml_trde_amt"))
            fallback_value = float(close * volume) if close > 0.0 and volume > 0.0 else 0.0
            if fallback_value > 0.0 and (value <= 0.0 or value < (fallback_value * 0.01)):
                value = fallback_value
            bars.append(
                {
                    "open": _pick_num(row, ("open_pric", "open", "start_pric", "stck_oprc")) or close,
                    "high": _pick_num(row, ("high_pric", "high", "hgpr", "stck_hgpr")) or close,
                    "low": _pick_num(row, ("low_pric", "low", "lwpr", "stck_lwpr")) or close,
                    "close": close,
                    "volume": volume,
                    "value": value,
                }
            )
            if len(bars) >= normalized_limit:
                break
        bars.reverse()
        self._daily_bars_cache[cache_key] = (now, [dict(row) for row in bars])
        if len(self._daily_bars_cache) > 4096:
            oldest = min(self._daily_bars_cache.items(), key=lambda item: item[1][0])[0]
            self._daily_bars_cache.pop(oldest, None)
        return [dict(row) for row in bars]

    def get_intraday_bars(self, symbol: str, *, interval: int = 2, limit: int = 200) -> list[dict[str, float]]:
        normalized_symbol = str(symbol or "").strip()
        normalized_limit = max(1, int(limit))
        normalized_interval = max(1, int(interval))
        cache_ttl_sec = max(10.0, float(os.getenv("KIWOOM_INTRADAY_BARS_CACHE_TTL_SEC", "30")))
        cache_key = (normalized_symbol, normalized_interval, normalized_limit)
        now = time.time()
        cached = self._intraday_bars_cache.get(cache_key)
        if cached is not None:
            cached_at, cached_rows = cached
            if (now - cached_at) <= cache_ttl_sec:
                return [dict(row) for row in cached_rows]

        headers = {"api-id": self.settings.price_history_api_id}
        today = datetime.now().strftime("%Y%m%d")
        payload = {
            "stk_cd": normalized_symbol,
            "date": today,
            "interval": normalized_interval,
            "limit": normalized_limit,
        }

        data = self.request(
            "POST",
            self.settings.price_path,
            json_body=payload,
            headers=headers,
        )

        rows = []
        if isinstance(data, dict):
            for key in ("stk_time", "stk_minute", "candle", "intraday", "prices", "ohlcv"):
                if key in data and isinstance(data[key], list):
                    rows = data[key]
                    break
            if not rows:
                rows = data.get("stk_ddwkmm") or []

        bars: list[dict[str, float]] = []

        def _pick_num(row: dict[str, Any], keys: tuple[str, ...]) -> float:
            for key in keys:
                if key in row:
                    val = self._to_float_abs(row.get(key))
                    if val > 0:
                        return val
            return 0.0

        def _pick_int(row: dict[str, Any], keys: tuple[str, ...]) -> int:
            for key in keys:
                if key in row:
                    val = self._to_int_abs(row.get(key))
                    if val > 0:
                        return val
            return 0

        for row in rows:
            if not isinstance(row, dict):
                continue
            close = _pick_num(row, ("close_pric", "cur_prc", "close", "cls_prc"))
            if close <= 0:
                continue
            open_p = _pick_num(row, ("open_pric", "open", "start_pric", "stck_oprc")) or close
            high = _pick_num(row, ("high_pric", "high", "hgpr", "stck_hgpr")) or max(open_p, close)
            low = _pick_num(row, ("low_pric", "low", "lwpr", "stck_lwpr")) or min(open_p, close)
            volume = float(_pick_int(row, ("acml_vol", "trde_qty", "vol", "trade_qty", "deal_qty")))
            bars.append(
                {
                    "open": open_p,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                }
            )
            if len(bars) >= normalized_limit:
                break

        if not bars and not self._is_regular_market_open():
            bars = self.get_daily_bars(symbol, limit=normalized_limit)

        self._intraday_bars_cache[cache_key] = (now, [dict(row) for row in bars])
        if len(self._intraday_bars_cache) > 4096:
            oldest = min(self._intraday_bars_cache.items(), key=lambda item: item[1][0])[0]
            self._intraday_bars_cache.pop(oldest, None)

        return [dict(row) for row in bars]

    def get_market_regime_snapshot(self, *, inds_cd: str = "001") -> dict[str, Any]:
        return self.request(
            "POST",
            "/api/dostk/sect",
            json_body={"inds_cd": inds_cd},
            headers={"api-id": self.settings.market_regime_api_id},
        )

    def get_symbol_investor_flow(self, symbol: str, *, after_close: bool = False) -> dict[str, Any]:
        api_id = (
            self.settings.after_close_investor_flow_api_id
            if after_close
            else self.settings.investor_flow_api_id
        )
        payload = self.request(
            "POST",
            self.settings.price_path,
            json_body={"stk_cd": symbol},
            headers={"api-id": api_id},
        )
        foreign_net = self._first_number(
            payload,
            (
                "frgn_ntby_qty",
                "frgn_net_qty",
                "for_net_qty",
                "foreign_net_qty",
                "frgn_sunm_qty",
                "frgn_pure_buy_qty",
            ),
            absolute=False,
        )
        institution_net = self._first_number(
            payload,
            (
                "orgn_ntby_qty",
                "inst_net_qty",
                "institution_net_qty",
                "orgn_net_qty",
                "orgn_sunm_qty",
            ),
            absolute=False,
        )
        return {
            "symbol": symbol,
            "foreign_net_qty": foreign_net,
            "institution_net_qty": institution_net,
            "raw": payload,
        }

    def get_vi_trigger_snapshot(self) -> dict[str, Any]:
        payload = self.request(
            "POST",
            self.settings.price_path,
            json_body={},
            headers={"api-id": self.settings.vi_trigger_api_id},
        )
        rows: list[dict[str, Any]] = []
        for node in self._walk_nodes(payload):
            if not isinstance(node, dict):
                continue
            symbol = str(
                node.get("stk_cd")
                or node.get("jongmok_cd")
                or node.get("symbol")
                or ""
            ).strip()
            if not symbol:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "name": str(node.get("stk_nm") or node.get("name") or "").strip(),
                    "raw": node,
                }
            )
        dedup = list({row["symbol"]: row for row in rows}.values())
        return {"count": len(dedup), "rows": dedup[:20], "raw": payload}
