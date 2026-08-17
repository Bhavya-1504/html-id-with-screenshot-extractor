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

async def capture_hover_screenshot(page, element, element_id, folder, index):
    """
    Capture ONE screenshot of the exact ID-bearing element.

    Fix for layered/overlapping mega-menu elements:
    Playwright screenshots pixels from the page. If another menu item/panel is
    physically painted above the requested element, the screenshot can show
    the overlapping element even though the locator/ID is correct.

    This function:
      - verifies the exact ID
      - hovers the exact element
      - temporarily hides only foreign elements painted over its center
      - highlights the requested element
      - screenshots only the requested element
      - restores everything immediately afterward
    """
    sid = safe_name(element_id)
    screenshot_path = folder / f"{index:03d}_{sid}.png"

    # Strict ID verification before interaction.
    try:
        if await element.get_attribute("id") != element_id:
            return ""
    except Exception:
        return ""

    try:
        await element.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass

    await page.wait_for_timeout(350)

    try:
        if not await element.is_visible():
            return ""
    except Exception:
        return ""

    # Hover the exact element.
    try:
        box = await element.bounding_box()
        if not box:
            return ""

        await page.mouse.move(
            box["x"] + box["width"] / 2,
            box["y"] + box["height"] / 2
        )
    except Exception:
        try:
            await element.hover(force=True, timeout=10000)
        except Exception:
            return ""

    await page.wait_for_timeout(650)

    # Verify again after hover.
    try:
        if await element.get_attribute("id") != element_id:
            return ""
    except Exception:
        return ""

    # Save target styling, highlight it, and hide foreign elements that are
    # physically covering the target's center point.
    try:
        await element.evaluate("""
            (el) => {
                const key = "idExtractorOriginalStyles";
                el.dataset[key] = JSON.stringify({
                    outline: el.style.outline || "",
                    outlineOffset: el.style.outlineOffset || "",
                    position: el.style.position || "",
                    zIndex: el.style.zIndex || "",
                    isolation: el.style.isolation || ""
                });

                // Put the requested element above normal siblings where possible.
                const computed = getComputedStyle(el);
                if (computed.position === "static") {
                    el.style.position = "relative";
                }
                el.style.zIndex = "2147483647";
                el.style.isolation = "isolate";
                el.style.outline = "2px solid red";
                el.style.outlineOffset = "2px";

                // Track temporarily hidden overlays globally.
                window.__idExtractorHidden = [];

                const r = el.getBoundingClientRect();
                const x = Math.max(0, Math.min(window.innerWidth - 1, r.left + r.width / 2));
                const y = Math.max(0, Math.min(window.innerHeight - 1, r.top + r.height / 2));

                // Hide only foreign elements that are painted above the target.
                // Children of the target are allowed because they are part of it.
                for (let i = 0; i < 20; i++) {
                    const top = document.elementFromPoint(x, y);
                    if (!top) break;

                    if (top === el || el.contains(top)) {
                        break;
                    }

                    // Never hide ancestors of the target.
                    if (top.contains(el)) {
                        break;
                    }

                    window.__idExtractorHidden.push({
                        el: top,
                        visibility: top.style.visibility
                    });
                    top.style.visibility = "hidden";
                }
            }
        """)
    except Exception:
        pass

    await page.wait_for_timeout(120)

    # Final identity check.
    try:
        if await element.get_attribute("id") != element_id:
            return ""
    except Exception:
        return ""

    try:
        # Screenshot dimensions automatically follow THIS element's rendered size.
        await element.screenshot(
            path=str(screenshot_path),
            animations="disabled",
            timeout=15000
        )
    except Exception:
        return ""
    finally:
        # Restore target styles and any elements hidden for overlap removal.
        try:
            await page.evaluate("""
                () => {
                    if (window.__idExtractorHidden) {
                        for (const item of window.__idExtractorHidden) {
                            try {
                                item.el.style.visibility = item.visibility || "";
                            } catch (e) {}
                        }
                        delete window.__idExtractorHidden;
                    }
                }
            """)
        except Exception:
            pass

        try:
            await element.evaluate("""
                (el) => {
                    try {
                        const key = "idExtractorOriginalStyles";
                        const old = JSON.parse(el.dataset[key] || "{}");

                        el.style.outline = old.outline || "";
                        el.style.outlineOffset = old.outlineOffset || "";
                        el.style.position = old.position || "";
                        el.style.zIndex = old.zIndex || "";
                        el.style.isolation = old.isolation || "";

                        delete el.dataset[key];
                    } catch (e) {}
                }
            """)
        except Exception:
            pass

    return str(screenshot_path) if screenshot_path.exists() else ""

async def handle_onetrust_consent(page):
    """
    Try to accept all OneTrust cookies before extracting IDs.

    If no banner is present, extraction continues normally.
    If a banner is detected but Accept All cannot be found, return the
    requested status and continue extraction.
    """
    # Common OneTrust accept-all selectors across OneTrust implementations.
    selectors = [
        "#onetrust-accept-btn-handler",
        "#onetrust-banner-sdk button[id*='accept']",
        "#onetrust-banner-sdk button",
        "button#onetrust-accept-btn-handler",
        "[data-testid='uc-accept-all-button']"
    ]

    # Give OneTrust time to initialize.
    await page.wait_for_timeout(1200)

    # First check whether the OneTrust banner exists.
    banner_selectors = [
        "#onetrust-banner-sdk",
        "#onetrust-consent-sdk",
        "[id*='onetrust-banner']",
        "[class*='onetrust']"
    ]

    banner = None
    for selector in banner_selectors:
        try:
            loc = page.locator(selector).first
            if await loc.is_visible(timeout=1500):
                banner = loc
                break
        except Exception:
            continue

    if banner is None:
        return "No OneTrust consent banner detected"

    # Try the official OneTrust Accept All button first.
    accept_selectors = [
        "#onetrust-accept-btn-handler",
        "#onetrust-banner-sdk button[id*='accept']",
        "button:has-text('Accept All')",
        "button:has-text('Accept all')",
        "button:has-text('Accept Cookies')",
        "button:has-text('Allow All')",
        "button:has-text('Allow all')"
    ]

    for selector in accept_selectors:
        try:
            button = page.locator(selector).first
            if await button.is_visible(timeout=1500):
                await button.click(timeout=5000)
                await page.wait_for_timeout(1000)
                return "OneTrust Accept All clicked"
        except Exception:
            continue

    # If the banner is visible but no Accept All control was found,
    # continue extraction as requested.
    return "Consent banner detected – Accept All not found"


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

        consent_status = await handle_onetrust_consent(page)

        # Freeze the ID list BEFORE any hover interaction.
        # Then create a stable ElementHandle for each exact ID. This prevents
        # menu/hover DOM changes from shifting nth() indexes and mismatching IDs.
        ids = await page.locator('[id^="link_"]').evaluate_all("""
            elements => elements
                .map(el => el.id)
                .filter(id => id && id.startsWith("link_"))
        """)
        ids = list(dict.fromkeys(ids))

        for i, eid in enumerate(ids):
            try:
                handle = await page.evaluate_handle(
                    "id => document.getElementById(id)",
                    eid
                )
                el = handle.as_element()
                if el is None:
                    continue

                actual_id = await el.get_attribute("id")
                if actual_id != eid:
                    continue

                tag = await el.evaluate("(e) => e.tagName.toLowerCase()")
                try:
                    text = re.sub(r"\s+", " ", await el.inner_text()).strip()
                except Exception:
                    text = ""
                href = await el.get_attribute("href")
                visible = await el.is_visible()

                screenshot = await capture_hover_screenshot(
                    page, el, eid, folder, i + 1
                ) if visible else ""

                results.append({
                    "id": eid,
                    "tag": tag,
                    "text": text,
                    "href": href or "",
                    "visible": visible,
                    "screenshot": screenshot
                })
            except Exception as e:
                results.append({
                    "id": "",
                    "tag": "",
                    "text": "",
                    "href": "",
                    "visible": False,
                    "screenshot": "",
                    "error": type(e).__name__
                })

        with open(txt, "w", encoding="utf-8") as f:
            for r in results:
                if r["id"]:
                    f.write(r["id"] + "\n")

        return {
            "url": url,
            "folder": folder,
            "txt": txt,
            "results": results,
            "consent_status": consent_status,
            "error": ""
        }
    except Exception as e:
        return {
            "url": url,
            "folder": folder,
            "txt": txt,
            "results": [],
            "consent_status": "",
            "error": f"{type(e).__name__}: {e}"
        }
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
        if result.get("consent_status"):
            st.caption(f"🍪 Consent: {result['consent_status']}")

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

            st.markdown("**Hover-state screenshot**")
            p = item["screenshot"]
            if p and Path(p).exists():
                st.image(p, use_container_width=True)
                with open(p, "rb") as f:
                    st.download_button(
                        "⬇️ Download Screenshot",
                        f.read(),
                        file_name=Path(p).name,
                        mime="image/png",
                        key=f"screenshot_{ui}_{n}"
                    )
            else:
                st.info("Screenshot unavailable")
