from pyppeteer import launch

class BrowserService:
    def __init__(self):
        self.browser = None
        self.page = None

    async def initialize_browser(self):
        if not self.browser:
            self.browser = await launch(
                executablePath='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                headless=True,
                args=['--ignore-certificate-errors']
            )
            self.page = await self.browser.newPage()
            await self.page.setViewport({'width': 1280, 'height': 800})
            await self.page.setExtraHTTPHeaders({'Accept-Language': 'en-US,en;q=0.9'})
            await self.page.goto('http://localhost:8000', {'waitUntil': 'networkidle2'})
            await self.page.click('#join')
            await self.page.waitForSelector('video')
            await self.page.waitFor(2000)

    async def take_screenshot(self, output_path: str = 'screenshot.png') -> str:
        await self.initialize_browser()

        video_wrapper = await self.page.querySelector('#player-1000')
        video_element = await video_wrapper.querySelector('video')
        await video_element.screenshot({'path': output_path})
        return output_path

    async def data(self) -> dict:
        await self.initialize_browser()

        valor_de_mi_variable = await self.page.evaluate('''() => {
        return window.rtm_data;
        }''')

        return valor_de_mi_variable

    async def close_browser(self):
        if self.browser:
            await self.browser.close()
            self.browser = None
            self.page = None
