# HTML ID Regex Extractor — Optimized + Screenshots

This version keeps screenshots and focuses only on ID regex input.

Inputs:
- one or more URLs
- ID regex

Outputs:
- screenshots for every matched ID
- CSV with: id, href, element_text, tag
- ZIP containing all screenshots + CSVs

Optimizations:
- one DOM pass for all IDs/metadata
- class mode removed
- no networkidle wait
- hidden-menu hover chain rebuilt only when necessary
- short hover/reset waits
- screenshot progress shown live

Each screenshot includes a clearly separated ELEMENT TEXT panel.
