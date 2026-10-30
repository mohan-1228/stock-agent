"""Show saved history for one ticker.   Usage: python history.py AAPL [days]"""
import sys

import config
from storage import connect, ticker_history


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: python history.py TICKER [days]")
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    conn = connect(config.DB_PATH)
    df = ticker_history(conn, sys.argv[1], limit)
    conn.close()
    print(df.to_string(index=False) if not df.empty else "No saved history for that ticker yet.")


if __name__ == "__main__":
    main()
