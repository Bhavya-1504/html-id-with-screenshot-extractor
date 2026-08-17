# HTML ID Extractor — Exact ID + Overlap-Safe Screenshots

This version fixes a specific mega-menu problem where the correct ID was
selected, but another menu item was painted above it at the same screen
coordinates, causing the screenshot to visually show the wrong element.

For each `link_` ID:
- the exact ID-bearing DOM element is selected and verified
- that exact element is hovered
- foreign elements painted over its center are temporarily hidden
- the exact element is temporarily raised/highlighted
- one screenshot is captured at that element's own rendered dimensions
- all temporary visual changes are immediately restored

OneTrust handling is unchanged.
