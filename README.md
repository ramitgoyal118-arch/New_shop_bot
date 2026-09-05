# Gemini Shop Bot

Features:
- Gemini Premium stock selling
- Minimum quantity 2
- Price default 0.6 USDT
- No unique/random payment amount
- 10-minute order expiry
- Live 5-second countdown
- Buyer cancel button + /cancel
- `/paid ORDER_ID TX_HASH`
- BSC/BEP-20 USDT transaction verification
- Actual on-chain amount matching with +/- 0.01 USDT tolerance
- Same TX hash cannot be reused
- Automatic delivery after successful verification
- Bulk stock add (20–25+ links)
- Duplicate stock links skipped
- Owner/admin controls
- `/backupstock` exports unsold stock to TXT
- `/stock`, `/stats`, `/setprice`
- SQLite database

Railway:
1. Add these files to your GitHub repo.
2. Set environment variables from `.env.example`.
3. Set Start Command to: `python bot.py` (or use the Procfile worker).
4. Run ONLY ONE instance of this bot for the Telegram token.
5. For persistent stock/orders, attach a Railway Volume and set `DB_FILE=/data/shop.db`.

Security:
- Use a NEW Telegram bot token because the old token was exposed.
- Never commit the real token to GitHub.

Admin commands also include `/pending` for pending payment orders.
