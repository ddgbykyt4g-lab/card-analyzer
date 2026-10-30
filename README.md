# card-analyzer

**On your phone:** [📊 This week's prices](reports/latest.md) · [➕ Log a bet](../../actions/workflows/log-bet.yml)

### Using it from the GitHub app
- **Prices:** open `reports/latest.md` (the link above). It updates every Saturday morning and after each
  bet you log. Each match shows the lowest odds worth taking; take a price at or above them.
- **Log a bet:** Actions → *Log a bet* → *Run workflow*. Type the bet with the odds and stake you got,
  e.g. `Arsenal v Leeds: Leeds over 1.5 @ 2.10 Sky Bet £10`, or paste the research line with your
  odds and stake filled in. Your phone gets "Bet logged" or "Bet NOT logged" within about a minute.
  Tap *Run workflow* once only: a second tap logs the bet twice.
- **Shop bets:** same, with the shop as bookmaker (`Coral shop £10`). If the match has already kicked
  off, fill in *placed* with when you placed it (`2026-10-10 11:30`, UK time).

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
300 scored pre-kickoff projections (the freeze rule in `CLAUDE.md`, which wins if the two ever differ).

## Betfair Exchange prices (read-only)

`.github/workflows/betfair.yml` logs Betfair's card prices every 30 minutes so each bet gets a closing line value
(CLV) against the exchange close: the honest test of whether the model beats the market. It never places bets.

One-off setup:
1. **Delayed app key (free):** logged in to betfair.com, open the Accounts API visualiser
   (developer.betfair.com), run `createDeveloperAppKeys` with any name, and copy the key marked `1.0-DELAY`.
2. **Secrets:** repository Settings → Secrets and variables → Actions → New secret: `BETFAIR_APP_KEY`,
   `BETFAIR_USERNAME`, `BETFAIR_PASSWORD`. If your account has two-step login on, also `BETFAIR_CERT` and
   `BETFAIR_KEY` (a self-signed certificate uploaded under My Account → Security → Automated betting program access).
3. **Test:** Actions → *Betfair prices* → *Run workflow* during the day. If it fails with
   `BETTING_RESTRICTED_LOCATION`, GitHub ran it in the US: add a self-hosted runner on a UK machine and set the
   repository variable `BETFAIR_RUNNER` to its label.
4. After the first runs, check `data/exchange/catalogue.csv`: every card-like market Betfair listed, with how it
   was read. Rows with an empty `parsed` column were skipped.
