# HTML ID Extractor — OneTrust + Hover Screenshot

Use Python 3.12 on Streamlit Community Cloud.

For each URL the app:
1. Loads the page.
2. Detects the OneTrust consent banner.
3. Clicks OneTrust **Accept All** when available.
4. If no banner exists, continues normally.
5. If a banner exists but Accept All cannot be found, reports:
   `Consent banner detected – Accept All not found`
   and continues extraction.
6. Extracts all HTML IDs beginning with `link_`.
7. Hovers each visible matching element using the real mouse.
8. Captures exactly **one screenshot per element** in the hover state.

No normal screenshots and no context screenshots are generated.
