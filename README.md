# HTML Attribute Regex Extractor

The user can choose whether to extract matching elements by:
- ID
- Class

Then enter any regex.

Examples:
- ID: `^link_`
- ID: `^link_navdd`
- Class: `sub-menu__item`
- Class: `^btn-`

Class regex is tested against the full class attribute string.

Output CSV columns:
- `id` or `class`
- `href`
- `element_text`
- `tag`

The app keeps OneTrust handling, depth-first traversal, hover reset, screenshots,
and the clearly separated ELEMENT TEXT panel in each image.
