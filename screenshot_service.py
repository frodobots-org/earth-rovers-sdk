from pyppeteer import launch

class ScreenshotService:
    async def take_screenshot(self, url: str, output_path: str = 'screenshot.png') -> FileResponse:
        browser = await launch(
            executablePath='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
            headless=True,
            args=['--ignore-certificate-errors']
        )
        page = await browser.newPage()

        await page.setExtraHTTPHeaders({'Accept-Language': 'en-US,en;q=0.9'})

        page.on('console', lambda msg: print('PAGE LOG:', msg.text))
        page.on('error', lambda err: print('PAGE ERROR:', err))
        page.on('pageerror', lambda pageErr: print('PAGE ERROR:', pageErr))

        await page.goto(url, {'waitUntil': 'networkidle2'})
        await page.click('#join')
        await page.waitForSelector('video')
        await page.waitFor(2000)  # Wait for 2 seconds

        video_element = await page.querySelector('video')
        await video_element.screenshot({'path': output_path})

        await browser.close()
        return output_path
