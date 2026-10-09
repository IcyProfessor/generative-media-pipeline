# v0.2 migration fixture, not a v0.3 job

Preserved synthetic A-F artifacts from the source v0.2 archive. The WAV files
contain generated tones, NOT speech or singing. The old `lyrics`, `label` and
`timestamped_words` schemas are intentional: `tests/test_migration.py` exercises
conversion into a new replay-only v0.3 JobStore. Do not run `resume` here.

The fixture is not a YouTube production result, model evaluation or benchmark.
Media IDs resolve against `src/genmedia/data/assets/demo/manifest.json`.
For a fresh current-format job, run `python -m genmedia demo`.
