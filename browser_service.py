import os
from pyppeteer import launch


class BrowserService:
    def __init__(self):
        self.browser = None
        self.page = None
        self.default_viewport = {"width": 3840, "height": 2160}

    async def initialize_browser(self):
        if not self.browser:
            try:
                executable_path = os.getenv(
                    "CHROME_EXECUTABLE_PATH",
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                )
                self.browser = await launch(
                    executablePath=executable_path,
                    headless=True,
                    args=[
                        "--ignore-certificate-errors",
                        "--no-sandbox",
                        f"--window-size={self.default_viewport['width']},{self.default_viewport['height']}",
                    ],
                )
                self.page = await self.browser.newPage()
                await self.page.setViewport(self.default_viewport)
                await self.page.setExtraHTTPHeaders(
                    {"Accept-Language": "en-US,en;q=0.9"}
                )
                await self.page.goto(
                    "http://127.0.0.1:8000", {"waitUntil": "networkidle2"}
                )
                await self.page.click("#join")
                await self.page.waitForSelector("video")
                await self.page.waitForSelector("#map")
                await self.page.setViewport(self.default_viewport)

                await self.page.waitFor(2000)
            except Exception as e:
                print(f"Error initializing browser: {e}")
                self.browser = None
                self.page = None
                await self.close_browser()
                raise

    async def take_screenshot(self, video_output_folder: str):
        await self.initialize_browser()

        # Get the full page dimensions
        dimensions = await self.page.evaluate(
            """() => {
            return {
                width: Math.max(document.documentElement.scrollWidth, window.innerWidth),
                height: Math.max(document.documentElement.scrollHeight, window.innerHeight),
            }
        }"""
        )

        # If the content is larger than the default viewport, adjust it
        if (
            dimensions["width"] > self.default_viewport["width"]
            or dimensions["height"] > self.default_viewport["height"]
        ):
            await self.page.setViewport(dimensions)

        # Take a full page screenshot
        # await self.page.screenshot({"path": "full_page.png", "fullPage": True})

        # Capture individual elements
        for element_id, output_path in [
            ("#player-1000", f"{video_output_folder}/front.png"),
            ("#player-1001", f"{video_output_folder}/rear.png"),
            ("#map", f"{video_output_folder}/map.png"),
        ]:
            element = await self.page.querySelector(element_id)
            if element:
                await element.screenshot({"path": output_path})
            else:
                print(f"Element {element_id} not found")

        return (
            f"{video_output_folder}/front.png",
            f"{video_output_folder}/rear.png",
            f"{video_output_folder}/map.png",
        )

    async def data(self) -> dict:
        await self.initialize_browser()

        bot_data = await self.page.evaluate(
            """() => {
        return window.rtm_data;
        }"""
        )

        return bot_data

    async def close_browser(self):
        if self.browser:
            await self.browser.close()
            self.browser = None
            self.page = None
