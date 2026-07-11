import server


def test_tw_value_ranking_uses_all_market_actual_turnover():
    raw = {
        "1111.TW": {
            "stock_type": "stock", "name": "甲公司", "price": 50,
            "volume": 100, "turnover_10k": 2_000,
        },
        "2222.TWO": {
            "stock_type": "stock", "name": "乙公司", "price": 25,
            "volume": 100, "turnover_10k": 5_000,
        },
        "0050.TW": {
            "stock_type": "etf", "name": "ETF", "price": 200,
            "volume": 1_000, "turnover_10k": 9_999,
        },
    }

    ranking = server._build_tw_value_ranking(raw, top_n=100)

    assert [row["ticker"] for row in ranking] == ["0050.TW", "2222.TWO", "1111.TW"]
    assert [row["rank"] for row in ranking] == [1, 2, 3]
    assert ranking[1]["turnover_value"] == 50_000_000


def test_tw_value_ranking_keeps_actual_turnover_over_price_volume_estimate():
    raw = {
        "1111.TW": {
            "stock_type": "stock", "name": "甲公司", "price": 100,
            "volume": 1_000, "turnover_10k": 123,
        },
    }

    ranking = server._build_tw_value_ranking(raw)

    assert ranking[0]["turnover_value"] == 1_230_000
