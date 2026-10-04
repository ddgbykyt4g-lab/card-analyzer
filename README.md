# card-analyzer

## Saturday routine (free)

1. GitHub Actions runs `.github/workflows/saturday.yml` each Saturday at 07:17 UK (06:17 in winter):
   it updates the data, settles bets, saves the price sheet to `reports/<date>-prices.md` and sends a
   phone alert (needs the `NTFY_TOPIC` repository secret).
2. Open `reports/<date>-prices.md`, paste it into Sonnet or Gemini with web search on, followed by the
   prompt below. Check the source links before relying on any adjustment.
3. Place bets yourself, then log each one with the odds and stake you actually got:
   `python price_sheet.py --db cards.db --log-bet "<line from the research output>"`

### Team-news research prompt

```
You are helping me research yellow/red card bets for today's matches. Do not place bets.

Below is my model's price sheet: for each match, projected cards and the model's
probability for over/under each line. Search the web for each of today's matches and check:
confirmed or likely line-ups, suspensions and injuries to key defensive players,
derby or rivalry status, what's at stake (title, Europe, relegation), the appointed
referee if announced, and tactical match-ups likely to raise or lower fouling.

Rules:
- For each match, keep the model probability or adjust it by at most ±8 percentage
  points, citing the specific evidence with a source link. No evidence, no adjustment.
- Minimum odds = (1.08 - p_push) / p for overs and (1.05 - p_push) / p for unders,
  where p is the adjusted probability and p_push is 0 on half lines.
- Shortlist at most 8 selections with a concrete reason, adjusted p of at least 25%,
  and realistic minimum odds. Flag selections from the same match. If nothing
  qualifies, say "No bet".
- If you can't find information, say so. Don't guess line-ups or referees.

For each selection, output one line in exactly this format:
Home v Away | market | over 3.5 @ <odds> | Sky Bet | model p | adjusted p | reason

[paste price sheet here]
```

Season rules: flat £10 stakes, shortlist only, overs only at about +8-10% EV, model frozen until about
200 settled bets or the end of the season.
