from playwright.sync_api import sync_playwright

URLS = [f'https://sanand0.github.io/tdsdata/js_table/?seed={i}' for i in range(85, 95)]
grand_total = 0.0

def scrape_current_page_fast(page):
    # This evaluates JS inside Chrome memory, bypassing Playwright loop overhead entirely
    return page.evaluate("""() => {
        let total = 0;
        let cells = document.querySelectorAll('table td');
        cells.forEach(cell => {
            let text = cell.innerText.replace(/,/g, '').trim();
            let val = parseFloat(text);
            if (!isNaN(val)) {
                total += val;
            }
        });
        return total;
    }""")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    
    for url in URLS:
        print(f"Visiting {url}")
        page.goto(url)
        page.wait_for_load_state("domcontentloaded") # Faster than waiting for networkidle
        
        while True:
            grand_total += scrape_current_page_fast(page)
            
            next_btn = page.locator("text=Next").first
            if next_btn.count() == 0 or not next_btn.is_visible():
                break
                
            next_btn.click()
            page.wait_for_load_state("domcontentloaded")

    browser.close()
    print(f"\nTOTAL = {grand_total}")
