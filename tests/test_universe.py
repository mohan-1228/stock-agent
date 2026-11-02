import pandas as pd

from update_universe import merge_universe, normalize_symbol, parse_sp500_html

SAMPLE_HTML = """
<table id="constituents">
  <thead><tr><th>Symbol</th><th>Security</th><th>GICS Sector</th></tr></thead>
  <tbody>
    <tr><td>AAPL</td><td>Apple Inc.</td><td>Information Technology</td></tr>
    <tr><td>BRK.B</td><td>Berkshire Hathaway</td><td>Financials</td></tr>
    <tr><td> BF.B </td><td>Brown-Forman</td><td>Consumer Staples</td></tr>
  </tbody>
</table>
<table id="changes"><tr><th>Date</th></tr><tr><td>2026-01-01</td></tr></table>
"""


def test_normalize_symbol():
    assert normalize_symbol("BRK.B") == "BRK-B"
    assert normalize_symbol(" brk.b ") == "BRK-B"
    assert normalize_symbol("AAPL") == "AAPL"


def test_parse_picks_constituents_table_and_fixes_symbols():
    df = parse_sp500_html(SAMPLE_HTML)
    assert list(df["ticker"]) == ["AAPL", "BRK-B", "BF-B"]
    assert df.loc[0, "name"] == "Apple Inc."


def test_merge_keeps_sp500_row_on_duplicate_and_adds_new():
    sp = pd.DataFrame({"ticker": ["AAPL", "MSFT"], "name": ["Apple", "Microsoft"]})
    extras = pd.DataFrame({"ticker": ["tsm", "AAPL"], "name": ["TSMC", "Apple duplicate"]})
    merged = merge_universe(sp, extras)
    assert list(merged["ticker"]) == ["AAPL", "MSFT", "TSM"]
    assert merged.loc[0, "name"] == "Apple"