import asyncio
import aiohttp
import base64
import numpy as np
import cv2


async def fetch_and_display_image():
    url = "http://localhost:8000/v1/front"

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        base64_image = data["front_frame"]

                        image_bytes = base64.b64decode(base64_image)
                        image_array = np.frombuffer(image_bytes, dtype=np.uint8)
                        image = cv2.imdecode(image_array, cv2.IMREAD_COLOR)

                        cv2.imshow("Rear Frame", image)
                        if cv2.waitKey(1) & 0xFF == ord("q"):
                            break
                    else:
                        print(f"Request error: {response.status}")
            except Exception as e:
                print(f"Error: {e}")

            await asyncio.sleep(0.01)

    cv2.destroyAllWindows()


async def main():
    await fetch_and_display_image()


if __name__ == "__main__":
    asyncio.run(main())
