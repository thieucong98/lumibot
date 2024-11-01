import pandas as pd
from typing import Dict, Any
from decimal import Decimal, ROUND_DOWN
import time
import logging

from lumibot.strategies.strategy import Strategy
from lumibot.tools.pandas import prettify_dataframe_with_decimals

logger = logging.getLogger(__name__)


class DriftCalculationLogic:
    def __init__(self, target_weights: Dict[str, Decimal]) -> None:
        self.df = pd.DataFrame({
            "symbol": target_weights.keys(),
            "is_quote_asset": False,
            "current_quantity": Decimal(0),
            "current_value": Decimal(0),
            "current_weight": Decimal(0),
            "target_weight": [Decimal(weight) for weight in target_weights.values()],
            "target_value": Decimal(0),
            "absolute_drift": Decimal(0)
        })

    def add_position(self, *, symbol: str, is_quote_asset: bool, current_quantity: Decimal, current_value: Decimal) -> None:
        if symbol in self.df["symbol"].values:
            self.df.loc[self.df["symbol"] == symbol, "is_quote_asset"] = is_quote_asset
            self.df.loc[self.df["symbol"] == symbol, "current_quantity"] = current_quantity
            self.df.loc[self.df["symbol"] == symbol, "current_value"] = current_value
        else:
            new_row = {
                "symbol": symbol,
                "is_quote_asset": is_quote_asset,
                "current_quantity": current_quantity,
                "current_value": current_value,
                "current_weight": Decimal(0),
                "target_weight": Decimal(0),
                "target_value": Decimal(0),
                "absolute_drift": Decimal(0)
            }
            # Convert the dictionary to a DataFrame
            new_row_df = pd.DataFrame([new_row])

            # Concatenate the new row to the existing DataFrame
            self.df = pd.concat([self.df, new_row_df], ignore_index=True)

    def calculate(self) -> pd.DataFrame:
        """
        A positive drift means we need to buy more of the asset,
        a negative drift means we need to sell some of the asset.
        """
        total_value = self.df["current_value"].sum()
        self.df["current_weight"] = self.df["current_value"] / total_value
        self.df["target_value"] = self.df["target_weight"] * total_value

        def calculate_drift_row(row: pd.Series) -> Decimal:
            if row["is_quote_asset"]:
                # We can never buy or sell the quote asset
                return Decimal(0)
            elif row["current_quantity"] > Decimal(0) and row["target_weight"] == Decimal(0):
                return Decimal(-1)
            elif row["current_quantity"] == Decimal(0) and row["target_weight"] > Decimal(0):
                return Decimal(1)
            else:
                return row["target_weight"] - row["current_weight"]

        self.df["absolute_drift"] = self.df.apply(calculate_drift_row, axis=1)
        return self.df


class LimitOrderRebalanceLogic:
    def __init__(
            self,
            *,
            strategy: Strategy,
            df: pd.DataFrame,
            fill_sleeptime: int = 15,
            acceptable_slippage: Decimal = Decimal("0.0005")
    ) -> None:
        self.strategy = strategy
        self.df = df
        self.fill_sleeptime = fill_sleeptime
        self.acceptable_slippage = acceptable_slippage

    def rebalance(self) -> None:
        # Execute sells first
        for index, row in self.df.iterrows():
            if row["absolute_drift"] == -1:
                # Sell everything
                symbol = row["symbol"]
                quantity = row["current_quantity"]
                last_price = Decimal(self.strategy.get_last_price(symbol))
                limit_price = self.calculate_limit_price(last_price=last_price, side="sell")
                self.place_limit_order(symbol=symbol, quantity=quantity, limit_price=limit_price, side="sell")
            elif row["absolute_drift"] < 0:
                symbol = row["symbol"]
                last_price = Decimal(self.strategy.get_last_price(symbol))
                limit_price = self.calculate_limit_price(last_price=last_price, side="sell")
                quantity = ((row["current_value"] - row["target_value"]) / limit_price).quantize(Decimal('1'), rounding=ROUND_DOWN)
                self.place_limit_order(symbol=symbol, quantity=quantity, limit_price=limit_price, side="sell")

        if not self.strategy.is_backtesting:
            # Sleep to allow sell orders to fill
            time.sleep(self.fill_sleeptime)

        # Get current cash position from the broker
        cash_position = self.get_current_cash_position()

        # Execute buys
        for index, row in self.df.iterrows():
            if row["absolute_drift"] > 0:
                symbol = row["symbol"]
                last_price = Decimal(self.strategy.get_last_price(symbol))
                limit_price = self.calculate_limit_price(last_price=last_price, side="buy")
                order_value = row["target_value"] - row["current_value"]
                quantity = (min(order_value, cash_position) / limit_price).quantize(Decimal('1'), rounding=ROUND_DOWN)
                if quantity > 0:
                    self.place_limit_order(symbol=symbol, quantity=quantity, limit_price=limit_price, side="buy")
                    cash_position -= min(order_value, cash_position)
                else:
                    logger.error(f"Ran out of cash to buy {symbol}. cash: {cash_position}")

    def calculate_limit_price(self, *, last_price: Decimal, side: str) -> Decimal:
        if side == "sell":
            return last_price * (1 - self.acceptable_slippage / Decimal(10000))
        elif side == "buy":
            return last_price * (1 + self.acceptable_slippage / Decimal(10000))

    def get_current_cash_position(self) -> Decimal:
        self.strategy.update_broker_balances(force_update=True)
        return Decimal(self.strategy.cash)

    def place_limit_order(self, *, symbol: str, quantity: Decimal, limit_price: Decimal, side: str) -> None:
        limit_order = self.strategy.create_order(
            asset=symbol,
            quantity=quantity,
            side=side,
            limit_price=float(limit_price)
        )
        self.strategy.submit_order(limit_order)


class DriftRebalancer(Strategy):
    """The DriftRebalancer strategy rebalances a portfolio based on drift from target weights.

    The strategy calculates the drift of each asset in the portfolio and triggers a rebalance if the drift exceeds
    the absolute_drift_threshold. The strategy will sell assets that have drifted above the threshold and
    buy assets that have drifted below the threshold.

    The current version of the DriftRebalancer strategy only supports limit orders and whole share quantities.
    Submit an issue if you need market orders or fractional shares. It should be pretty easy to add.

    Example parameters:

    parameters = {
        "market": "NYSE",
        "sleeptime": "1D",
        "absolute_drift_threshold": "0.20",
        "acceptable_slippage": "0.0005",
        "fill_sleeptime": 15,
        "target_weights": {
            "SPY": "0.60",
            "TLT": "0.40"
        }
    }
    """

    # noinspection PyAttributeOutsideInit
    def initialize(self, parameters: Any = None) -> None:
        self.set_market(self.parameters.get("market", "NYSE"))
        self.sleeptime = self.parameters.get("sleeptime", "1D")
        self.absolute_drift_threshold = Decimal(self.parameters.get("absolute_drift_threshold", "0.20"))
        self.acceptable_slippage = Decimal(self.parameters.get("acceptable_slippage", "0.0005"))
        self.fill_sleeptime = self.parameters.get("fill_sleeptime", 15)
        self.target_weights = {k: Decimal(v) for k, v in self.parameters["target_weights"].items()}

        # Sanity checks
        if self.acceptable_slippage >= self.absolute_drift_threshold:
            raise ValueError("acceptable_slippage must be less than absolute_drift_threshold")
        if self.absolute_drift_threshold >= Decimal("1.0"):
            raise ValueError("absolute_drift_threshold must be less than 1.0")
        for key, target_weight in self.target_weights.items():
            if self.absolute_drift_threshold >= target_weight:
                logger.warning(
                    f"absolute_drift_threshold of {self.absolute_drift_threshold} is "
                    f">= target_weight of {key}: {target_weight}. Drift in this asset will never trigger a rebalance."
                )

    def on_trading_iteration(self) -> None:
        dt = self.get_datetime()
        logger.info(f"{dt} on_trading_iteration called")
        self.cancel_open_orders()
        drift_calculator = DriftCalculationLogic(target_weights=self.target_weights)

        # Get all positions and add them to the calculator
        positions = self.get_positions()
        for position in positions:
            symbol = position.symbol
            current_quantity = Decimal(position.quantity)
            if position.asset == self.quote_asset:
                is_quote_asset = True
                current_value = Decimal(position.quantity)
            else:
                is_quote_asset = False
                current_value = Decimal(self.get_last_price(symbol)) * current_quantity
            drift_calculator.add_position(
                symbol=symbol,
                is_quote_asset=is_quote_asset,
                current_quantity=current_quantity,
                current_value=current_value
            )

        df = drift_calculator.calculate()

        # Check if the absolute value of any drift is greater than the threshold
        if (df["absolute_drift"].abs() > self.absolute_drift_threshold).any():
            logger.info(f"Absolute drift exceeds threshold. Rebalancing portfolio.")
            logger.info(f"Drift:\n{prettify_dataframe_with_decimals(df)}")
            rebalance_logic = LimitOrderRebalanceLogic(
                strategy=self,
                df=df,
                fill_sleeptime=self.fill_sleeptime,
                acceptable_slippage=self.acceptable_slippage
            )
            rebalance_logic.rebalance()

    def on_abrupt_closing(self):
        dt = self.get_datetime()
        logger.info(f"{dt} on_abrupt_closing called")
        self.log_message("On abrupt closing called.", broadcast=True)
        self.cancel_open_orders()

    def on_bot_crash(self, error):
        dt = self.get_datetime()
        logger.info(f"{dt} on_bot_crash called")
        self.log_message(f"Bot crashed with error: {error}", broadcast=True)
        self.cancel_open_orders()