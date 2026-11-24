# RUN.md — quick commands

Run from the app directory:

```bash
cd ~/GitHub/automated_job_application/claude_applicator
```

| Command | What it does |
|---------|--------------|
| `./watch.sh` | Live run with the two-panel dashboard. |
| `./watch.sh prepare` | Dry-run — fills everything, stops at Submit. |
| `./watch.sh attach` | Dashboard on an already-running run (e.g. the 05:01 scheduled one). Ctrl-C closes the view, not the run. |
| `pkill -f "claude -p Run the LinkedIn auto-apply routine"` | **Stop a running run.** Needed after `attach` — Ctrl-C there only closes the dashboard, the run keeps going. This pattern hits only the apply agent (never your interactive `claude` session); the wrapper, `caffeinate`, and MCP servers wind down with it, Chrome stays up. |
| `tail -f "$(ls -t output/scheduled_*.log \| head -1)"` | Follow the latest scheduled run's wrapper log. |
| `cat output/observed_<ts>.log` | Read a finished run's reasoning + actions as flat text. |
| `./db_report.sh` | Print the DB summary + refresh `applications_report.txt`. |
| `./db_to_csv.sh` | Export the DB to `applications.csv` and open it in Numbers/Excel. |
| `cat output/run_<ts>.log` | Read a run's own applied/skipped summary. |
