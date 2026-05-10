import yfinance as yf
import json

ticker = yf.Ticker("AAPL")

print("--- 1. TICKER INFO (FUNDAMENTALS & PROFILE) ---")
info = ticker.info
print(f"Number of keys in info: {len(info)}")
print("Sample keys:")
for i, key in enumerate(sorted(list(info.keys()))):
    print(f"- {key}")
    if i > 50 and False: # Just to see them all in the terminal log
        break

print("\n--- 2. FINANCIAL STATEMENTS ---")
print("Annual Income Statement columns:", ticker.income_stmt.columns if not ticker.income_stmt.empty else "Empty")
print("Quarterly Balance Sheet columns:", ticker.quarterly_balance_sheet.columns if not ticker.quarterly_balance_sheet.empty else "Empty")

print("\n--- 3. OTHER DATA ATTRIBUTES ---")
print("Major Holders:", "Available" if not ticker.major_holders.empty else "None")
print("Institutional Holders:", "Available" if not ticker.institutional_holders.empty else "None")
print("Dividends/Splits:", "Available" if not ticker.actions.empty else "None")
print("Options:", ticker.options)
print("Earnings Dates:", ticker.calendar)
print("Recommendations:", "Available" if ticker.recommendations is not None else "None")
print("News:", len(ticker.news))
