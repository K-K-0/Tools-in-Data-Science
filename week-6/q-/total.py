from playwright.sync_api import sync_playwright

urls = [
    'https://sanand0.github.io/tdsdata/js_table/?seed=7',
    'https://sanand0.github.io/tdsdata/js_table/?seed=8',
    'https://sanand0.github.io/tdsdata/js_table/?seed=9',
    'https://sanand0.github.io/tdsdata/js_table/?seed=10',
    'https://sanand0.github.io/tdsdata/js_table/?seed=11',
    'https://sanand0.github.io/tdsdata/js_table/?seed=12',
    'https://sanand0.github.io/tdsdata/js_table/?seed=13',
    'https://sanand0.github.io/tdsdata/js_table/?seed=14',
    'https://sanand0.github.io/tdsdata/js_table/?seed=15',
    'https://sanand0.github.io/tdsdata/js_table/?seed=16',

]

grand_total = 0

with sync_playwright() as p:

    browser = p.chromium.launch(headless=True)
    page = browser.new_page()

    for url in urls:

        page.goto(url)
        page.wait_for_load_state()

        while True:

            tables = page.locator("table")

            for i in range(tables.count()):

                rows = tables.nth(i).locator("tr")

                for row in rows.all():

                    cells = row.locator("td")

                    for cell in cells.all():

                        text = cell.inner_text().replace(",", "").strip()

                        try:
                            grand_total += float(text)
                        except:
                            pass

            next_btn = page.locator("text=Next")

            if next_btn.count() == 0:
                break

            next_btn.click()
            page.wait_for_load_state()

    browser.close()

print("Total =", grand_total)