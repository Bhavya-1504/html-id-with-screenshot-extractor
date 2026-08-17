import asyncio
import os
import re
import shutil
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import nest_asyncio
import streamlit as st
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

nest_asyncio.apply()

# Ensure Playwright's Chromium browser is available in the deployment environment.
# This runs once at app startup if Chromium has not already been installed.
try:
    import subprocess
    subprocess.run(['playwright', 'install', 'chromium'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
except Exception:
    pass

APP_DIR = Path(__file__).parent if "__file__" in globals() else Path(".")
OUTPUT_DIR = APP_DIR / "html_id_output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def safe_name(value, max_length=120):
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return value[:max_length].strip("._") or "item"


def normalize_url(url):
    url = url.strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def url_folder_name(url, index):
    parsed = urlparse(url)
    host = safe_name(parsed.netloc or "unknown_host")
    path = safe_name(parsed.path.strip("/") or "homepage")
    return f"{index:03d}_{host}_{path}"


async def extract_from_url(browser, url, index):
    folder = OUTPUT_DIR / url_folder_name(url, index)
    screenshot_dir = folder / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)

    txt_path = folder / "extracted_ids.txt"

    page = await browser.new_page(
        viewport={"width": 1440, "height": 1000},
        device_scale_factor=1
    )

    results = []

    try:
        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000
        )

        # Give client-side rendered pages a chance to populate.
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except PlaywrightTimeoutError:
            pass

        locator = page.locator('[id^="link_"]')
        count = await locator.count()

        for i in range(count):
            element = locator.nth(i)

            try:
                element_id = await element.get_attribute("id")
                tag_name = await element.evaluate("(el) => el.tagName.toLowerCase()")
                text = (await element.inner_text(timeout=5000)).strip()
                href = await element.get_attribute("href")

                # Make sure the element is in the viewport before capturing it.
                try:
                    await element.scroll_into_view_if_needed(timeout=5000)
                except Exception:
                    pass

                # Screenshot filename.
                screenshot_name = (
                    f"{i+1:03d}_{safe_name(element_id or 'link')}.png"
                )
                screenshot_path = screenshot_dir / screenshot_name

                screenshot_status = "Captured"

                try:
                    # Check visibility first.
                    visible = await element.is_visible()
                    if not visible:
                        screenshot_status = "Unavailable - element hidden"
                    else:
                        await element.screenshot(
                            path=str(screenshot_path),
                            animations="disabled",
                            timeout=10000
                        )
                except Exception as screenshot_error:
                    screenshot_status = f"Unavailable - {type(screenshot_error).__name__}"

                results.append({
                    "id": element_id or "",
                    "tag": tag_name or "",
                    "text": re.sub(r"\s+", " ", text),
                    "href": href or "",
                    "screenshot": str(screenshot_path) if screenshot_path.exists() else "",
                    "screenshot_status": screenshot_status
                })

            except Exception as element_error:
                results.append({
                    "id": "",
                    "tag": "",
                    "text": "",
                    "href": "",
                    "screenshot": "",
                    "screenshot_status": f"Element extraction failed - {type(element_error).__name__}"
                })

        # Preserve the original simple TXT output concept:
        # one extracted ID per line.
        with open(txt_path, "w", encoding="utf-8") as f:
            for item in results:
                if item["id"]:
                    f.write(item["id"] + "\n")

        return {
            "url": url,
            "folder": folder,
            "txt_path": txt_path,
            "results": results,
            "error": None
        }

    except Exception as e:
        return {
            "url": url,
            "folder": folder,
            "txt_path": txt_path,
            "results": [],
            "error": f"{type(e).__name__}: {e}"
        }

    finally:
        await page.close()


async def process_urls(urls):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage"
            ]
        )

        all_results = []

        try:
            for index, url in enumerate(urls, start=1):
                result = await extract_from_url(browser, url, index)
                all_results.append(result)
        finally:
            await browser.close()

        return all_results


def create_zip(all_results):
    zip_path = OUTPUT_DIR / "HTML_ID_Extractor_Screenshots.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for result in all_results:
            folder = Path(result["folder"])

            if folder.exists():
                for file_path in folder.rglob("*"):
                    if file_path.is_file():
                        z.write(
                            file_path,
                            arcname=file_path.relative_to(OUTPUT_DIR)
                        )

    return zip_path


st.set_page_config(
    page_title="HTML ID Extractor",
    page_icon="🔎",
    layout="wide"
)

st.title("🔎 HTML ID Extractor")
st.write(
    "Paste one or more URLs below. The app extracts every HTML ID "
    "beginning with `link_` and captures a screenshot of each matching element."
)

urls_text = st.text_area(
    "URLs",
    height=180,
    placeholder=(
        "https://example.com/page-1\n"
        "https://example.com/page-2"
    )
)

extract_button = st.button(
    "🔎 Extract IDs & Capture Screenshots",
    type="primary",
    use_container_width=True
)

if extract_button:
    raw_urls = [
        normalize_url(line)
        for line in urls_text.splitlines()
        if line.strip()
    ]

    # Remove duplicates while preserving order.
    urls = list(dict.fromkeys(raw_urls))

    if not urls:
        st.warning("Please enter at least one URL.")
        st.stop()

    # Clear previous output.
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    progress = st.progress(0)
    status = st.empty()

    try:
        # Process one URL at a time so the UI remains predictable and
        # each URL gets its own output folder.
        results = []

        async def process_with_progress():
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage"
                    ]
                )

                try:
                    for index, url in enumerate(urls, start=1):
                        status.write(f"Processing {index}/{len(urls)}: {url}")
                        result = await extract_from_url(browser, url, index)
                        results.append(result)
                        progress.progress(index / len(urls))
                finally:
                    await browser.close()

            return results

        results = asyncio.get_event_loop().run_until_complete(
            process_with_progress()
        )

    except Exception as e:
        st.error(f"Processing failed: {type(e).__name__}: {e}")
        st.stop()

    status.empty()

    total_ids = sum(len(r["results"]) for r in results)

    st.success(
        f"Completed {len(results)} URL(s). Found {total_ids} "
        f"element(s) with IDs beginning with `link_`."
    )

    # Download all screenshots/TXT files.
    zip_path = create_zip(results)

    with open(zip_path, "rb") as f:
        st.download_button(
            "⬇️ Download All Screenshots + TXT Files (ZIP)",
            data=f.read(),
            file_name="HTML_ID_Extractor_Screenshots.zip",
            mime="application/zip",
            use_container_width=True
        )

    st.divider()

    for url_index, result in enumerate(results, start=1):
        st.header(f"URL {url_index}")
        st.code(result["url"])

        if result["error"]:
            st.error(result["error"])
            continue

        items = result["results"]

        if not items:
            st.info("No HTML IDs beginning with `link_` were found.")
            continue

        # TXT download for this URL.
        with open(result["txt_path"], "rb") as f:
            txt_data = f.read()

        st.download_button(
            f"⬇️ Download IDs for URL {url_index}",
            data=txt_data,
            file_name="extracted_ids.txt",
            mime="text/plain",
            key=f"txt_download_{url_index}"
        )

        st.write(f"**Found {len(items)} matching element(s)**")

        for item_index, item in enumerate(items, start=1):
            st.subheader(
                f"{item_index}. `{item['id']}`"
            )

            col1, col2 = st.columns([1, 2])

            with col1:
                st.write(f"**Tag:** `{item['tag']}`")

                if item["text"]:
                    st.write(f"**Text:** {item['text']}")
                else:
                    st.write("**Text:** *(empty)*")

                if item["href"]:
                    st.write(f"**Href:** `{item['href']}`")

                st.write(
                    f"**Screenshot:** {item['screenshot_status']}"
                )

            with col2:
                if item["screenshot"] and Path(item["screenshot"]).exists():
                    st.image(
                        item["screenshot"],
                        caption=item["id"],
                        use_container_width=True
                    )

                    with open(item["screenshot"], "rb") as image_file:
                        st.download_button(
                            "⬇️ Download Screenshot",
                            data=image_file.read(),
                            file_name=Path(item["screenshot"]).name,
                            mime="image/png",
                            key=f"img_{url_index}_{item_index}"
                        )
                else:
                    st.warning(
                        "A screenshot could not be captured for this element."
                    )

            st.divider()
