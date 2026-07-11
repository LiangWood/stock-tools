from pathlib import Path


WEB_INDEX = Path(__file__).resolve().parents[1] / "web" / "index.html"


def _html() -> str:
    return WEB_INDEX.read_text(encoding="utf-8")


def test_chart_toolbar_has_indicator_settings_entrypoint():
    html = _html()

    assert 'id="chart-indicator-settings-btn"' in html
    assert 'title="技術線設定"' in html
    assert 'id="chart-indicator-panel"' in html


def test_chart_indicator_settings_supports_ma_and_ema_modes():
    html = _html()

    assert "indicatorMode" in html
    assert 'data-indicator-mode="EMA"' in html
    assert 'data-indicator-mode="MA"' in html
    assert "calcMA(" in html
    assert "calcEMA(" in html


def test_chart_indicator_settings_exposes_six_custom_period_rows():
    html = _html()

    for index in range(6):
        assert f'data-indicator-index="{index}"' in html
    assert "DEFAULT_INDICATORS" in html
    assert "renderIndicatorControls()" in html


def test_chart_indicator_period_inputs_update_while_typing():
    html = _html()

    assert "updateIndicatorPeriod" in html
    assert "input.addEventListener('input'" in html


def test_chart_indicator_buttons_live_in_collapsible_overlay():
    html = _html()
    topbar_start = html.index('id="chart-topbar"')
    topbar_end = html.index('id="chart-area"')
    topbar = html[topbar_start:topbar_end]

    assert 'id="chart-indicator-overlay"' in html
    assert 'id="chart-indicator-collapse-btn"' in html
    assert "toggleIndicatorOverlay" in html
    assert "indicator-overlay-collapsed" in html
    assert 'id="chart-indicator-buttons"' not in topbar


def test_chart_indicator_collapse_icon_is_larger_without_growing_hit_area():
    html = _html()
    btn_start = html.index("#chart-indicator-collapse-btn {")
    btn_end = html.index(".indicator-overlay-collapsed", btn_start)
    btn_css = html[btn_start:btn_end]
    collapsed_start = html.index(".indicator-overlay-collapsed .chart-indicator-strip {")
    collapsed_end = html.index(".indicator-overlay-collapsed #chart-indicator-buttons", collapsed_start)
    collapsed_css = html[collapsed_start:collapsed_end]

    assert "width: 28px" in btn_css
    assert "height: 28px" in btn_css
    assert "font-size: 22px" in btn_css
    assert "line-height: 1" in btn_css
    assert "min-height: 36px" in collapsed_css
    assert "align-items: center" in collapsed_css
    assert "border: 0" in collapsed_css
    assert "background: transparent" in collapsed_css
    assert "box-shadow: none" in collapsed_css
    assert "chevronIcon(" in html
    assert "<svg" in html
    assert "btn.textContent = nextCollapsed" not in html


def test_chart_indicator_overlay_only_renders_enabled_lines():
    html = _html()

    assert "S.indicators.filter(item => item.enabled).map((item)" in html
    assert "data-indicator-toggle=\"${idx}\"" not in html


def test_chart_volume_histogram_is_hidden():
    html = _html()

    assert "addHistogramSeries" not in html
    assert "volSeries.setData" not in html
    assert "priceScale('vol')" not in html


def test_chart_close_button_is_fixed_visible_and_chart_info_is_16px():
    html = _html()
    close_css_start = html.index("#close-chart-btn {")
    close_css_end = html.index("#close-chart-btn:hover", close_css_start)
    close_css = html[close_css_start:close_css_end]
    symbol_css_start = html.index("#chart-symbol {")
    symbol_css_end = html.index("#chart-symbol .sym-name", symbol_css_start)
    symbol_css = html[symbol_css_start:symbol_css_end]
    name_css_start = html.index("#chart-symbol .sym-name {")
    name_css_end = html.index("#chart-live-price", name_css_start)
    name_css = html[name_css_start:name_css_end]
    live_css_start = html.index("#chart-live-price {")
    live_css_end = html.index("/* 游標 OHLC 資訊列 */", live_css_start)
    live_css = html[live_css_start:live_css_end]
    ohlc_css_start = html.index("#chart-ohlc {")
    ohlc_css_end = html.index("#chart-ohlc.active", ohlc_css_start)
    ohlc_css = html[ohlc_css_start:ohlc_css_end]
    live_markup_start = html.index("// Live price display")
    live_markup_end = html.index("// ── 關閉 / 展開圖表", live_markup_start)
    live_markup = html[live_markup_start:live_markup_end]

    assert "position: absolute" not in close_css
    assert "opacity: 0" not in close_css
    assert "pointer-events: none" not in close_css
    assert "margin-left: 6px" in close_css
    assert "font-size: 16px" in symbol_css
    assert "font-size: 16px" in name_css
    assert "font-size: 16px" in live_css
    assert ".chart-live-value" in live_css
    assert "font-size:12px" not in live_markup
    assert "font-size: 12px" not in live_markup
    assert "font-size: 12px" not in live_css
    assert "font-size: 16px" in ohlc_css


def test_chart_panel_drag_max_width_is_1400px():
    html = _html()

    assert "MAX_W = 1400" in html


def test_tw_toolbar_has_rotation_then_stackable_rank_tabs_and_no_observation_tabs():
    html = _html()
    tw_tabs_start = html.index('id="tw-strategy-tabs"')
    tw_tabs_end = html.index('id="crypto-strategy-tabs"')
    tw_tabs = html[tw_tabs_start:tw_tabs_end]

    rotation_pos = tw_tabs.index('data-strategy="rotation"')
    value_pos = tw_tabs.index('data-strategy="value"')
    rs_pos = tw_tabs.index('data-strategy="rs">RS排名')
    chips_pos = tw_tabs.index('data-strategy="chips">籌碼排名')
    assert rotation_pos < value_pos < rs_pos < chips_pos
    assert "籌碼加乘" not in tw_tabs
    assert "RS籌碼共振" not in tw_tabs
    assert "成交值排名" in tw_tabs
    assert 'id="tw-view-tabs"' not in html
    assert "起漲觀察" not in html
    assert "台股突破觀察" not in html


def test_tw_value_ranking_uses_dedicated_full_market_endpoint():
    html = _html()

    assert "TW_VALUE_COLS" in html
    assert "formatUnsignedYi(v / 1e8)" in html
    assert "'/api/tw-value-ranking'" in html
    assert "loadTwValueRanking(valueUrl)" in html


def test_tw_rank_tabs_are_stackable_and_rotation_is_exclusive():
    html = _html()

    assert "twActiveStrategies: ['value']" in html
    assert "toggleTwStrategy" in html
    assert "S.twStrategy = strategy" in html
    assert "strategy === 'rotation'" in html
    assert "S.twActiveStrategies = ['rotation']" in html
    assert "S.twActiveStrategies.includes('rotation')" in html
    assert "mergeTwRankStrategies" in html


def test_tw_default_sort_uses_last_selected_rank_strategy():
    html = _html()

    assert "function twScoreKey()" in html
    assert "if (S.twStrategy === 'rs' && twHasStrategy('rs')) return 'rs_score'" in html
    assert "if (S.twStrategy === 'value' && twHasStrategy('value')) return 'turnover_value'" in html
    assert "if (S.twStrategy === 'chips' && twHasStrategy('chips')) return 'tw_score'" in html


def test_tw_value_and_chips_single_mode_hide_rs_score_column():
    html = _html()

    assert "function visibleTwCols" in html
    assert "active.length === 1 && !active.includes('rs')" in html
    assert "cols.filter(col => col.key !== 'rs_score')" in html


def test_tw_rank_column_uses_current_render_order():
    html = _html()

    assert "col.key === 'rank' && S.universe === 'tw'" in html


def test_chart_divider_has_no_dotted_handle():
    html = _html()

    assert "#divider::after" not in html
    assert "content: '⋮'" not in html


def test_measure_selection_is_hidden_when_idle():
    html = _html()

    assert "#measure-sel" in html
    assert "selEl.style.display = 'block'" in html
    assert "selEl.style.display = 'none'" in html


def test_measure_tool_uses_drag_rectangle_and_price_distance():
    html = _html()

    assert "startClientY" in html
    assert "yToPrice" in html
    assert "candleSeries.coordinateToPrice" in html
    assert "selEl.style.top" in html
    assert "selEl.style.height" in html
    assert "垂直距離" in html
