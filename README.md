# FPL Draft report (Boofla)

Builds a daily report for FPL Draft team 275109: squad health, injuries, fixtures,
best free agents, trade targets, and lineup/waiver advice.

- Runs automatically every morning via GitHub Actions and publishes to this repo's GitHub Pages site.
- To refresh it now: Actions tab → "FPL report" → "Run workflow".
- Scoring weights live in `score()` inside `fpl_report.py`.
