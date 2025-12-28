import asyncio
import logging
from typing import List, Tuple
from urllib.parse import parse_qs, urlparse

import requests
import streamlit as st
from playwright.async_api import async_playwright, Page

# Streamlit on Windows often forces SelectorEventLoop, which cannot spawn subprocesses
# needed by Playwright. Force Proactor policy so Chromium can launch.
if hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def _collect_news_links(query: str, limit: int, max_pages: int = 3) -> Tuple[List[str], List[str]]:
    """Use Playwright to search Google News and return raw result links plus debug logs."""
    debug: List[str] = []

    def log_debug(msg: str) -> None:
        logger.info(msg)
        debug.append(msg)
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ]
        )
        context = await browser.new_context(user_agent=USER_AGENT, viewport={"width": 1280, "height": 900})
        page: Page = await context.new_page()

        try:
            await page.goto("https://www.google.com/", wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
            log_debug("Opened google.com and waited 1.5s")

            # Search box can be input or textarea depending on layout.
            box = page.locator("input[name='q'], textarea[name='q']").first
            await box.wait_for(state="visible", timeout=10000)
            log_debug("Found search box; filling query")
            await box.fill(query)
            await page.keyboard.press("Enter")

            # Wait for navigation to complete after search
            await page.wait_for_load_state("networkidle", timeout=15000)
            await page.wait_for_timeout(1000)
            
            # Debug: capture what page we landed on
            screenshot_bytes = await page.screenshot(full_page=False)
            log_debug(f"Search submitted; current URL: {page.url}")
            log_debug(f"Page title: {await page.title()}")
            
            # Check for CAPTCHA
            captcha_present = await page.locator("iframe[src*='recaptcha'], div:has-text('unusual traffic')").count()
            if captcha_present > 0:
                log_debug("⚠️ CAPTCHA or bot detection page detected!")
            
            # Move to News tab if present.
            try:
                news_tab = page.locator("a:has-text('News')").filter(has=page.locator("[href*='tbm=nws']"))
                news_count = await news_tab.count()
                if news_count == 0:
                    news_tab = page.locator("a[href*='tbm=nws']")
                    news_count = await news_tab.count()
                log_debug(f"News tab candidates found: {news_count}")
            except Exception as e:
                log_debug(f"Error counting news tabs: {e}, using fallback URL")
                news_count = 0

            if news_count > 0:
                log_debug("Clicking News tab")
                try:
                    await news_tab.first.click(timeout=10000)
                    await page.wait_for_load_state("networkidle", timeout=15000)
                    await page.wait_for_timeout(500)
                    log_debug(f"Post-click URL: {page.url}")
                except Exception as e:
                    log_debug(f"Click failed: {e}, using direct URL instead")
                    search_url = f"https://www.google.com/search?q={query}&tbm=nws"
                    await page.goto(search_url, wait_until="networkidle", timeout=15000)
                    await page.wait_for_timeout(500)
                    log_debug(f"Loaded fallback News URL: {page.url}")
            else:
                log_debug("News tab not found; attempting direct tbm=nws URL")
                search_url = f"https://www.google.com/search?q={query}&tbm=nws"
                await page.goto(search_url, wait_until="networkidle", timeout=15000)
                await page.wait_for_timeout(500)
                log_debug(f"Loaded fallback News URL: {page.url}")

            urls: List[str] = []

            def unwrap_google_redirect(href: str) -> str:
                parsed = urlparse(href)
                if "google." not in parsed.netloc:
                    return href
                qs = parse_qs(parsed.query)
                for key in ("url", "q"):
                    if key in qs and qs[key]:
                        return qs[key][0]
                return href

            def is_googleish(href: str) -> bool:
                host = urlparse(href).netloc.lower()
                return (
                    host.endswith("google.com")
                    or host.endswith("google.co.in")
                    or host.startswith("news.google.")
                    or host.endswith("gstatic.com")
                    or host.endswith("about.google")
                    or "google." in host
                )

            for page_idx in range(max_pages):
                if page_idx > 0:
                    paged_url = f"https://www.google.com/search?q={query}&tbm=nws&start={page_idx*10}"
                    await page.goto(paged_url, wait_until="networkidle", timeout=15000)
                    await page.wait_for_timeout(500)
                    log_debug(f"Loaded page {page_idx+1}: {page.url}")

                # Ensure page is stable before querying
                await page.wait_for_load_state("domcontentloaded")
                
                links = page.locator("a[href]")
                try:
                    count = await links.count()
                except Exception as nav_exc:
                    log_debug(f"Retrying after navigation/context change: {nav_exc}")
                    await page.wait_for_load_state("networkidle", timeout=10000)
                    await page.wait_for_timeout(500)
                    links = page.locator("a[href]")
                    count = await links.count()
                log_debug(f"Page {page_idx+1}: total hrefs on page: {count}")

                for idx in range(count):
                    raw_href = await links.nth(idx).get_attribute("href")
                    if not raw_href or not raw_href.startswith("http"):
                        continue
                    href = unwrap_google_redirect(raw_href)
                    if not href.startswith("http"):
                        continue
                    if is_googleish(href):
                        continue
                    urls.append(href)
                    if len(urls) >= limit:
                        break

                if len(urls) >= limit:
                    break

            unique = list(dict.fromkeys(urls))
            log_debug(f"Collected {len(unique)} unique candidate URLs across {max_pages} page(s)")
            return unique, debug
        finally:
            await context.close()
            await browser.close()


def _resolve_redirects(urls: List[str], debug: List[str]) -> List[str]:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    resolved: List[str] = []
    for url in urls:
        try:
            resp = session.get(url, allow_redirects=True, timeout=15)
            resolved.append(resp.url)
            debug.append(f"Resolved redirect: {url} -> {resp.url}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redirect resolution failed for %s: %s", url, exc)
            debug.append(f"Failed to resolve: {url} ({exc})")
    return list(dict.fromkeys(resolved))


def run_search(query: str, limit: int) -> Tuple[List[str], List[str]]:
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        raw_urls, debug = loop.run_until_complete(_collect_news_links(query, limit))
    finally:
        loop.close()
        asyncio.set_event_loop(None)
    resolved = _resolve_redirects(raw_urls, debug)
    return resolved, debug


st.set_page_config(page_title="News Link Extractor", page_icon="📰", layout="centered")
st.title("Google News Link Extractor")
st.caption("Enter a keyword, fetch News results, and output real article URLs (redirects resolved).")

keyword = st.text_input("Keyword or phrase", placeholder="e.g. renewable energy", max_chars=100)
limit = st.slider("Maximum links", min_value=3, max_value=30, value=12, step=1)

if st.button("Search News", type="primary", use_container_width=True):
    if not keyword.strip():
        st.warning("Please enter a keyword first.")
    else:
        debug_logs: List[str] = []
        with st.status("Running Playwright search...", expanded=True) as status:
            try:
                urls, debug_logs = run_search(keyword.strip(), limit)
                status.update(label="Done", state="complete")
            except Exception as exc:  # noqa: BLE001
                status.update(label="Failed", state="error")
                st.error(f"Search failed: {exc}")
                urls = []

        if urls:
            st.success(f"Found {len(urls)} unique links")
            for i, url in enumerate(urls, start=1):
                st.write(f"{i:02d}. {url}")
        else:
            st.info("No links found.")

        if debug_logs:
            with st.expander("Debug log", expanded=False):
                for line in debug_logs:
                    st.write(line)

st.divider()
st.markdown(
    """
    **Setup hints**
    - Install deps: `pip install streamlit playwright requests`
    - Install browser once: `python -m playwright install chromium`
    - Run: `streamlit run streamlit_news.py`
    """
)
