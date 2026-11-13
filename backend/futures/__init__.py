"""Stock futures module. Step 1, the data layer: NFO contracts (instruments.py), continuous daily futures candles
with OI (candles.py), the MWPL ban list (ban.py), the eligibility rules (eligibility.py) and the evening-job step
that refreshes all three (data.py). Kite is used for login and market data only; nothing here orders."""
