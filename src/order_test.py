from __future__ import annotations

import os

from config import load_settings
from kiwoom_api import KiwoomAPI


def main() -> None:
    settings = load_settings()
    api = KiwoomAPI(settings)

    api.login()
    price = api.get_last_price(settings.symbol)
    print(f"quote_ok symbol={settings.symbol} price={price}")

    confirm = os.getenv("CONFIRM_ORDER_TEST", "false").strip().lower()
    if confirm not in {"1", "true", "yes", "y"}:
        print("skip_order CONFIRM_ORDER_TEST is not true")
        return

    response = api.place_order(symbol=settings.symbol, side="BUY", quantity=1)
    print(f"order_response={response}")


if __name__ == "__main__":
    main()
