"""Fubon account and inventory queries.

Wraps the Fubon SDK (fubon_neo 2.5.0) futures/options accounting queries.
Provides both low-level SDK wrapper methods and BrokerProtocol-aligned methods.

Real shapes (introspected from the 2.5.0 wheel)::

    sdk.futopt_accounting.query_margin_equity(account)     -> Result[list[Equity]]
    sdk.futopt_accounting.query_hybrid_position(account)   -> Result[list[HybridPosition]]
    sdk.futopt_accounting.query_single_position(account)   -> Result[list[Position]]
    sdk.futopt_accounting.close_position_record(account, start_date, end_date=None)
                                                           -> Result[list[CloseRecord]]

Every call needs the trading ``Account`` returned by login and answers with a
``Result`` envelope.  A failed or malformed envelope raises here; the
BrokerProtocol methods turn that into "unknown" (``None``) rather than "empty",
because 2.5.0 reports a genuinely empty book as success with an empty list.
"""

from __future__ import annotations

from typing import Any

from structlog import get_logger

from hft_platform.feed_adapter.fubon.order_codec import FubonSdkResultError, unwrap_sdk_result

logger = get_logger("feed_adapter.fubon.account_gateway")


class FubonAccountGateway:
    """Fubon futures/options account and position queries."""

    __slots__ = ("_sdk", "_account", "log")

    def __init__(self, sdk: Any, account: Any = None) -> None:
        self._sdk = sdk
        self._account = account
        self.log = logger

    def set_account(self, account: Any) -> None:
        """Bind the trading account returned by login (every query needs it)."""
        self._account = account

    def _pick_account(self, account: Any = None) -> Any:
        """Return the SDK account to query.

        A BrokerProtocol caller may pass an account *id string*; the SDK needs
        its own ``Account`` object, so a string (or None) means the stored one.
        """
        picked = self._account if account is None or isinstance(account, str) else account
        if picked is None:
            raise RuntimeError("FubonAccountGateway: no trading account (login has not completed)")
        return picked

    def _query_list(self, method: str, *args: Any, account: Any = None) -> list[Any]:
        """Run ``sdk.futopt_accounting.<method>(account, *args)`` and return its list.

        Raises:
            FubonSdkResultError: failed envelope, or ``data`` is not a list (a
                missing answer is never read as an empty one).
        """
        if self._sdk is None:
            raise RuntimeError("FubonAccountGateway: no SDK")
        call = getattr(self._sdk.futopt_accounting, method)
        data = unwrap_sdk_result(call(self._pick_account(account), *args), method)
        if not isinstance(data, list):
            raise FubonSdkResultError(f"{method}: expected a list, got {type(data).__name__}")
        return data

    # ------------------------------------------------------------------ #
    # Low-level SDK wrappers
    # ------------------------------------------------------------------ #

    def get_margin(self, account: Any = None) -> list[Any]:
        """Get margin equity (``query_margin_equity``).

        Returns:
            List of ``Equity`` records (one per currency).
        """
        try:
            result = self._query_list("query_margin_equity", account=account)
            self.log.info("fubon_get_margin", count=len(result))
            return result
        except Exception as exc:
            self.log.error("fubon_get_margin_failed", error=str(exc))
            raise

    def get_single_positions(self, account: Any = None) -> list[Any]:
        """Get single-leg positions (``query_single_position``)."""
        try:
            result = self._query_list("query_single_position", account=account)
            self.log.info("fubon_get_single_positions", count=len(result))
            return result
        except Exception as exc:
            self.log.error("fubon_get_single_positions_failed", error=str(exc))
            raise

    def get_hybrid_positions(self, account: Any = None) -> list[Any]:
        """Get positions including spread combinations (``query_hybrid_position``)."""
        try:
            result = self._query_list("query_hybrid_position", account=account)
            self.log.info("fubon_get_hybrid_positions", count=len(result))
            return result
        except Exception as exc:
            self.log.error("fubon_get_hybrid_positions_failed", error=str(exc))
            raise

    # ------------------------------------------------------------------ #
    # BrokerProtocol-aligned methods
    # ------------------------------------------------------------------ #

    def get_positions(self) -> list[Any] | None:
        """Get positions (BrokerProtocol-aligned).

        Delegates to :meth:`get_single_positions` (one record per leg, what the
        platform's per-symbol reconciliation compares against).

        Returns:
            List of position records (``[]`` = broker confirmed flat), or None
            when the broker state could not be read.
        """
        try:
            return self.get_single_positions()
        except Exception as exc:
            self.log.error("fubon_get_positions_failed", error=str(exc))
            # Protocol: None = broker state unknown; [] = confirmed empty.
            # Failure must return None so reconciliation doesn't treat
            # "broker unreachable" as "confirmed no positions".
            return None

    def get_account_balance(self, account: Any = None) -> Any:
        """Get account balance (BrokerProtocol-aligned).

        Delegates to :meth:`get_margin` (the futures margin equity).

        Args:
            account: SDK ``Account`` to query; None or an id string uses the
                account stored at login.

        Returns:
            The list of ``Equity`` records, or empty dict on failure.
        """
        try:
            return self.get_margin(account)
        except Exception as exc:
            self.log.error("fubon_get_account_balance_failed", error=str(exc))
            return {}

    def list_position_detail(self, account: Any = None) -> list[Any]:
        """Query per-position detail incl. unrealized P&L (BrokerProtocol-aligned).

        Uses ``query_single_position``; each ``Position`` carries ``profit_or_loss``.

        Args:
            account: SDK ``Account``; None or an id string uses the stored account.

        Returns:
            List of position detail records, or ``[]`` on failure.
        """
        try:
            return self.get_single_positions(account)
        except Exception as exc:
            self.log.error("fubon_list_position_detail_failed", error=str(exc))
            return []

    def list_profit_loss(
        self,
        account: Any = None,
        begin_date: str | None = None,
        end_date: str | None = None,
    ) -> list[Any]:
        """Query closed-position (realized P&L) records (BrokerProtocol-aligned).

        Uses ``close_position_record(account, start_date, end_date=None)``; the
        dates are passed to the SDK as given.

        Args:
            account: SDK ``Account``; None or an id string uses the stored account.
            begin_date: Start date (required by the SDK).
            end_date: End date; omitted means today.

        Returns:
            List of ``CloseRecord`` records, or ``[]`` on failure.
        """
        if begin_date is None:
            self.log.warning("fubon_list_profit_loss_unavailable", reason="begin_date required")
            return []
        try:
            args: tuple[Any, ...] = (begin_date,) if end_date is None else (begin_date, end_date)
            result = self._query_list("close_position_record", *args, account=account)
            self.log.info(
                "fubon_list_profit_loss",
                count=len(result),
                begin_date=begin_date,
                end_date=end_date,
            )
            return result
        except Exception as exc:
            self.log.error(
                "fubon_list_profit_loss_failed",
                error=str(exc),
                begin_date=begin_date,
                end_date=end_date,
            )
            return []
