import asyncio
import base64
import cv2
import numpy as np
import websockets
import json


async def process_frame(frame_data, window_name):
    frame = cv2.imdecode(
        np.frombuffer(base64.b64decode(frame_data), np.uint8), cv2.IMREAD_COLOR
    )
    cv2.imshow(window_name, frame)


async def display_video():
    uri = "ws://localhost:8000/api/ws/screenshots"
    async with websockets.connect(uri, max_size=None) as websocket:
        while True:
            message = await websocket.recv()
            data = json.loads(message)

            # Procesar las imágenes en paralelo
            rear_task = asyncio.create_task(
                process_frame(data["rear_frame"], "Rear Frame")
            )
            front_task = asyncio.create_task(
                process_frame(data["front_frame"], "Front Frame")
            )

            await asyncio.gather(rear_task, front_task)

            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

    cv2.destroyAllWindows()


asyncio.run(display_video())
