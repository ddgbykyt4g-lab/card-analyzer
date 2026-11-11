#!/usr/bin/env python3
"""Queue for the "Log a bet" workflow, so no bet is lost when runs overlap.

GitHub keeps one waiting run per concurrency group and cancels it when a newer one arrives, so a bet sent while
two other runs hold or wait for cards.db used to vanish without an alert. Now the workflow's first job (outside
that group) only commits the bet as a file under data/bet_queue/; the second job (in the cards-db group) logs
every queued bet. A waiting run that gets cancelled loses nothing: the run that replaced it logs its bet too.

  add DIR              write one queued bet from the environment (BET, PLACED, ADJ, REASON, RUN_ID). A bet with
                       no placement time gets the time it was queued, so a run that waits past kickoff still
                       finds the match and prices it as of then.
  run DIR OUT          log every queued bet with price_sheet.py --log-bet (oldest first), remove its file, and
                       write the outcomes to OUT (JSON) for the alert.
  notify OUT STATUS    one phone alert per bet: logged, or not logged and why. STATUS is the job status.
Never places a bet: it only records bets the user reports.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
UK = ZoneInfo("Europe/London")


def add(queue: Path, env=os.environ, now: datetime | None = None) -> Path:
    now = now or datetime.now(timezone.utc)
    bet = (env.get("BET") or "").strip()
    if not bet:
        raise SystemExit("No bet given")
    item = dict(bet=bet, placed=(env.get("PLACED") or "").strip() or f"{now.astimezone(UK):%Y-%m-%d %H:%M}",
                adjusted_p=(env.get("ADJ") or "").strip(), reason=(env.get("REASON") or "").strip(),
                queued_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    queue.mkdir(parents=True, exist_ok=True)
    path = queue / f"{now:%Y%m%dT%H%M%S}-{env.get('RUN_ID') or os.getpid()}.json"
    path.write_text(json.dumps(item, indent=1) + "\n")
    return path


def command(item: dict, db: str) -> list[str]:
    args = [sys.executable, str(ROOT / "price_sheet.py"), "--db", db, "--log-bet", item["bet"], "--placed", item["placed"]]
    if item.get("adjusted_p"):
        args += ["--adjusted-p", item["adjusted_p"]]
    if item.get("reason"):
        args += ["--reason", item["reason"]]
    return args


def run(queue: Path, out: Path, db: str = "cards.db", runner=subprocess.run) -> list[dict]:
    results = []
    for path in sorted(queue.glob("*.json")):
        item = json.loads(path.read_text())
        proc = runner(command(item, db), capture_output=True, text=True)
        results.append(dict(bet=item["bet"], ok=proc.returncode == 0,
                            output=(proc.stdout + proc.stderr).strip()))
        path.unlink()  # logged, or rejected with the reason in the alert: either way it's done
    out.write_text(json.dumps(results, indent=1))
    for r in results:
        print(("LOGGED: " if r["ok"] else "NOT LOGGED: ") + r["bet"] + "\n" + r["output"] + "\n")
    print(f"{len(results)} queued bet(s) processed")
    return results


def notify(out: Path, status: str) -> None:
    results = json.loads(out.read_text()) if out.exists() else []
    if not results and status != "success":  # failed before any bet was logged: they're all still queued
        results = [dict(bet="Your queued bet(s)", ok=False, output="")]
    for r in results:
        if status != "success":
            title, body = "Bet NOT logged yet", (f"{r['bet']}\nThe run failed before saving. The bet is still "
                                                 "queued: open the run in the Actions tab and choose Re-run failed jobs.")
        elif r["ok"]:
            title, body = "Bet logged", r["output"]
        else:
            title, body = "Bet NOT logged", f"{r['output'].splitlines()[-1] if r['output'] else r['bet']}\n" \
                                            "Check the wording and send it again."
        subprocess.run([str(ROOT / "agent" / "notify.sh"), title, body], check=False)


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "add" and len(sys.argv) == 3:
        print(add(Path(sys.argv[2])))
    elif cmd == "run" and len(sys.argv) in (4, 5):
        run(Path(sys.argv[2]), Path(sys.argv[3]), *sys.argv[4:])
    elif cmd == "notify" and len(sys.argv) == 4:
        notify(Path(sys.argv[2]), sys.argv[3])
    else:
        raise SystemExit(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
