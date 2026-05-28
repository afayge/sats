from __future__ import annotations

from typing import Protocol

from sats.trading.models import BrokerAsset, BrokerOrder, BrokerPosition, BrokerTrade, OrderRequest, OrderResult


class BrokerError(RuntimeError):
    pass


class BrokerClient(Protocol):
    provider: str
    account_id: str

    def status(self) -> dict:
        ...

    def asset(self) -> BrokerAsset:
        ...

    def positions(self) -> list[BrokerPosition]:
        ...

    def orders(self, *, open_only: bool = False) -> list[BrokerOrder]:
        ...

    def trades(self, *, limit: int = 100) -> list[BrokerTrade]:
        ...

    def place_order(self, request: OrderRequest) -> OrderResult:
        ...

    def cancel_order(self, order_id: str) -> OrderResult:
        ...
