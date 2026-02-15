# Repository Agent Instructions

## Required Validation
- After any code change, always run:
  - `uv run --env-file .env python -m unittest discover -s tests -v`
- Do not consider the task complete until this command has been executed and results reported.
