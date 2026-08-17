import asyncio
import re
import shutil
import zipfile
from urllib.parse import urlparse

import streamlit as st

import os
import subprocess
from pathlib import Path

# Streamlit Cloud installs the Python Playwright package, but the browser
# binary is a separate download. Install Chromium once at startup if needed.
def ensure_playwright_browser():
    browser_cache = Path.home() / ".cache" / "ms-playwright"
    browser_cache.mkdir(parents=True, exist_ok=True)

    # Check whether Playwright can find an installed Chromium executable.
    try:
        result = subprocess.run(
            ["playwright", "install", "chromium"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr[-2000:])
    except Exception as e:
        st.warning(
            "Playwright browser installation failed. "
            "The app may not be able to capture screenshots: " + str(e)
        )

from playwright.async_api import async_playwright

OUTPUT_DIR = Path("/tmp/html_id_extractor")

ensure_playwright_browser()


def safe_name(value, max_length=100):
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", (value or "").strip())
    return value[:max_length].strip("._") or "element"

def normalize_url(url):
    url = url.strip()
    if not url:
        return ""
    return url if re.match(r"^https?://", url, re.I) else "https://" + url

async def wait_for_page(page):
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    await page.wait_for_timeout(1000)

async def capture_states(page, element, element_id, folder, index):
    sid = safe_name(element_id)
    normal = folder / f"{index:03d}_{sid}_normal.png"
    hover = folder / f"{index:03d}_{sid}_hover.png"
    context = folder / f"{index:03d}_{sid}_hover_context.png"

    # Freeze CSS animations/transitions without changing the element itself.
    await page.add_style_tag(content="""
        *, *::before, *::after {
            animation-duration: 0s !important;
            animation-delay: 0s !important;
            transition-duration: 0s !important;
            transition-delay: 0s !important;
            scroll-behavior: auto !important;
        }
    """)

    try:
        await element.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    await page.wait_for_timeout(400)

    if not await element.is_visible():
        return None

    # Normal state.
    await page.mouse.move(1, 1)
    await page.wait_for_timeout(250)
    try:
        await element.screenshot(path=str(normal), animations="disabled")
    except Exception:
        normal = None

    # REAL mouse hover. This triggers CSS :hover and JS mouseenter behavior.
    try:
        await element.hover(force=True, timeout=10000)
    except Exception:
        box = await element.bounding_box()
        if box:
            await page.mouse.move(
                box["x"] + box["width"] / 2,
                box["y"] + box["height"] / 2
            )
    await page.wait_for_timeout(700)

    try:
        await element.screenshot(path=str(hover), animations="disabled")
    except Exception:
        hover = None

    # Context screenshot while hover is still active.
    try:
        await page.evaluate("""
            (id) => {
                const el = document.getElementById(id);
                if (!el) return;
                el.setAttribute("data-id-extractor-highlight", "1");
                const s = document.createElement("style");
                s.id = "id-extractor-highlight-style";
                s.textContent = `
                    [data-id-extractor-highlight="1"] {
                        outline: 4px solid red !important;
                        outline-offset: 3px !important;
                    }
                `;
                document.head.appendChild(s);
            }
        """, element_id)
        await page.wait_for_timeout(100)
        await page.screenshot(path=str(context), full_page=False)
    except Exception:
        context = None

    # Remove highlight after capture.
    try:
        await page.evaluate("""
            () => {
                document.querySelectorAll(
                    '[data-id-extractor-highlight="1"]'
                ).forEach(e => e.removeAttribute("data-id-extractor-highlight"));
                const s = document.getElementById("id-extractor-highlight-style");
                if (s) s.remove();
            }
        """)
    except Exception:
        pass

    return {
        "normal": str(normal) if normal and normal.exists() else "",
        "hover": str(hover) if hover and hover.exists() else "",
        "context": str(context) if context and context.exists() else ""
    }

async def process_url(browser, url, index):
    parsed = urlparse(url)
    folder = OUTPUT_DIR / safe_name(
        f"{index}_{parsed.netloc}_{parsed.path.strip('/') or 'homepage'}"
    )
    folder.mkdir(parents=True, exist_ok=True)
    txt = folder / "extracted_ids.txt"

    page = await browser.new_page(
        viewport={"width": 1440, "height": 1000},
        device_scale_factor=1
    )
    results = []

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await wait_for_page(page)

        locator = page.locator('[id^="link_"]')
        count = await locator.count()

        for i in range(count):
            el = locator.nth(i)
            try:
                eid = await el.get_attribute("id")
                if not eid:
                    continue
                tag = await el.evaluate("(e) => e.tagName.toLowerCase()")
                try:
                    text = re.sub(r"\s+", " ", await el.inner_text()).strip()
                except Exception:
                    text = ""
                href = await el.get_attribute("href")
                visible = await el.is_visible()

                shots = await capture_states(
                    page, el, eid, folder, i + 1
                ) if visible else None

                results.append({
                    "id": eid,
                    "tag": tag,
                    "text": text,
                    "href": href or "",
                    "visible": visible,
                    "normal": shots["normal"] if shots else "",
                    "hover": shots["hover"] if shots else "",
                    "context": shots["context"] if shots else ""
                })
            except Exception as e:
                results.append({
                    "id": "",
                    "tag": "",
                    "text": "",
                    "href": "",
                    "visible": False,
                    "normal": "",
                    "hover": "",
                    "context": "",
                    "error": type(e).__name__
                })

        with open(txt, "w", encoding="utf-8") as f:
            for r in results:
                if r["id"]:
                    f.write(r["id"] + "\n")

        return {"url": url, "folder": folder, "txt": txt, "results": results, "error": ""}
    except Exception as e:
        return {"url": url, "folder": folder, "txt": txt, "results": [], "error": f"{type(e).__name__}: {e}"}
    finally:
        await page.close()

async def extract_all(urls, progress):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        results = []
        try:
            for i, url in enumerate(urls, 1):
                results.append(await process_url(browser, url, i))
                progress(i / len(urls), url)
        finally:
            await browser.close()
        return results

def make_zip(results):
    path = OUTPUT_DIR / "HTML_ID_Extractor_Hover_Screenshots.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for r in results:
            if Path(r["folder"]).exists():
                for f in Path(r["folder"]).rglob("*"):
                    if f.is_file():
                        z.write(f, f.relative_to(OUTPUT_DIR))
    return path

st.set_page_config(page_title="HTML ID Extractor", page_icon="🔎", layout="wide")
st.title("🔎 HTML ID Extractor")
st.write(
    "Extracts every ID beginning with `link_` and captures the normal state, "
    "real mouse-hover state, and highlighted page context."
)

urls_text = st.text_area("URLs — one per line", height=180)
run = st.button("🔎 Extract IDs & Capture Hover Screenshots", type="primary", use_container_width=True)

if run:
    urls = list(dict.fromkeys(
        normalize_url(x) for x in urls_text.splitlines() if x.strip()
    ))
    if not urls:
        st.warning("Please enter at least one URL.")
        st.stop()

    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True)

    progress = st.progress(0)
    status = st.empty()

    def update(value, url):
        progress.progress(value)
        status.write(f"Processing: {url}")

    try:
        results = asyncio.run(extract_all(urls, update))
    except Exception as e:
        st.error(f"Extraction failed: {type(e).__name__}: {e}")
        st.stop()

    progress.empty()
    status.empty()

    total = sum(len(r["results"]) for r in results)
    st.success(f"Completed {len(results)} URL(s). Found {total} matching element(s).")

    zip_path = make_zip(results)
    with open(zip_path, "rb") as f:
        st.download_button(
            "⬇️ Download All Screenshots + TXT Files",
            f.read(), file_name=zip_path.name, mime="application/zip",
            use_container_width=True
        )

    for ui, result in enumerate(results, 1):
        st.divider()
        st.header(f"URL {ui}")
        st.code(result["url"])

        if result["error"]:
            st.error(result["error"])
            continue

        with open(result["txt"], "rb") as f:
            st.download_button(
                f"⬇️ Download IDs — URL {ui}", f.read(),
                file_name=f"url_{ui}_extracted_ids.txt",
                mime="text/plain", key=f"txt_{ui}"
            )

        if not result["results"]:
            st.info("No IDs beginning with `link_` were found.")
            continue

        for n, item in enumerate(result["results"], 1):
            st.subheader(f"{n}. `{item['id']}`")
            st.write(
                f"**Tag:** `{item['tag']}` | "
                f"**Visible:** `{item['visible']}`"
            )
            if item["text"]:
                st.write(f"**Text:** {item['text']}")
            if item["href"]:
                st.write(f"**Href:** `{item['href']}`")

            c1, c2, c3 = st.columns(3)
            for col, label, key in [
                (c1, "Normal state", "normal"),
                (c2, "Hover state", "hover"),
                (c3, "Hover + page context", "context")
            ]:
                with col:
                    st.markdown(f"**{label}**")
                    p = item[key]
                    if p and Path(p).exists():
                        st.image(p, use_container_width=True)
                        with open(p, "rb") as f:
                            st.download_button(
                                "Download", f.read(),
                                file_name=Path(p).name, mime="image/png",
                                key=f"{key}_{ui}_{n}"
                            )
                    else:
                        st.info("Unavailable")
