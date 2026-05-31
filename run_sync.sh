#!/usr/bin/env bash
# Auto-sync script. Point cron / Task Scheduler at this file.
# Edit the path below to wherever you put the project.
cd "$(dirname "$0")" || exit 1
python3 diavgeia_sync.py --db gavdos.db >> sync.log 2>&1
