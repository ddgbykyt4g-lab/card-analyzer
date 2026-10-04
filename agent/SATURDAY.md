# Saturday card shortlist — runbook for the scheduled agent

You run unattended in a fresh cloud container. Follow these steps in order and keep going unless a step
says to stop. Everything in `CLAUDE.md` applies. Above all: **never place bets or contact a bookmaker** —
you produce a shortlist; the user decides and places bets.

## 1. Set up
0. A fresh checkout is on a detached HEAD, where `git push origin main` silently pushes nothing. First run:
   `git fetch origin main && git checkout -B main origin/main`
1. Attach and clone the repo: `add_repo` owner `ddgbykyt4g-lab`, repo `card-analyzer`, access `push`,
   then clone it as the tool instructs and `cd` into the clone. Work only there.
2. Python environment:
   ```
   python3 -m venv .venv && . .venv/bin/activate && pip install -q -r requirements.txt
   ```
3. Let Chromium trust the session proxy (needed for any page the collector renders):
   ```
   which certutil || (apt-get update -qq && apt-get install -y -qq libnss3-tools)
   mkdir -p ~/.pki/nssdb && (certutil -d sql:$HOME/.pki/nssdb -L >/dev/null 2>&1 || certutil -d sql:$HOME/.pki/nssdb -N --empty-password)
   certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n agent-proxy-ca -i /root/.ccr/agent-proxy-ca.crt
   ```
   Do not run `playwright install`; `requirements.txt` pins the version that matches the preinstalled Chromium.

## 2. Update data and settle
Season label: August–December of year Y → `Y-(Y+1 last two digits)` (e.g. `2026-27`); January–July → `(Y-1)-YY`.
```
python collect_cards.py --db cards.db --season <label> --no-cups
python settle.py --db cards.db --report
```
If the collector logs unmatched team names, fix them with `name_aliases` rows (see `CLAUDE.md`), then rerun.
Keep the `--report` output for the summary.

## 3. Price today's matches
```
python price_sheet.py --db cards.db --days 2 --json > /tmp/sheet.json
python price_sheet.py --db cards.db --days 2
```
Use only matches kicking off **today (UK date)**. If there are none, skip to step 6 and say so.

## 4. Research what the model can't see
For each of today's matches, search the web (team news sites, club sites, league referee appointments) for:
confirmed or likely line-ups, suspensions and injuries to key defensive players, derby/rivalry status,
match stakes (title, Europe, relegation), the appointed referee if announced, and tactical mismatches likely
to raise or lower fouling.

Then, per match, either keep the model probability or adjust it by **at most ±8 percentage points**, citing
the specific evidence. No evidence → no adjustment. Don't invent precision.

## 5. Build the shortlist
There is no live odds feed yet, so value is expressed as **the minimum price worth taking**.
- Consider match totals (`lines`) and team lines (`team_lines`) from `/tmp/sheet.json`.
- For a candidate, take the adjusted probability `p` and compute minimum odds for +5% EV: `(1.05 - p_push) / p`
  (`p_push` is the sheet's push probability, 0 on half lines; for whole-number match lines use the sheet's
  push-aware `min_over`/`min_under` and adjust only if you changed the probability).
- Shortlist at most 8 selections where the research gives a concrete reason to back that side, the adjusted
  probability is at least 25%, and the minimum odds are realistic for that market. Flag correlated selections
  (same match, or same team across markets). If nothing qualifies, the shortlist is "No bet".
- Provisional matches (no referee) are allowed but must be labelled.

## 6. Write the report and save
Write `reports/<YYYY-MM-DD>.md` containing:
- A shortlist table: kickoff, fixture, market, selection, model p, adjusted p, **take it only at or above
  these odds**, confidence (low/medium/high), one-line reason with sources.
- Under each selection, a ready-to-log line in this exact form for the user to complete with the odds and
  stake they actually get:
  `python price_sheet.py --db cards.db --log-bet "Home v Away: Team over 2.5 @ <odds> Sky Bet £<stake>" --adjusted-p <p> --reason "<reason>"`
  (match totals: drop the `Team` part, e.g. `"Home v Away over 3.5 @ <odds> ..."`).
- The track record from `settle.py --report`.
- Coverage gaps: leagues or matches not researched, missing referees, missing 1X2 prices.

Then commit `cards.db` and the report and push:
```
git add cards.db reports/ && git commit -m "Saturday shortlist <date>" && git fetch origin main && git rebase origin/main && git push origin HEAD:main
git fetch origin main && git status -sb | head -1   # must show main in sync with origin/main
```
(add the attribution lines your environment requires to the commit message).

## 7. Alert and final message
After the report is pushed (never before: the report is the record), send the phone alert:
```
agent/notify.sh "Cards <date>: <N> picks" "<summary>"
```
`<summary>` is plain text under about 900 characters: matches screened, then one line per shortlisted
selection (fixture, selection, minimum odds, a few words of reason) or "No bet", then last week's settled
result and any problems. Put the most important thing first. If the script reports that the alert failed or
`NTFY_TOPIC` isn't set, say so in your final message; do not try other ways to contact the user.

Then give the same summary as your final message. Remind them: nothing has been placed; log what they
actually bet with the command in the report.
