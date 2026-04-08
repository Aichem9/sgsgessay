import re
import time
import zipfile
from io import BytesIO
from urllib.parse import urljoin, quote

import pandas as pd
import streamlit as st
from playwright.sync_api import sync_playwright

BASE_URL = "https://sgsg.hankyung.com"
SEARCH_URL = BASE_URL + "/search?query={query}"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)


def sanitize_filename(name: str, max_len: int = 120) -> str:
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name or "untitled"


def extract_article_title(page) -> str:
    try:
        h1 = page.locator("h1")
        if h1.count() > 0:
            text = h1.first.inner_text(timeout=2000).strip()
            if text:
                return text
    except Exception:
        pass

    try:
        meta = page.locator("meta[property='og:title']")
        if meta.count() > 0:
            content = meta.first.get_attribute("content")
            if content and content.strip():
                return content.strip()
    except Exception:
        pass

    try:
        title = page.title().strip()
        if title:
            return title
    except Exception:
        pass

    return "untitled"


def clean_page_for_pdf(page):
    css = """
    header, footer, nav, aside,
    .ad, .ads, .advertisement, .banner, .popup, .layer_popup,
    [class*="ad-"], [id*="ad-"], iframe {
        display: none !important;
        visibility: hidden !important;
    }
    body {
        background: white !important;
    }
    """
    page.add_style_tag(content=css)


def collect_article_links(page, keyword: str, delay: float = 1.0, max_pages: int = 30):
    encoded = quote(keyword)
    page.goto(SEARCH_URL.format(query=encoded), wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(int(delay * 1000))

    collected = {}
    visited = set()

    for page_num in range(1, max_pages + 1):
        current_url = page.url
        if current_url in visited:
            break
        visited.add(current_url)

        anchors = page.locator("a[href*='/article/']")
        count = anchors.count()

        for i in range(count):
            try:
                a = anchors.nth(i)
                href = a.get_attribute("href")
                txt = a.inner_text(timeout=1000).strip()
                if not href:
                    continue
                full_url = urljoin(BASE_URL, href)
                if "/article/" in full_url and full_url not in collected:
                    collected[full_url] = {
                        "title_hint": txt,
                        "search_page": page_num,
                    }
            except Exception:
                continue

        next_url = f"{BASE_URL}/search?page={page_num + 1}&query={encoded}"
        try:
            page.goto(next_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(int(delay * 1000))
            if page.locator("a[href*='/article/']").count() == 0:
                break
        except Exception:
            break

    return list(collected.keys())


def save_pdf_bytes(context, url: str, delay: float = 1.0):
    page = context.new_page()
    try:
        page.goto(url, wait_until="networkidle", timeout=90000)
        page.wait_for_timeout(int(delay * 1000))
        clean_page_for_pdf(page)
        title = sanitize_filename(extract_article_title(page))
        pdf_bytes = page.pdf(
            format="A4",
            print_background=True,
            margin={
                "top": "12mm",
                "bottom": "12mm",
                "left": "10mm",
                "right": "10mm",
            },
        )
        return {
            "title": title,
            "url": url,
            "pdf_bytes": pdf_bytes,
            "status": "saved",
        }
    except Exception as e:
        return {
            "title": "",
            "url": url,
            "pdf_bytes": None,
            "status": f"error: {e}",
        }
    finally:
        page.close()


def build_zip(results):
    memory_file = BytesIO()
    with zipfile.ZipFile(memory_file, "w", zipfile.ZIP_DEFLATED) as zf:
        rows = []
        for idx, item in enumerate(results, start=1):
            if item["status"] == "saved" and item["pdf_bytes"]:
                filename = f"{idx:03d}_{sanitize_filename(item['title'])}.pdf"
                zf.writestr(filename, item["pdf_bytes"])
                pdf_name = filename
            else:
                pdf_name = ""
            rows.append({
                "url": item["url"],
                "title": item["title"],
                "pdf_file": pdf_name,
                "status": item["status"],
            })

        df = pd.DataFrame(rows)
        csv_text = df.to_csv(index=False, encoding="utf-8-sig")
        zf.writestr("index.csv", csv_text.encode("utf-8-sig"))

    memory_file.seek(0)
    return memory_file


st.set_page_config(page_title="생글생글 검색 PDF 저장기", layout="wide")
st.title("생글생글 검색 결과 PDF 저장기")

query = st.text_input("검색어", value="논술")
max_pages = st.number_input("최대 검색 페이지 수", min_value=1, max_value=200, value=20)
delay = st.slider("페이지 대기 시간(초)", min_value=0.5, max_value=5.0, value=1.2, step=0.1)

run_btn = st.button("검색 후 PDF 생성")

if run_btn:
    if not query.strip():
        st.warning("검색어를 입력하세요.")
        st.stop()

    progress = st.progress(0)
    status_box = st.empty()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="ko-KR",
            viewport={"width": 1440, "height": 2200},
        )
        page = context.new_page()

        status_box.info("검색 결과에서 기사 링크를 수집하는 중입니다.")
        urls = collect_article_links(page, query.strip(), delay=delay, max_pages=int(max_pages))

        st.write(f"수집된 기사 수: **{len(urls)}개**")

        results = []
        total = max(len(urls), 1)

        for idx, url in enumerate(urls, start=1):
            status_box.info(f"[{idx}/{len(urls)}] PDF 생성 중: {url}")
            result = save_pdf_bytes(context, url, delay=delay)
            results.append(result)
            progress.progress(idx / total)
            time.sleep(delay)

        browser.close()

    df = pd.DataFrame([
        {
            "title": r["title"],
            "url": r["url"],
            "status": r["status"],
        }
        for r in results
    ])

    st.subheader("결과")
    st.dataframe(df, use_container_width=True)

    zip_buffer = build_zip(results)

    st.download_button(
        label="PDF ZIP 다운로드",
        data=zip_buffer,
        file_name=f"{sanitize_filename(query)}_pdf_bundle.zip",
        mime="application/zip",
    )

    status_box.success("완료되었습니다.")
