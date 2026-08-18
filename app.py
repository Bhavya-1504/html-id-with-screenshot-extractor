
import asyncio
import csv
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import streamlit as st
from playwright.async_api import async_playwright

OUTPUT_DIR = Path("/tmp/html_id_regex_extractor")


def safe_name(value, max_length=120):
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", (value or "").strip())
    return value[:max_length].strip("._") or "element"


def normalize_url(url):
    url = url.strip()
    if not url:
        return ""
    return url if re.match(r"^https?://", url, re.I) else "https://" + url


def ensure_playwright_browser():
    try:
        subprocess.run(
            ["playwright", "install", "chromium"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=300,
        )
    except Exception:
        pass


async def handle_onetrust_consent(page):
    # Fast path for standard OneTrust.
    try:
        btn = page.locator("#onetrust-accept-btn-handler").first
        if await btn.is_visible(timeout=1800):
            await btn.click(timeout=4000)
            await page.wait_for_timeout(250)
            return "OneTrust Accept All clicked"
    except Exception:
        pass

    banner_found = False
    for selector in [
        "#onetrust-banner-sdk",
        "#onetrust-consent-sdk",
        "[id*='onetrust-banner']",
    ]:
        try:
            if await page.locator(selector).first.is_visible(timeout=400):
                banner_found = True
                break
        except Exception:
            pass

    if not banner_found:
        return "No OneTrust consent banner detected"

    for selector in [
        "button:has-text('Accept All')",
        "button:has-text('Accept all')",
        "button:has-text('Accept Cookies')",
        "button:has-text('Allow All')",
    ]:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=400):
                await btn.click(timeout=3000)
                await page.wait_for_timeout(250)
                return "OneTrust Accept All clicked"
        except Exception:
            pass

    return "Consent banner detected – Accept All not found"


async def collect_matching_ids(page, regex_pattern):
    """
    One DOM pass:
      - depth-first order
      - ID regex match
      - metadata
      - stable DOM path
      - matching ancestor paths for hover menus
    """
    return await page.evaluate(
        """
        (regexPattern) => {
            let rx;
            try {
                rx = new RegExp(regexPattern);
            } catch (e) {
                return { regex_error: e.message, results: [] };
            }

            const results = [];

            function cleanText(el) {
                return (el.innerText || el.textContent || "")
                    .replace(/\\s+/g, " ")
                    .trim();
            }

            function domPath(el) {
                const parts = [];
                let cur = el;

                while (cur && cur.nodeType === Node.ELEMENT_NODE) {
                    const parent = cur.parentElement;

                    if (!parent) {
                        parts.unshift(cur.tagName.toLowerCase());
                        break;
                    }

                    const siblings = Array.from(parent.children);
                    const idx = siblings.indexOf(cur) + 1;

                    parts.unshift(
                        cur.tagName.toLowerCase() + ":nth-child(" + idx + ")"
                    );
                    cur = parent;
                }

                return parts.join(" > ");
            }

            function visit(node, matchingAncestorPaths) {
                if (!node || node.nodeType !== Node.ELEMENT_NODE) return;

                const id = node.id || "";
                rx.lastIndex = 0;
                const isMatch = id && rx.test(id);
                rx.lastIndex = 0;

                let nextAncestors = matchingAncestorPaths;

                if (isMatch) {
                    const path = domPath(node);
                    const chain = [...matchingAncestorPaths, path];

                    results.push({
                        id,
                        href: node.getAttribute("href") || "",
                        element_text: cleanText(node),
                        tag: node.tagName.toLowerCase(),
                        dom_path: path,
                        hover_chain: chain
                    });

                    nextAncestors = chain;
                }

                for (const child of node.children) {
                    visit(child, nextAncestors);
                }
            }

            visit(document.documentElement, []);
            return { regex_error: "", results };
        }
        """,
        regex_pattern,
    )


async def close_hover(page):
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass

    try:
        vp = page.viewport_size or {"width": 1440, "height": 1000}
        await page.mouse.move(vp["width"] - 4, vp["height"] - 4)
    except Exception:
        pass

    await page.wait_for_timeout(60)


async def reveal_chain(page, hover_chain):
    """
    Only needed for hidden dropdown/menu items.
    """
    for path in hover_chain:
        loc = page.locator(path).first

        try:
            box = await loc.bounding_box()
        except Exception:
            box = None

        if not box:
            try:
                await loc.hover(force=True, timeout=1800)
            except Exception:
                return False
        else:
            try:
                await page.mouse.move(
                    box["x"] + box["width"] / 2,
                    box["y"] + box["height"] / 2,
                )
            except Exception:
                return False

        await page.wait_for_timeout(70)

    return True


def add_text_panel(raw_path, final_path, element_text):
    from PIL import Image, ImageDraw, ImageFont
    import textwrap

    img = Image.open(raw_path).convert("RGB")
    img_w, img_h = img.size

    text_value = (element_text or "").strip() or "(no visible text)"
    font = ImageFont.load_default()

    final_w = max(img_w, 260)
    chars = max(24, min(90, int((final_w - 24) / 7)))
    wrapped = textwrap.wrap(text_value, width=chars) or [text_value]

    pad = 10
    divider_h = 2
    label_h = 18
    line_h = 16
    panel_h = pad + label_h + 8 + line_h * len(wrapped) + pad

    final = Image.new("RGB", (final_w, img_h + divider_h + panel_h), "white")
    final.paste(img, ((final_w - img_w) // 2, 0))

    draw = ImageDraw.Draw(final)
    draw.rectangle(
        [(0, img_h), (final_w - 1, img_h + divider_h - 1)],
        fill=(0, 0, 0),
    )

    panel_top = img_h + divider_h
    draw.rectangle(
        [(0, panel_top), (final_w - 1, final.height - 1)],
        outline=(90, 90, 90),
        width=1,
    )

    y = panel_top + pad
    draw.rectangle(
        [(pad, y), (pad + 95, y + 18)],
        outline=(90, 90, 90),
        width=1,
    )
    draw.text((pad + 5, y + 3), "ELEMENT TEXT", fill=(0, 0, 0), font=font)

    y += 26
    for line in wrapped:
        draw.text((pad, y), line, fill=(0, 0, 0), font=font)
        y += line_h

    final.save(final_path, format="PNG")
    img.close()

    try:
        raw_path.unlink()
    except Exception:
        pass


async def capture_screenshot(page, item, folder, index):
    """
    Optimized screenshot path:
      - visible elements: direct hover + screenshot
      - hidden elements: reset + reveal only required parent chain
      - one screenshot per ID
      - element text panel appended below image
    """
    element_id = item["id"]
    raw_path = folder / f"{index:03d}_{safe_name(element_id)}__raw.png"
    final_path = folder / f"{index:03d}_{safe_name(element_id)}.png"

    element = page.locator(item["dom_path"]).first

    try:
        if (await element.get_attribute("id") or "") != element_id:
            return ""

        visible = False
        try:
            visible = await element.is_visible()
        except Exception:
            pass

        if not visible:
            await close_hover(page)

            if not await reveal_chain(
                page,
                item.get("hover_chain", [item["dom_path"]]),
            ):
                return ""

            element = page.locator(item["dom_path"]).first

            if (await element.get_attribute("id") or "") != element_id:
                return ""

            try:
                visible = await element.is_visible()
            except Exception:
                visible = False

            if not visible:
                return ""

        box = await element.bounding_box()

        if not box:
            try:
                await element.scroll_into_view_if_needed(timeout=1800)
            except Exception:
                pass
            box = await element.bounding_box()

        if not box:
            return ""

        # Hover exact target.
        await page.mouse.move(
            box["x"] + box["width"] / 2,
            box["y"] + box["height"] / 2,
        )

        await page.wait_for_timeout(100)

        if (await element.get_attribute("id") or "") != element_id:
            return ""

        await element.screenshot(
            path=str(raw_path),
            animations="disabled",
            timeout=8000,
        )

        if not raw_path.exists():
            return ""

        add_text_panel(
            raw_path,
            final_path,
            item.get("element_text", ""),
        )

        return str(final_path) if final_path.exists() else ""

    except Exception:
        return ""


def write_csv(rows, csv_path):
    fields = ["id", "href", "element_text", "tag"]

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()

        for row in rows:
            writer.writerow({
                "id": row.get("id", ""),
                "href": row.get("href", ""),
                "element_text": row.get("element_text", ""),
                "tag": row.get("tag", ""),
            })


async def process_url(browser, url, url_index, regex_pattern, screenshot_callback):
    parsed = urlparse(url)

    folder = OUTPUT_DIR / safe_name(
        f"{url_index}_{parsed.netloc}_{parsed.path.strip('/') or 'homepage'}"
    )
    folder.mkdir(parents=True, exist_ok=True)

    csv_path = folder / "id_matches.csv"

    page = await browser.new_page(
        viewport={"width": 1440, "height": 1000},
        device_scale_factor=1,
    )

    try:
        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        # Small settle time only.
        await page.wait_for_timeout(350)

        consent_status = await handle_onetrust_consent(page)

        collection = await collect_matching_ids(page, regex_pattern)

        if collection.get("regex_error"):
            return {
                "url": url,
                "folder": folder,
                "csv": csv_path,
                "rows": [],
                "consent_status": consent_status,
                "error": f"Invalid regex: {collection['regex_error']}",
            }

        items = collection["results"]

        # CSV metadata is available immediately, before screenshots.
        write_csv(items, csv_path)

        rows = []
        total = len(items)

        for index, item in enumerate(items, start=1):
            if screenshot_callback:
                screenshot_callback(index, total, item["id"])

            screenshot = await capture_screenshot(
                page,
                item,
                folder,
                index,
            )

            rows.append({
                "id": item["id"],
                "href": item["href"],
                "element_text": item["element_text"],
                "tag": item["tag"],
                "screenshot": screenshot,
            })

        return {
            "url": url,
            "folder": folder,
            "csv": csv_path,
            "rows": rows,
            "consent_status": consent_status,
            "error": "",
        }

    except Exception as e:
        return {
            "url": url,
            "folder": folder,
            "csv": csv_path,
            "rows": [],
            "consent_status": "",
            "error": f"{type(e).__name__}: {e}",
        }

    finally:
        await page.close()


async def run_extraction(urls, regex_pattern, url_callback, screenshot_callback):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

        results = []

        try:
            for index, url in enumerate(urls, start=1):
                if url_callback:
                    url_callback(index, len(urls), url)

                result = await process_url(
                    browser,
                    url,
                    index,
                    regex_pattern,
                    screenshot_callback,
                )
                results.append(result)
        finally:
            await browser.close()

        return results


def make_zip(results):
    zip_path = OUTPUT_DIR / "HTML_ID_Regex_Extractor_Results.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for result in results:
            folder = Path(result["folder"])

            if folder.exists():
                for file in folder.rglob("*"):
                    if file.is_file():
                        z.write(
                            file,
                            arcname=file.relative_to(OUTPUT_DIR),
                        )

    return zip_path


ensure_playwright_browser()

st.set_page_config(
    page_title="HTML ID Regex Extractor",
    page_icon="🔎",
    layout="wide",
)

st.title("🔎 HTML ID Regex Extractor")

st.write(
    "Enter one or more URLs and an ID regex. "
    "The app extracts matching IDs, metadata, screenshots, and a CSV."
)

urls_text = st.text_area(
    "URLs — one per line",
    height=180,
    placeholder="https://example.com/page-1\nhttps://example.com/page-2",
)

regex_pattern = st.text_input(
    "ID regex",
    value=r"^link_",
    help="Examples: ^link_ | ^link_navdd | ^cta_ | .*button.*",
)

run = st.button(
    "🔎 Extract Matching IDs",
    type="primary",
    use_container_width=True,
)

if run:
    urls = list(dict.fromkeys(
        normalize_url(x)
        for x in urls_text.splitlines()
        if x.strip()
    ))

    if not urls:
        st.warning("Please enter at least one URL.")
        st.stop()

    if not regex_pattern.strip():
        st.warning("Please enter an ID regex.")
        st.stop()

    try:
        re.compile(regex_pattern)
    except re.error as e:
        st.error(f"Invalid regex: {e}")
        st.stop()

    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    url_bar = st.progress(0)
    screenshot_bar = st.progress(0)

    url_status = st.empty()
    screenshot_status = st.empty()

    def update_url(index, total, url):
        url_bar.progress(index / total)
        url_status.write(f"URL {index}/{total}: {url}")

    def update_screenshot(index, total, element_id):
        screenshot_bar.progress(index / total if total else 0)
        screenshot_status.write(
            f"Screenshot {index}/{total}: {element_id}"
        )

    try:
        results = asyncio.run(
            run_extraction(
                urls,
                regex_pattern,
                update_url,
                update_screenshot,
            )
        )
    except Exception as e:
        st.error(f"Extraction failed: {type(e).__name__}: {e}")
        st.stop()

    url_bar.empty()
    screenshot_bar.empty()
    url_status.empty()
    screenshot_status.empty()

    total_matches = sum(len(r["rows"]) for r in results)

    st.success(
        f"Completed {len(results)} URL(s). "
        f"Found {total_matches} matching ID(s)."
    )

    zip_path = make_zip(results)

    with open(zip_path, "rb") as f:
        st.download_button(
            "⬇️ Download All Results",
            f.read(),
            file_name=zip_path.name,
            mime="application/zip",
            use_container_width=True,
        )

    for url_index, result in enumerate(results, start=1):
        st.divider()
        st.header(f"URL {url_index}")
        st.code(result["url"])

        if result.get("consent_status"):
            st.caption(f"🍪 Consent: {result['consent_status']}")

        if result["error"]:
            st.error(result["error"])
            continue

        with open(result["csv"], "rb") as f:
            st.download_button(
                f"⬇️ Download CSV — URL {url_index}",
                f.read(),
                file_name=f"url_{url_index}_id_matches.csv",
                mime="text/csv",
                key=f"csv_{url_index}",
            )

        for n, item in enumerate(result["rows"], start=1):
            st.subheader(f"{n}. `{item['id']}`")
            st.write(f"**Tag:** `{item['tag']}`")

            if item["href"]:
                st.write(f"**Href:** `{item['href']}`")

            if item["element_text"]:
                st.write(f"**Element text:** {item['element_text']}")
            else:
                st.write("**Element text:** *(none)*")

            p = item["screenshot"]

            if p and Path(p).exists():
                st.image(p, use_container_width=False)
            else:
                st.info("Screenshot unavailable")
