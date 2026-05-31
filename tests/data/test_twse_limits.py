from data.twse_fetcher import _current_limit_flags, _fetch_twse_limits


def test_fetch_twse_limits_uses_exchange_limit_prices(monkeypatch):
    monkeypatch.setattr(
        "data.twse_fetcher._get",
        lambda url: [
            {"Code": "2330", "TodayLimitUp": "1,100.00", "TodayLimitDown": "900.00"},
            {"Code": "BAD", "TodayLimitUp": "0", "TodayLimitDown": "0"},
        ],
    )

    result = _fetch_twse_limits()

    assert result == {
        "2330": {
            "limit_up": 1100.0,
            "limit_down": 900.0,
        }
    }


def test_current_limit_flags_detects_twse_limit_up_from_quote():
    is_up, is_down, basis = _current_limit_flags({
        "price": 289.0,
        "high": 289.0,
        "low": 268.0,
        "day_return": 0.0988,
    })

    assert is_up is True
    assert is_down is False
    assert basis == "tw-stock-agent:exchange_quote_current_limit"


def test_current_limit_flags_detects_tpex_limit_up_from_best_ask_zero():
    is_up, is_down, basis = _current_limit_flags({
        "price": 50.5,
        "high": 50.5,
        "low": 45.75,
        "day_return": 0.0990,
        "last_bid": 50.5,
        "last_ask": 0.0,
    })

    assert is_up is True
    assert is_down is False
    assert basis == "tw-stock-agent:tpex_quote_best_ask_zero"


def test_current_limit_flags_does_not_mark_non_limit_strong_move():
    is_up, is_down, basis = _current_limit_flags({
        "price": 16.95,
        "high": 17.4,
        "low": 16.0,
        "day_return": 0.0594,
    })

    assert is_up is False
    assert is_down is False
    assert basis == "unavailable"
