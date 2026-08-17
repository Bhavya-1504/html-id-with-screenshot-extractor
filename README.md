# HTML ID Extractor — Depth-First Hover + CSV + Text Footer

Changes in this version:
- CSV no longer contains `section`.
- CSV columns are:
  - id
  - href
  - element_text
  - tag
- Each screenshot still captures the exact ID-bearing element.
- The element text is appended BELOW the screenshot in a separate metadata panel.
- The metadata panel has:
  - a strong divider
  - a border
  - an `ELEMENT TEXT` label
- This makes the extracted text visually distinct from text rendered by the webpage.

OneTrust handling, parent -> child -> grandchild traversal, and hover reset remain unchanged.
