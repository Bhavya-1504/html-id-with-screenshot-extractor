
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

OUTPUT_DIR = Path("/tmp/html_id_extractor")


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


async def wait_for_page(page):
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    await page.wait_for_timeout(900)


async def handle_onetrust_consent(page):
    await page.wait_for_timeout(1200)

    banner_selectors = [
        "#onetrust-banner-sdk",
        "#onetrust-consent-sdk",
        "[id*='onetrust-banner']",
    ]

    banner_found = False
    for selector in banner_selectors:
        try:
            loc = page.locator(selector).first
            if await loc.is_visible(timeout=1200):
                banner_found = True
                break
        except Exception:
            continue

    if not banner_found:
        return "No OneTrust consent banner detected"

    accept_selectors = [
        "#onetrust-accept-btn-handler",
        "button#onetrust-accept-btn-handler",
        "#onetrust-banner-sdk button[id*='accept']",
        "button:has-text('Accept All')",
        "button:has-text('Accept all')",
        "button:has-text('Accept Cookies')",
        "button:has-text('Allow All')",
        "button:has-text('Allow all')",
    ]

    for selector in accept_selectors:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=1200):
                await btn.click(timeout=5000)
                await page.wait_for_timeout(900)
                return "OneTrust Accept All clicked"
        except Exception:
            continue

    return "Consent banner detected – Accept All not found"


async def collect_ids_depth_first(page):
    return await page.evaluate("""
        () => {
            const results = [];

            function cleanText(el) {
                return (el.innerText || el.textContent || "")
                    .replace(/\\s+/g, " ")
                    .trim();
            }

            function visit(node, linkAncestors) {
                if (!node || node.nodeType !== Node.ELEMENT_NODE) return;

                let nextAncestors = linkAncestors;

                if (node.id && node.id.startsWith("link_")) {
                    const chain = [...linkAncestors, node.id];

                    results.push({
                        id: node.id,
                        href: node.getAttribute("href") || "",
                        element_text: cleanText(node),
                        tag: node.tagName.toLowerCase(),
                        hover_chain: chain
                    });

                    nextAncestors = chain;
                }

                for (const child of node.children) {
                    visit(child, nextAncestors);
                }
            }

            visit(document.documentElement, []);
            return results;
        }
    """)


async def close_hover_state(page):
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass

    try:
        viewport = page.viewport_size or {"width": 1440, "height": 1000}
        await page.mouse.move(
            max(1, viewport["width"] - 5),
            max(1, viewport["height"] - 5),
        )
    except Exception:
        pass

    await page.wait_for_timeout(350)


async def exact_locator(page, element_id):
    escaped = element_id.replace("\\", "\\\\").replace('"', '\\"')
    return page.locator(f'[id="{escaped}"]').first


async def reveal_hover_chain(page, hover_chain):
    for eid in hover_chain:
        loc = await exact_locator(page, eid)

        try:
            await loc.scroll_into_view_if_needed(timeout=4000)
        except Exception:
            pass

        try:
            box = await loc.bounding_box()
            if box:
                await page.mouse.move(
                    box["x"] + box["width"] / 2,
                    box["y"] + box["height"] / 2,
                )
            else:
                await loc.hover(force=True, timeout=5000)
        except Exception:
            try:
                await loc.hover(force=True, timeout=5000)
            except Exception:
                return False

        await page.wait_for_timeout(300)

    return True


async def capture_exact_element(page, item, folder, index):
    """
    One ID = one image.

    The image contains:
      - the screenshot of the exact ID-bearing element
      - a clearly separated metadata footer containing ELEMENT TEXT

    The footer is visually distinct so it cannot be confused with text
    rendered by the website itself.
    """
    element_id = item["id"]
    raw_path = folder / f"{index:03d}_{safe_name(element_id)}__raw.png"
    screenshot_path = folder / f"{index:03d}_{safe_name(element_id)}.png"

    await close_hover_state(page)

    try:
        if not await reveal_hover_chain(page, item.get("hover_chain", [element_id])):
            return ""

        element = await exact_locator(page, element_id)

        if await element.get_attribute("id") != element_id:
            return ""

        if not await element.is_visible():
            return ""

        box = await element.bounding_box()
        if not box:
            return ""

        # Final hover on the exact target.
        await page.mouse.move(
            box["x"] + box["width"] / 2,
            box["y"] + box["height"] / 2,
        )
        await page.wait_for_timeout(450)

        if await element.get_attribute("id") != element_id:
            return ""

        # Capture only the exact element.
        await element.screenshot(
            path=str(raw_path),
            animations="disabled",
            timeout=15000,
        )

        if not raw_path.exists():
            return ""

        # Append a distinct text panel BELOW the screenshot.
        from PIL import Image, ImageDraw, ImageFont
        import textwrap

        img = Image.open(raw_path).convert("RGB")
        img_w, img_h = img.size

        element_text = (item.get("element_text") or "").strip()
        if not element_text:
            element_text = "(no visible text)"

        label = "ELEMENT TEXT"
        font = ImageFont.load_default()

        # Keep the metadata panel readable but compact.
        panel_min_width = 260
        final_w = max(img_w, panel_min_width)

        # Wrap text based on available width.
        approx_chars = max(24, min(90, int((final_w - 24) / 7)))
        wrapped = textwrap.wrap(element_text, width=approx_chars) or [element_text]

        label_h = 18
        line_h = 16
        pad = 10
        divider_h = 2
        panel_h = pad + label_h + 6 + (line_h * len(wrapped)) + pad

        final = Image.new("RGB", (final_w, img_h + divider_h + panel_h), "white")

        # Center screenshot if its width is narrower than the metadata panel.
        x_offset = (final_w - img_w) // 2
        final.paste(img, (x_offset, 0))

        draw = ImageDraw.Draw(final)

        # Strong separator so metadata cannot be mistaken for website content.
        draw.rectangle(
            [(0, img_h), (final_w - 1, img_h + divider_h - 1)],
            fill=(0, 0, 0)
        )

        panel_top = img_h + divider_h

        # Metadata panel border.
        draw.rectangle(
            [(0, panel_top), (final_w - 1, final.height - 1)],
            outline=(90, 90, 90),
            width=1
        )

        y = panel_top + pad

        # Label in a dedicated boxed area.
        label_box_w = 95
        label_box_h = 18
        draw.rectangle(
            [(pad, y), (pad + label_box_w, y + label_box_h)],
            outline=(90, 90, 90),
            width=1
        )
        draw.text(
            (pad + 5, y + 3),
            label,
            fill=(0, 0, 0),
            font=font
        )

        y += label_h + 8

        for line in wrapped:
            draw.text(
                (pad, y),
                line,
                fill=(0, 0, 0),
                font=font
            )
            y += line_h

        final.save(screenshot_path, format="PNG")
        img.close()

        try:
            raw_path.unlink()
        except Exception:
            pass

        return str(screenshot_path) if screenshot_path.exists() else ""

    except Exception:
        return ""

    finally:
        await close_hover_state(page)

def write_csv(rows, csv_path):
    fields = ["id", "href", "element_text", "tag"]
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


async def process_url(browser, url, url_index):
    parsed = urlparse(url)
    folder = OUTPUT_DIR / safe_name(
        f"{url_index}_{parsed.netloc}_{parsed.path.strip('/') or 'homepage'}"
    )
    folder.mkdir(parents=True, exist_ok=True)
    csv_path = folder / "elements.csv"

    page = await browser.new_page(
        viewport={"width": 1440, "height": 1000},
        device_scale_factor=1,
    )

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await wait_for_page(page)

        consent_status = await handle_onetrust_consent(page)
        items = await collect_ids_depth_first(page)

        rows = []
        for index, item in enumerate(items, start=1):
            screenshot = await capture_exact_element(page, item, folder, index)
            rows.append({
                "id": item["id"],
                "href": item["href"],
                "element_text": item["element_text"],
                "tag": item["tag"],
                "screenshot": screenshot,
            })

        write_csv(rows, csv_path)

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


async def run_extraction(urls, progress_callback=None):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )

        results = []
        try:
            for i, url in enumerate(urls, start=1):
                result = await process_url(browser, url, i)
                results.append(result)
                if progress_callback:
                    progress_callback(i / len(urls), url)
        finally:
            await browser.close()

        return results


def make_zip(results):
    zip_path = OUTPUT_DIR / "HTML_ID_Extractor_Results.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for result in results:
            folder = Path(result["folder"])
            if folder.exists():
                for file in folder.rglob("*"):
                    if file.is_file():
                        z.write(file, arcname=file.relative_to(OUTPUT_DIR))
    return zip_path


ensure_playwright_browser()

st.set_page_config(page_title="HTML ID Extractor", page_icon="🔎", layout="wide")
st.title("🔎 HTML ID Extractor")
st.write(
    "Extracts `link_` IDs in parent → child → grandchild order, "
    "resets hover between elements, captures one image per ID, "
    "and generates a CSV with ID, href, element text, and tag."
)

urls_text = st.text_area(
    "URLs — one per line",
    height=180,
    placeholder="https://example.com/page-1\\nhttps://example.com/page-2",
)

run = st.button(
    "🔎 Extract IDs & Capture Screenshots",
    type="primary",
    use_container_width=True,
)

if run:
    urls = list(dict.fromkeys(
        normalize_url(x) for x in urls_text.splitlines() if x.strip()
    ))

    if not urls:
        st.warning("Please enter at least one URL.")
        st.stop()

    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    progress = st.progress(0)
    status = st.empty()

    def update_progress(value, url):
        progress.progress(value)
        status.write(f"Processing: {url}")

    try:
        results = asyncio.run(run_extraction(urls, update_progress))
    except Exception as e:
        st.error(f"Extraction failed: {type(e).__name__}: {e}")
        st.stop()

    progress.empty()
    status.empty()

    total = sum(len(r["rows"]) for r in results)
    st.success(f"Completed {len(results)} URL(s). Found {total} matching element(s).")

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
                file_name=f"url_{url_index}_elements.csv",
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
