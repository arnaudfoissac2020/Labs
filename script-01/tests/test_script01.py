import math
import pytest
import pandas as pd
from pathlib import Path

from script_01_lending_due_diligence import (
    _parse_protocol_tvl_summary,
    _parse_lending_markets,
    _parse_tvl_history,
    _compute_protocol_scores,
    _apply_screening_criteria,
)
from helpers import load_fixture, FIXTURES


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_eth_series(values: list) -> dict:
    """Build a minimal DeFi Llama protocol payload with a given ETH TVL series."""
    return {
        "name": "TestProtocol",
        "category": "Lending",
        "chainTvls": {
            "Ethereum": {
                "tvl": [{"totalLiquidityUSD": v} for v in values]
            }
        },
        "tvl": [{"totalLiquidityUSD": values[-1]}],
        "fees": {},
        "audits": [],
        "description": "",
    }


def _make_market(name: str, tvl: float, deposits: float, borrows: float,
                 active: bool = True, supply_rate: float = 0.05) -> dict:
    return {
        "name": name,
        "totalValueLockedUSD": str(tvl),
        "totalDepositBalanceUSD": str(deposits),
        "totalBorrowBalanceUSD": str(borrows),
        "rates": [{"side": "LENDER", "type": "VARIABLE", "rate": str(supply_rate)}],
        "liquidationThreshold": "0.8",
        "canBorrowFrom": True,
        "isActive": active,
    }


# ─── TestParseTvlSummary ───────────────────────────────────────────────────────

class TestParseTvlSummary:
    def test_fixture_returns_expected_keys(self):
        fixture_path = FIXTURES / "defillama__protocol_aave-v3.json"
        if not fixture_path.exists():
            pytest.skip("Fixture not yet captured — run PRODUCE_MOCK=1 python script_01_lending_due_diligence.py")
        data = load_fixture("defillama__protocol_aave-v3")
        result = _parse_protocol_tvl_summary(data)
        for key in ("tvl_eth_usd", "tvl_change_30d_pct", "tvl_stability_ratio", "name"):
            assert key in result
        assert 0.0 <= result["tvl_stability_ratio"] <= 1.0

    def test_normal_30d_series(self):
        values = list(range(100, 131))  # 31 values, rising
        result = _parse_protocol_tvl_summary(_make_eth_series(values))
        assert result["tvl_eth_usd"] == 130
        assert result["tvl_change_30d_pct"] == pytest.approx((130 / 101 - 1) * 100, rel=1e-6)

    def test_stability_ratio_is_min_over_max(self):
        values = [80, 100, 90, 120, 60]
        result = _parse_protocol_tvl_summary(_make_eth_series(values))
        assert result["tvl_stability_ratio"] == pytest.approx(60 / 120, rel=1e-6)

    def test_fewer_than_30_days_uses_available(self):
        values = [500, 600, 700]
        result = _parse_protocol_tvl_summary(_make_eth_series(values))
        assert result["tvl_eth_usd"] == 700
        assert result["tvl_change_30d_pct"] == pytest.approx((700 / 500 - 1) * 100, rel=1e-6)

    def test_empty_eth_series_returns_zeros(self):
        data = {"name": "X", "category": "N/A", "chainTvls": {}, "tvl": [], "fees": {}, "audits": [], "description": ""}
        result = _parse_protocol_tvl_summary(data)
        assert result["tvl_eth_usd"] == 0
        assert result["tvl_change_30d_pct"] == 0
        assert result["tvl_stability_ratio"] == 0

    def test_zero_tvl_30d_ago_no_division_error(self):
        values = [0, 100, 200]
        result = _parse_protocol_tvl_summary(_make_eth_series(values))
        assert result["tvl_change_30d_pct"] == 0


# ─── TestParseLendingMarkets ───────────────────────────────────────────────────

class TestParseLendingMarkets:
    def test_fixture_returns_dataframe(self):
        fixture_path = FIXTURES / "graphql_JCNWRypm7FYw.json"
        if not fixture_path.exists():
            pytest.skip("Fixture not yet captured — run PRODUCE_MOCK=1 python script_01_lending_due_diligence.py")
        data = load_fixture("graphql_JCNWRypm7FYw")
        df = _parse_lending_markets("Aave V3", data)
        assert not df.empty
        assert "utilization_rate" in df.columns
        assert "tvl_share_pct" in df.columns

    def test_utilization_rate_calculation(self):
        data = {"markets": [_make_market("USDC", 1_000_000, 1_000_000, 850_000)]}
        df = _parse_lending_markets("Test", data)
        assert df.iloc[0]["utilization_rate"] == pytest.approx(0.85, rel=1e-6)

    def test_zero_deposits_no_division_error(self):
        data = {"markets": [_make_market("USDC", 1_000_000, 0, 0)]}
        df = _parse_lending_markets("Test", data)
        assert df.iloc[0]["utilization_rate"] == 0.0

    def test_tvl_share_sums_to_100(self):
        data = {"markets": [
            _make_market("USDC", 600_000, 600_000, 400_000),
            _make_market("WETH", 300_000, 300_000, 200_000),
            _make_market("DAI",  100_000, 100_000,  50_000),
        ]}
        df = _parse_lending_markets("Test", data)
        assert df["tvl_share_pct"].sum() == pytest.approx(100.0, rel=1e-6)

    def test_inactive_markets_excluded(self):
        data = {"markets": [
            _make_market("USDC", 1_000_000, 1_000_000, 500_000, active=True),
            _make_market("OLD",  500_000,   500_000,   200_000, active=False),
        ]}
        df = _parse_lending_markets("Test", data)
        assert len(df) == 1
        assert df.iloc[0]["market"] == "USDC"

    def test_empty_markets_returns_empty_dataframe(self):
        df = _parse_lending_markets("Test", {"markets": []})
        assert df.empty


# ─── TestParseTvlHistory ──────────────────────────────────────────────────────

class TestParseTvlHistory:
    def test_fixture_date_format(self):
        fixture_path = FIXTURES / "defillama__protocol_aave-v3.json"
        if not fixture_path.exists():
            pytest.skip("Fixture not yet captured — run PRODUCE_MOCK=1 python script_01_lending_due_diligence.py")
        data = load_fixture("defillama__protocol_aave-v3")
        df = _parse_tvl_history(data, days=30)
        if not df.empty:
            import re
            assert re.match(r"\d{4}-\d{2}-\d{2}", df.iloc[0]["date"])

    def test_days_parameter_limits_rows(self):
        series = [{"date": 1_700_000_000 + i * 86400, "totalLiquidityUSD": float(i * 1e6)} for i in range(30)]
        data = {"chainTvls": {"Ethereum": {"tvl": series}}}
        df = _parse_tvl_history(data, days=5)
        assert len(df) == 5

    def test_first_tvl_change_is_nan(self):
        series = [{"date": 1_700_000_000 + i * 86400, "totalLiquidityUSD": float((i + 1) * 1e6)} for i in range(10)]
        data = {"chainTvls": {"Ethereum": {"tvl": series}}}
        df = _parse_tvl_history(data, days=10)
        assert math.isnan(df.iloc[0]["tvl_change_pct"])

    def test_no_ethereum_chain_returns_empty(self):
        data = {"chainTvls": {"Arbitrum": {"tvl": [{"date": 1_700_000_000, "totalLiquidityUSD": 1e6}]}}}
        df = _parse_tvl_history(data)
        assert df.empty


# ─── TestComputeProtocolScores ────────────────────────────────────────────────

class TestComputeProtocolScores:
    PERFECT_TVL = {"current_usd": 10e9, "max_daily_drop_pct": -1.0, "trend_30d_pct": 5.0, "stability_ratio": 0.95}
    PERFECT_CONC = {"hhi": 500, "top3_share_pct": 40, "avg_utilization": 0.50, "high_util_count": 0, "market_count": 10}

    def test_perfect_metrics_tvl_solidity_is_4(self):
        scores = _compute_protocol_scores(self.PERFECT_TVL, {})
        assert scores["tvl_solidity"] == 4

    def test_small_tvl_penalised_by_2(self):
        tvl = {**self.PERFECT_TVL, "current_usd": 400e6}
        scores = _compute_protocol_scores(tvl, {})
        assert scores["tvl_solidity"] == 2

    def test_large_daily_drop_penalised_by_1(self):
        tvl = {**self.PERFECT_TVL, "max_daily_drop_pct": -25.0}
        scores = _compute_protocol_scores(tvl, {})
        assert scores["tvl_solidity"] == 3

    def test_negative_trend_penalised_by_1(self):
        tvl = {**self.PERFECT_TVL, "trend_30d_pct": -20.0}
        scores = _compute_protocol_scores(tvl, {})
        assert scores["tvl_solidity"] == 3

    def test_low_stability_penalised_by_1(self):
        tvl = {**self.PERFECT_TVL, "stability_ratio": 0.60}
        scores = _compute_protocol_scores(tvl, {})
        assert scores["tvl_solidity"] == 3

    def test_tvl_solidity_floor_is_1(self):
        tvl = {"current_usd": 100e6, "max_daily_drop_pct": -30.0, "trend_30d_pct": -20.0, "stability_ratio": 0.50}
        scores = _compute_protocol_scores(tvl, {})
        assert scores["tvl_solidity"] == 1

    @pytest.mark.parametrize("hhi,expected", [
        (1499, 4), (1500, 3), (1999, 3), (2000, 2), (2499, 2), (2500, 1),
    ])
    def test_hhi_boundary_conditions(self, hhi, expected):
        conc = {**self.PERFECT_CONC, "hhi": hhi}
        scores = _compute_protocol_scores({}, conc)
        assert scores["concentration"] == expected

    def test_high_avg_utilization_penalised_by_2(self):
        conc = {**self.PERFECT_CONC, "avg_utilization": 0.92}
        scores = _compute_protocol_scores({}, conc)
        assert scores["health"] == 2

    def test_moderate_avg_utilization_penalised_by_1(self):
        conc = {**self.PERFECT_CONC, "avg_utilization": 0.85}
        scores = _compute_protocol_scores({}, conc)
        assert scores["health"] == 3

    def test_many_high_util_markets_penalised_by_1(self):
        conc = {**self.PERFECT_CONC, "high_util_count": 3}
        scores = _compute_protocol_scores({}, conc)
        assert scores["health"] == 3

    def test_health_floor_is_1(self):
        conc = {**self.PERFECT_CONC, "avg_utilization": 0.95, "high_util_count": 5}
        scores = _compute_protocol_scores({}, conc)
        assert scores["health"] == 1

    def test_global_is_average_of_dimensions(self):
        scores = _compute_protocol_scores(self.PERFECT_TVL, self.PERFECT_CONC)
        dims = {k: v for k, v in scores.items() if k != "global"}
        expected = round(sum(dims.values()) / len(dims), 2)
        assert scores["global"] == expected

    def test_partial_data_skips_missing_dimensions(self):
        scores = _compute_protocol_scores(self.PERFECT_TVL, {})
        assert "tvl_solidity" in scores
        assert "concentration" not in scores
        assert "health" not in scores

    def test_empty_inputs_returns_empty(self):
        scores = _compute_protocol_scores({}, {})
        assert scores == {}


# ─── TestApplyScreeningCriteria ───────────────────────────────────────────────

class TestApplyScreeningCriteria:
    THRESHOLDS = {
        "min_tvl_usd": 500_000_000,
        "max_tvl_drop_30d_pct": -30.0,
        "min_tvl_stability": 0.70,
    }

    def _df(self, tvl=1e9, change=5.0, stability=0.90):
        return pd.DataFrame([{
            "protocol": "TestProtocol",
            "tvl_eth_usd": tvl,
            "tvl_change_30d_pct": change,
            "tvl_stability_ratio": stability,
        }])

    def test_all_pass(self):
        df = _apply_screening_criteria(self._df(), self.THRESHOLDS)
        assert df.iloc[0]["eligible"] == True

    def test_fails_tvl_threshold(self):
        df = _apply_screening_criteria(self._df(tvl=499_999_999), self.THRESHOLDS)
        assert df.iloc[0]["eligible"] == False

    def test_fails_at_exact_tvl_boundary(self):
        df = _apply_screening_criteria(self._df(tvl=500_000_000), self.THRESHOLDS)
        assert df.iloc[0]["eligible"] == True

    def test_fails_change_threshold(self):
        df = _apply_screening_criteria(self._df(change=-30.1), self.THRESHOLDS)
        assert df.iloc[0]["eligible"] == False

    def test_fails_stability_threshold(self):
        df = _apply_screening_criteria(self._df(stability=0.699), self.THRESHOLDS)
        assert df.iloc[0]["eligible"] == False

    def test_does_not_mutate_input(self):
        original = self._df()
        _apply_screening_criteria(original, self.THRESHOLDS)
        assert "eligible" not in original.columns
