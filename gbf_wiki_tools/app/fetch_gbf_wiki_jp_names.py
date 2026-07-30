import argparse
import csv
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, sync_playwright


API_URL = "https://gbf.huijiwiki.com/api.php"
SITE_ORIGIN = "https://gbf.huijiwiki.com"
APP_DIR = Path(__file__).resolve().parent
TOOL_DIR = APP_DIR.parent
RUNTIME_PLATFORM = "windows" if sys.platform == "win32" else "macos"
OUTPUT_PATH = TOOL_DIR / "gbf_wiki_jp_names.csv"
PROFILE_PATH = TOOL_DIR / ".runtime" / RUNTIME_PLATFORM / "browser-profile"
CSV_FIELDS = ("id", "japanese_name", "chinese_name")
BATCH_SIZE = 3
MAX_RETRIES = 3


def normalize_text(value: object) -> str:
    return " ".join(str(value or "").split())


def parse_mode() -> str:
    parser = argparse.ArgumentParser(description="抓取灰机 Wiki 人物中日文名")
    parser.add_argument("mode", choices=("full", "update"))
    return parser.parse_args().mode


def read_existing_rows() -> list[dict[str, str]]:
    if not OUTPUT_PATH.exists():
        return []

    with OUTPUT_PATH.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        fields = reader.fieldnames or []
        missing_fields = [field for field in CSV_FIELDS if field not in fields]
        if missing_fields:
            raise RuntimeError(f"现有 CSV 缺少字段：{', '.join(missing_fields)}")

        rows = []
        seen_ids = set()
        for item in reader:
            character_id = normalize_text(item.get("id"))
            if not character_id or character_id in seen_ids:
                continue
            seen_ids.add(character_id)
            rows.append(
                {
                    "id": character_id,
                    "chinese_name": normalize_text(item.get("chinese_name")),
                    "japanese_name": normalize_text(item.get("japanese_name")),
                }
            )
        return rows


def write_rows(rows: list[dict[str, str]]) -> None:
    sorted_rows = sorted(rows, key=lambda item: int(item["id"]))
    temporary_path = OUTPUT_PATH.with_suffix(".csv.tmp")
    with temporary_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS, lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(sorted_rows)
    temporary_path.replace(OUTPUT_PATH)


def launch_browser(playwright) -> tuple[BrowserContext, str]:
    PROFILE_PATH.mkdir(parents=True, exist_ok=True)
    errors = []
    for channel in ("msedge", "chrome"):
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_PATH),
                channel=channel,
                headless=False,
                no_viewport=True,
            )
            return context, channel
        except Exception as error:
            errors.append(f"{channel}: {error}")
    details = "\n".join(errors)
    raise RuntimeError(f"无法启动 Edge 或 Chrome：\n{details}")


def open_api_session(context: BrowserContext) -> Page:
    page = context.pages[0] if context.pages else context.new_page()
    bootstrap_url = (
        f"{API_URL}?action=query&list=allpages&apprefix=Char%2F"
        "&aplimit=max&format=json&formatversion=2"
    )
    print("正在打开灰机 Wiki。若浏览器显示安全验证，请在窗口中手动完成。")
    page.goto(bootstrap_url, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_function(
            "document.body && document.body.innerText.trim().startsWith('{')",
            timeout=120_000,
        )
    except Exception as error:
        raise RuntimeError(
            "等待灰机 Wiki 安全验证超时，请重新运行并在浏览器窗口中完成验证。"
        ) from error
    return page


def get_json_body(page: Page) -> dict:
    text = page.locator("body").inner_text()
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"接口没有返回 JSON，当前页面：{page.url}") from error


def api_fetch(page: Page, params: dict[str, str]) -> dict:
    result = page.evaluate(
        """
        async ({ apiUrl, query }) => {
          const url = new URL(apiUrl)
          url.search = new URLSearchParams(query).toString()
          const response = await fetch(url.toString(), { credentials: 'same-origin' })
          return {
            ok: response.ok,
            status: response.status,
            text: await response.text()
          }
        }
        """,
        {"apiUrl": API_URL, "query": params},
    )
    if not result["ok"]:
        raise RuntimeError(f"接口请求失败：HTTP {result['status']}")
    try:
        return json.loads(result["text"])
    except json.JSONDecodeError as error:
        raise RuntimeError("接口返回了无法解析的内容") from error


def collect_character_ids(page: Page) -> list[str]:
    ids = []
    seen_ids = set()
    continuation = None

    while True:
        params = {
            "action": "query",
            "list": "allpages",
            "apprefix": "Char/",
            "aplimit": "max",
            "format": "json",
            "formatversion": "2",
        }
        if continuation:
            params["apcontinue"] = continuation["apcontinue"]
            params["continue"] = continuation["continue"]

        data = api_fetch(page, params) if continuation else get_json_body(page)
        for item in data.get("query", {}).get("allpages", []):
            title = item.get("title", "")
            prefix = "Char/"
            character_id = title[len(prefix) :] if title.startswith(prefix) else ""
            if not character_id.isdigit() or character_id in seen_ids:
                continue
            seen_ids.add(character_id)
            ids.append(character_id)

        continuation = data.get("continue")
        if not continuation or not continuation.get("apcontinue"):
            break

    return sorted(ids, key=int)


def fetch_name_batch_once(page: Page, ids: list[str]) -> list[dict]:
    return page.evaluate(
        """
        async ({ siteOrigin, characterIds }) => {
          const normalize = value => String(value || '').replace(/\s+/g, ' ').trim()
          const parser = new DOMParser()

          return Promise.all(characterIds.map(async id => {
            try {
              const url = new URL(`/wiki/Char/${id}`, siteOrigin)
              const response = await fetch(url.toString(), { credentials: 'same-origin' })
              if (!response.ok) {
                throw new Error(`HTTP ${response.status}`)
              }

              const pageDocument = parser.parseFromString(await response.text(), 'text/html')
              const chineseName = normalize(pageDocument.querySelector('#firstHeading > h1')?.textContent)
              const configScript = [...pageDocument.scripts].find(script => {
                return script.textContent.includes('wgHuijiVars.Char')
              })
              let japaneseName = ''
              let characterData = null
              if (configScript) {
                const scriptText = configScript.textContent
                const configStart = scriptText.indexOf('RLCONF=') + 'RLCONF='.length
                const configEnd = scriptText.indexOf(';RLSTATE=', configStart)
                if (configStart >= 'RLCONF='.length && configEnd > configStart) {
                  const config = JSON.parse(scriptText.slice(configStart, configEnd))
                  characterData = config['wgHuijiVars.Char'] || null
                  japaneseName = normalize(characterData?.name_jp)
                }
              }

              if (!characterData) {
                return { ok: true, skip: true, id }
              }
              if (!chineseName) {
                throw new Error('未找到中文名')
              }
              return {
                ok: true,
                row: {
                  id,
                  chinese_name: chineseName,
                  japanese_name: japaneseName
                }
              }
            } catch (error) {
              return { ok: false, id, error: error.message }
            }
          }))
        }
        """,
        {"siteOrigin": SITE_ORIGIN, "characterIds": ids},
    )


def fetch_name_batch(page: Page, ids: list[str]) -> tuple[list[dict[str, str]], list[str]]:
    rows = {}
    skipped_ids = set()
    pending_ids = list(ids)
    last_errors = []

    for attempt in range(1, MAX_RETRIES + 1):
        if not pending_ids:
            break
        results = fetch_name_batch_once(page, pending_ids)
        last_errors = [item for item in results if not item["ok"]]
        for item in results:
            if item.get("skip"):
                skipped_ids.add(item["id"])
            elif item["ok"]:
                rows[item["row"]["id"]] = item["row"]
        pending_ids = [item["id"] for item in last_errors]
        if pending_ids and attempt < MAX_RETRIES:
            time.sleep(attempt)

    if pending_ids:
        details = "；".join(f"{item['id']}: {item['error']}" for item in last_errors)
        raise RuntimeError(f"人物页面抓取失败：{details}")
    return [rows[character_id] for character_id in ids if character_id in rows], sorted(
        skipped_ids, key=int
    )


def collect_names(page: Page, ids: list[str]) -> tuple[list[dict[str, str]], list[str]]:
    rows = []
    skipped_ids = []
    for index in range(0, len(ids), BATCH_SIZE):
        batch_ids = ids[index : index + BATCH_SIZE]
        batch_rows, batch_skipped_ids = fetch_name_batch(page, batch_ids)
        rows.extend(batch_rows)
        skipped_ids.extend(batch_skipped_ids)
        completed = min(index + len(batch_ids), len(ids))
        print(f"\r已抓取 {completed}/{len(ids)}", end="", flush=True)
        if completed < len(ids):
            time.sleep(0.2)
    if ids:
        print()
    return rows, skipped_ids


def main() -> None:
    mode = parse_mode()
    existing_rows = read_existing_rows() if mode == "update" else []
    existing_ids = {item["id"] for item in existing_rows}

    with sync_playwright() as playwright:
        context = None
        try:
            context, channel = launch_browser(playwright)
            print(f"已启动 {channel}。")
            page = open_api_session(context)
            all_ids = collect_character_ids(page)
            pending_ids = (
                [character_id for character_id in all_ids if character_id not in existing_ids]
                if mode == "update"
                else all_ids
            )
            print(f"站点人物页面：{len(all_ids)}；本次需要抓取：{len(pending_ids)}。")
            if not pending_ids:
                print("没有需要新增的人物 ID，CSV 未改动。")
                return

            new_rows, skipped_ids = collect_names(page, pending_ids)
            if skipped_ids:
                print(
                    f"已跳过 {len(skipped_ids)} 个不含人物数据的占位页面："
                    f"{', '.join(skipped_ids)}"
                )
            if not new_rows:
                print("没有可写入的新人物，CSV 未改动。")
                return

            write_rows([*existing_rows, *new_rows])
            print(f"已写入 {OUTPUT_PATH}")
            missing_japanese_names = [item["id"] for item in new_rows if not item["japanese_name"]]
            if missing_japanese_names:
                print(f"以下 ID 未找到日文名：{', '.join(missing_japanese_names)}")
        finally:
            if context:
                context.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("已取消。")
    except Exception as error:
        raise SystemExit(str(error)) from error
