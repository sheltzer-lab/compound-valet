"""Tests for core/references/clients.py"""

import importlib

import pytest

from core.references.clients import CrossRefClient


class TestCrossRefAuthHeaders:
    def test_no_token_by_default(self, monkeypatch):
        monkeypatch.delenv("CROSSREF_API_TOKEN", raising=False)
        assert "Crossref-Plus-API-Token" not in CrossRefClient().headers

    def test_plus_token_from_env(self, monkeypatch):
        monkeypatch.setenv("CROSSREF_API_TOKEN", "tok-123")
        assert CrossRefClient().headers["Crossref-Plus-API-Token"] == "Bearer tok-123"

    def test_explicit_email_wins(self):
        ua = CrossRefClient(email="arg@example.com").headers["User-Agent"]
        assert "mailto:arg@example.com)" in ua

    def test_empty_email_opts_out(self):
        assert "mailto" not in CrossRefClient(email="").headers["User-Agent"]


class TestCrossRefBudgets:
    def _reload(self):
        import core.references.clients as mod
        return importlib.reload(mod)

    def test_polite_budgets_without_token(self, monkeypatch):
        monkeypatch.delenv("CROSSREF_API_TOKEN", raising=False)
        monkeypatch.delenv("CROSSREF_RPS_BUDGET", raising=False)
        monkeypatch.delenv("CROSSREF_CONCURRENCY_BUDGET", raising=False)
        mod = self._reload()
        assert (mod.CROSSREF_RPS_BUDGET, mod.CROSSREF_CONCURRENCY_BUDGET) == (10.0, 3)

    def test_plus_budgets_with_token(self, monkeypatch):
        monkeypatch.setenv("CROSSREF_API_TOKEN", "tok-123")
        monkeypatch.delenv("CROSSREF_RPS_BUDGET", raising=False)
        monkeypatch.delenv("CROSSREF_CONCURRENCY_BUDGET", raising=False)
        mod = self._reload()
        assert (mod.CROSSREF_RPS_BUDGET, mod.CROSSREF_CONCURRENCY_BUDGET) == (150.0, 25)

    def test_divisor_still_splits_the_plus_budget(self, monkeypatch):
        """Each process of a fan-out must take a fraction of the raised budget, not all of it."""
        monkeypatch.setenv("CROSSREF_API_TOKEN", "tok-123")
        monkeypatch.setenv("API_RATE_DIVISOR", "10")
        monkeypatch.delenv("CROSSREF_RPS_BUDGET", raising=False)
        monkeypatch.delenv("CROSSREF_CONCURRENCY_BUDGET", raising=False)
        mod = self._reload()
        assert mod.CROSSREF_GLOBAL_LIMITER.rate == 15.0
        assert mod.CROSSREF_MAX_CONCURRENCY == 2  # 25 // 10

    def test_env_overrides_budget(self, monkeypatch):
        monkeypatch.setenv("CROSSREF_RPS_BUDGET", "3.5")
        assert self._reload().CROSSREF_RPS_BUDGET == 3.5

    @pytest.mark.parametrize("bad", ["off", "0", "-1", "   ", "2.5.1"])
    def test_unusable_rps_budget_falls_back_instead_of_breaking(self, monkeypatch, bad):
        """A typo in a setting must not reach the token bucket. Non-numeric fails at
        import; 0 divides by zero on the first request; negative spins forever in acquire()."""
        monkeypatch.delenv("CROSSREF_API_TOKEN", raising=False)
        monkeypatch.setenv("CROSSREF_RPS_BUDGET", bad)
        mod = self._reload()
        assert mod.CROSSREF_RPS_BUDGET == 10.0
        assert mod.CROSSREF_GLOBAL_LIMITER.rate > 0

    @pytest.mark.parametrize("bad", ["off", "0", "-4", "2.5"])
    def test_unusable_concurrency_budget_falls_back(self, monkeypatch, bad):
        """int() is stricter than float(): '2.5' is refused here and that is correct —
        a concurrency gate cannot be fractional."""
        monkeypatch.delenv("CROSSREF_API_TOKEN", raising=False)
        monkeypatch.setenv("CROSSREF_CONCURRENCY_BUDGET", bad)
        mod = self._reload()
        assert mod.CROSSREF_CONCURRENCY_BUDGET == 3
        assert mod.CROSSREF_MAX_CONCURRENCY >= 1

    def teardown_method(self):
        """Leave the module in its ambient-env state for other tests."""
        import core.references.clients as mod
        importlib.reload(mod)
