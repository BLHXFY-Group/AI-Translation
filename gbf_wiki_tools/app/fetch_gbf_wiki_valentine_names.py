from __future__ import annotations

import csv
import re
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from urllib.parse import quote, unquote, urlparse

from fetch_gbf_wiki_jp_names import (
    SITE_ORIGIN,
    launch_browser,
    normalize_text,
    open_api_session,
)

if TYPE_CHECKING:
    from playwright.sync_api import Page


APP_DIR = Path(__file__).resolve().parent
TOOL_DIR = APP_DIR.parent
OUTPUT_PATH = TOOL_DIR / "gbf_wiki_jp_names_valentine.csv"
CSV_FIELDS = ("japanese_name", "chinese_name")
START_YEAR = 2018
MAX_RETRIES = 3
NAME_SEPARATOR = " / "
PAGE_LABEL_PATTERN = re.compile(r"^第(\d+)页$")
ANCHOR_PATTERN = re.compile(r"^a(\d+)a$")


class MissingWikiPageError(RuntimeError):
    pass


def year_title(year: int) -> str:
    return f"{year}年情人节返礼汇总"


def year_url(year: int) -> str:
    return f"{SITE_ORIGIN}/wiki/{quote(year_title(year), safe='')}"


def parse_display_name(value: object) -> tuple[str, str]:
    display_name = normalize_text(value)
    if display_name.count(NAME_SEPARATOR) != 1:
        raise ValueError(f"姓名不符合‘中文名 / 日文名’格式：{display_name or '空值'}")
    chinese_name, japanese_name = display_name.split(NAME_SEPARATOR)
    chinese_name = normalize_text(chinese_name)
    japanese_name = normalize_text(japanese_name)
    if not chinese_name or not japanese_name:
        raise ValueError(f"中文名或日文名为空：{display_name}")
    return japanese_name, chinese_name


def read_existing_rows(output_path: Path = OUTPUT_PATH) -> dict[str, str]:
    if not output_path.exists():
        return {}

    with output_path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        fields = reader.fieldnames or []
        missing_fields = [field for field in CSV_FIELDS if field not in fields]
        if missing_fields:
            raise RuntimeError(f"现有 CSV 缺少字段：{', '.join(missing_fields)}")

        rows = {}
        for line_number, item in enumerate(reader, start=2):
            japanese_name = normalize_text(item.get("japanese_name"))
            chinese_name = normalize_text(item.get("chinese_name"))
            if not japanese_name or not chinese_name:
                raise RuntimeError(f"现有 CSV 第 {line_number} 行存在空姓名")
            if japanese_name in rows:
                raise RuntimeError(
                    f"现有 CSV 第 {line_number} 行日文名重复：{japanese_name}"
                )
            rows[japanese_name] = chinese_name
        return rows


def consolidate_fetched_rows(rows: list[dict]) -> dict[str, str]:
    latest_rows = {}
    for item in sorted(
        rows,
        key=lambda row: (
            int(row["source_year"]),
            int(row["source_page"]),
            int(row["source_anchor"]),
        ),
    ):
        latest_rows[item["japanese_name"]] = item["chinese_name"]
    return latest_rows


def merge_rows(existing_rows: dict[str, str], fetched_rows: dict[str, str]) -> dict[str, str]:
    merged_rows = dict(existing_rows)
    merged_rows.update(fetched_rows)
    return merged_rows


def write_rows_if_changed(
    existing_rows: dict[str, str],
    merged_rows: dict[str, str],
    output_path: Path = OUTPUT_PATH,
) -> bool:
    if existing_rows == merged_rows and output_path.exists():
        return False

    temporary_path = output_path.with_suffix(".csv.tmp")
    with temporary_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS, lineterminator="\r\n")
        writer.writeheader()
        for japanese_name in sorted(merged_rows):
            writer.writerow(
                {
                    "japanese_name": japanese_name,
                    "chinese_name": merged_rows[japanese_name],
                }
            )
    temporary_path.replace(output_path)
    return True


def parse_page_links(items: list[dict[str, str]], year: int) -> list[tuple[int, str]]:
    expected_title = year_title(year)
    pages = {}
    for item in items:
        label = normalize_text(item.get("text"))
        match = PAGE_LABEL_PATTERN.fullmatch(label)
        if not match:
            continue

        href = normalize_text(item.get("href"))
        parsed = urlparse(href)
        if parsed.scheme not in ("http", "https") or parsed.netloc != urlparse(SITE_ORIGIN).netloc:
            continue
        if parsed.query or parsed.fragment:
            continue

        page_number = int(match.group(1))
        if unquote(parsed.path) != f"/wiki/{expected_title}/{page_number}":
            continue
        pages[page_number] = href
    return sorted(pages.items())


def fetch_wiki_items(
    page: Page, url: str, expected_heading: str, content_type: str
) -> list[dict[str, str]]:
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = page.evaluate(
                """
                async ({ url, expectedHeading, contentType }) => {
                  const normalize = value => String(value || '').replace(/\s+/g, ' ').trim()
                  const controller = new AbortController()
                  const timeout = setTimeout(() => controller.abort(), 30_000)
                  let response
                  try {
                    response = await fetch(url, {
                      credentials: 'same-origin',
                      signal: controller.signal
                    })
                  } finally {
                    clearTimeout(timeout)
                  }

                  if (response.status === 404) {
                    return { ok: false, missing: true, error: `HTTP 404: ${url}` }
                  }
                  if (!response.ok) {
                    return { ok: false, error: `HTTP ${response.status}: ${url}` }
                  }

                  const requestedUrl = new URL(url)
                  const finalUrl = new URL(response.url)
                  if (finalUrl.origin !== requestedUrl.origin || finalUrl.pathname !== requestedUrl.pathname) {
                    return { ok: false, error: `页面被重定向到 ${response.url}` }
                  }
                  const responseType = response.headers.get('content-type') || ''
                  if (!responseType.includes('text/html')) {
                    return { ok: false, error: `返回内容不是 HTML：${responseType || '未知类型'}` }
                  }

                  const documentText = await response.text()
                  const pageDocument = new DOMParser().parseFromString(documentText, 'text/html')
                  if (pageDocument.querySelector('.noarticletext, #noarticletext')) {
                    return { ok: false, missing: true, error: `页面不存在：${url}` }
                  }
                  const heading = normalize(pageDocument.querySelector('#firstHeading h1')?.textContent)
                  if (heading !== expectedHeading) {
                    return {
                      ok: false,
                      error: `页面标题异常：预期‘${expectedHeading}’，实际‘${heading || '空值'}’`
                    }
                  }

                  let items
                  if (contentType === 'links') {
                    items = [...pageDocument.querySelectorAll('a')].map(anchor => ({
                      text: anchor.textContent || '',
                      href: new URL(anchor.getAttribute('href') || '', response.url).toString()
                    }))
                  } else if (contentType === 'names') {
                    items = [...pageDocument.querySelectorAll('tr[id]')]
                      .filter(row => /^a\d+a$/.test(row.id))
                      .map(row => ({
                        anchor: row.id,
                        display_name: row.querySelector('td:first-child b')?.textContent || ''
                      }))
                  } else {
                    return { ok: false, error: `未知内容类型：${contentType}` }
                  }
                  return { ok: true, items }
                }
                """,
                {
                    "url": url,
                    "expectedHeading": expected_heading,
                    "contentType": content_type,
                },
            )
            if result.get("missing"):
                raise MissingWikiPageError(result.get("error") or f"页面不存在：{url}")
            if not result.get("ok"):
                raise RuntimeError(result.get("error") or "页面抓取失败")
            return result.get("items") or []
        except MissingWikiPageError:
            raise
        except Exception as error:
            last_error = error
            if attempt < MAX_RETRIES:
                time.sleep(attempt)
    raise RuntimeError(f"页面连续 {MAX_RETRIES} 次抓取失败：{last_error}")


def discover_year_pages(items: list[dict[str, str]], year: int) -> list[tuple[int, str]]:
    pages = parse_page_links(items, year)
    if not pages:
        raise RuntimeError("汇总页没有发现有效分页")
    return pages


def extract_page_rows(
    items: list[dict[str, str]], year: int, page_number: int
) -> tuple[list[dict], int]:
    if not items:
        raise RuntimeError("分页中没有找到人名记录")

    rows = []
    invalid_count = 0
    for item in items:
        anchor = normalize_text(item.get("anchor"))
        anchor_match = ANCHOR_PATTERN.fullmatch(anchor)
        if not anchor_match:
            print(f"[错误] {year} 年第 {page_number} 页存在无效锚点：{anchor}")
            invalid_count += 1
            continue
        try:
            japanese_name, chinese_name = parse_display_name(item.get("display_name"))
        except ValueError as error:
            print(f"[错误] {year} 年第 {page_number} 页 {anchor}：{error}")
            invalid_count += 1
            continue
        rows.append(
            {
                "japanese_name": japanese_name,
                "chinese_name": chinese_name,
                "source_year": year,
                "source_page": page_number,
                "source_anchor": int(anchor_match.group(1)),
            }
        )
    return rows, invalid_count


def collect_year(page: Page, year: int) -> tuple[list[dict], int]:
    title = year_title(year)
    link_items = fetch_wiki_items(page, year_url(year), title, "links")
    pages = discover_year_pages(link_items, year)
    print(f"{year} 年：发现 {len(pages)} 个有效分页。")

    rows = []
    error_count = 0
    for page_number, page_url in pages:
        try:
            name_items = fetch_wiki_items(
                page, page_url, f"{title}/{page_number}", "names"
            )
            page_rows, invalid_count = extract_page_rows(
                name_items, year, page_number
            )
            rows.extend(page_rows)
            error_count += invalid_count
            print(f"  第 {page_number} 页：{len(page_rows)} 条。")
        except Exception as error:
            error_count += 1
            print(f"[错误] {year} 年第 {page_number} 页抓取失败：{error}")
    return rows, error_count


def collect_years(
    page: Page,
    current_year: int,
    collector: Callable[[Page, int], tuple[list[dict], int]] = collect_year,
) -> tuple[list[dict], int]:
    rows = []
    error_count = 0
    for year in range(START_YEAR, current_year + 1):
        try:
            year_rows, year_errors = collector(page, year)
            rows.extend(year_rows)
            error_count += year_errors
        except Exception as error:
            error_count += 1
            print(f"[错误] {year} 年汇总页抓取失败，已跳过：{error}")
    return rows, error_count


def main() -> None:
    from playwright.sync_api import sync_playwright

    existing_rows = read_existing_rows()
    current_year = datetime.now().year

    with sync_playwright() as playwright:
        context = None
        try:
            context, channel = launch_browser(playwright)
            print(f"已启动 {channel}。")
            page = open_api_session(context)
            fetched_rows, error_count = collect_years(page, current_year)
        finally:
            if context:
                context.close()

    if not fetched_rows:
        if existing_rows:
            print("本次没有抓取到有效人名，已保留现有 CSV。")
            return
        raise RuntimeError("本次没有抓取到有效人名，未生成 CSV。")

    latest_rows = consolidate_fetched_rows(fetched_rows)
    merged_rows = merge_rows(existing_rows, latest_rows)
    changed = write_rows_if_changed(existing_rows, merged_rows)
    if changed:
        print(f"已写入 {OUTPUT_PATH}")
    else:
        print("CSV 内容没有变化，已保持原文件。")
    print(
        f"年度记录：{len(fetched_rows)}；"
        f"本次唯一日文名：{len(latest_rows)}；"
        f"汇总后：{len(merged_rows)}；错误：{error_count}。"
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("已取消。")
    except Exception as error:
        raise SystemExit(str(error)) from error
