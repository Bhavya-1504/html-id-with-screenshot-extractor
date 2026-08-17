# HTML ID Extractor — Text Included in Screenshot

For each `link_` ID the app now:
- extracts the exact ID-bearing element
- extracts that element's text, if any
- shows the text in Streamlit as **Element text**
- hovers the exact element
- captures one screenshot at that element's own dimensions
- highlights the exact ID-bearing element
- appends a compact text strip to the saved screenshot containing the
  extracted element text

If an element has no text, the image label says `(no visible text)`.

OneTrust consent handling remains unchanged.
