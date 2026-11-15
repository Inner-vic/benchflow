#!/bin/sh
set -eu

LOG_FILE=/app/livc-test/sw0055-2723-replay-20260917-001/logs/candidate.stdout.log
printf 'candidate_wrapper_started pid=%s\n' "$$" >> "$LOG_FILE"
exec /app/livc/bin/infer_expert_pcm >> "$LOG_FILE" 2>&1
