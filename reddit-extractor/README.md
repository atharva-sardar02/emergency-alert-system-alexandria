# Reddit Extractor (Alexandria EAS)

Pulls recent posts from local subreddits and filters for emergency-related signals.
- OAuth support via `.env` (`REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT`)
- `/new` listing + local keyword filter, optional comment scan, EAS scoring
- Progress logs, rate-limit handling, CSV output

## Quickstart
pip install -r requirements.txt

Put creds in reddit-extractor/.env (never commit this file)
python -u reddit_extractor.py --hours 24 --max_per_sub 150 --verbose --out data/alx_reddit.csv

# Alexandria EAS — Reddit Harvester

This small script fetches recent Reddit posts from local subreddits and filters them for emergency-related keywords.

## Quick start

```bash
python reddit_extractor.py --hours 24 --out data/alx_reddit.csv
```

No API keys are required in the default mode (public JSON endpoints). Be respectful of rate limits.

## Subreddits
- r/AlexandriaVA
- r/nova
- r/ArlingtonVA
- r/washingtondc

## Output fields
`id, created_utc, created_iso, subreddit, author, title, selftext, url, permalink, score, num_comments, matched_keywords, high_priority, eas_score`

## Notes
- Heuristic `eas_score` bubbles probable incidents to the top.
- For official API (PRAW), set env vars `REDDIT_CLIENT_ID/SECRET/USER_AGENT` and adapt the script if desired.
- Consider cron: `*/10 * * * * /usr/bin/python3 reddit_extractor.py --hours 2 --out /var/data/alx_reddit.csv`.
