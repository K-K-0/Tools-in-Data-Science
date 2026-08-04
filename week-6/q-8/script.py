from playwright.sync_api import sync_playwright
import re

# Paste your Seed 85-94 URLs here
URLS = [
    'https://sanand0.github.io/tdsdata/js_table/?seed=85',
    'https://sanand0.github.io/tdsdata/js_table/?seed=86',
    'https://sanand0.github.io/tdsdata/js_table/?seed=87',
    'https://sanand0.github.io/tdsdata/js_table/?seed=88',
    'https://sanand0.github.io/tdsdata/js_table/?seed=89',
    'https://sanand0.github.io/tdsdata/js_table/?seed=90',
    'https://sanand0.github.io/tdsdata/js_table/?seed=91',
    'https://sanand0.github.io/tdsdata/js_table/?seed=92',
    'https://sanand0.github.io/tdsdata/js_table/?seed=93',
    'https://sanand0.github.io/tdsdata/js_table/?seed=94',

]

grand_total = 0.0


def scrape_current_page(page):
    total = 0.0

    tables = page.locator("table")
    table_count = tables.count()

    for i in range(table_count):
        table = tables.nth(i)

        rows = table.locator("tr")
        row_count = rows.count()

        for r in range(row_count):
            cells = rows.nth(r).locator("td")
            cell_count = cells.count()

            for c in range(cell_count):
                text = cells.nth(c).inner_text().strip()

                # remove commas
                text = text.replace(",", "")

                try:
                    total += float(text)
                except ValueError:
                    pass

    return total


with sync_playwright() as p:

    browser = p.chromium.launch(headless=True)
    page = browser.new_page()

    for url in URLS:

        print(f"Visiting {url}")

        page.goto(url)
        page.wait_for_load_state("networkidle")

        while True:

            grand_total += scrape_current_page(page)

            # Find a Next button/link
            next_btn = page.locator("text=Next")

            if next_btn.count() == 0:
                break

            if not next_btn.first.is_visible():
                break

            next_btn.first.click()
            page.wait_for_load_state("networkidle")

    browser.close()

print(f"\nTOTAL = {grand_total}")