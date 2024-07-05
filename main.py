from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from dotenv import load_dotenv
from fastapi.staticfiles import StaticFiles
import requests
import os
from pydantic import BaseModel

from rtm_client import RtmClient

load_dotenv()

app = FastAPI()

class AuthResponse(BaseModel):
    CHANNEL_NAME: str
    RTC_TOKEN: str
    RTM_TOKEN: str
    USERID: int
    APP_ID: str

# In-memory storage for the response
auth_response_data = {}

app.mount("/static", StaticFiles(directory="./static"), name="static")

@app.post("/auth")
async def auth():
    # Read the authorization header from environment variables
    auth_header = os.getenv("SDK_API_TOKEN")
    bot_name = os.getenv("BOT_NAME")

    if not auth_header:
        raise HTTPException(status_code=500, detail="Authorization header not configured")
    if not bot_name:
        raise HTTPException(status_code=500, detail="Bot name not configured")

    # Define the headers
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {auth_header}'
    }

    # Define the payload
    data = {
        "bot_name": bot_name
    }

    # Make the POST request
    response = requests.post(
        # 'https://frodobots-web-api.onrender.com/api/v1/sdk/token',
        'http://0.0.0.0:3000/api/v1/sdk/token',
        headers=headers,
        json=data
    )

    # Check if the request was successful
    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail="Failed to retrieve tokens")

    # Parse the response
    response_data = response.json()
    global auth_response_data
    auth_response_data = {
        "CHANNEL_NAME": response_data.get("CHANNEL_NAME"),
        "RTC_TOKEN": response_data.get("RTC_TOKEN"),
        "RTM_TOKEN": response_data.get("RTM_TOKEN"),
        "USERID": response_data.get("USERID"),
        "APP_ID": response_data.get("APP_ID"),
    }

    print(auth_response_data)

    return JSONResponse(content=response_data)

@app.get("/")
async def get_index(request: Request):
    # Ensure auth() is called asynchronously
    if not auth_response_data:
        await auth()

    # Ensure auth_response_data is accessed after auth() has been called
    app_id = auth_response_data.get("APP_ID", "3b64a6f5683d4abe9a7f3f72b7e7e9c8")
    token = auth_response_data.get("RTC_TOKEN", "")  # Provide a default empty string
    channel = auth_response_data.get("CHANNEL_NAME", "")  # Provide a default empty string as well

    # Convert all values to strings to prevent TypeError in replace()
    app_id = str(app_id)
    token = str(token)
    channel = str(channel)

    with open("index.html", "r") as file:
        html_content = file.read()

    html_content = html_content.replace("{{ appid }}", app_id)
    html_content = html_content.replace("{{ token }}", token)
    html_content = html_content.replace("{{ channel }}", channel)

    return HTMLResponse(content=html_content, status_code=200)

@app.post("/control")
async def control(request: Request):
    if not auth_response_data:
        await auth()

    body = await request.json()
    command = body.get("command")
    if not command:
        raise HTTPException(status_code=400, detail="Command not provided")

    RtmClient(auth_response_data).send_message(command)

    return {"message": "Command sent successfully"}

@app.get("/screenshot")
def get_screenshot():
    # This functionality depends on your application's architecture
    # If you have a way to capture screenshots from the video stream, implement it here
    # For example, returning a placeholder response
    return JSONResponse(content={"image": "BLOB_PLACEHOLDER", "timestamp": "2021-07-13T20:00:00Z"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
