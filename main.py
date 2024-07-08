import os
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from dotenv import load_dotenv
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from rtm_client import RtmClient

load_dotenv()

app = FastAPI()
FRODOBOTS_API_URL = "https://frodobots-web-api.onrender.com/api/v1"

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
    global auth_response_data  # Declare global at the start of the function
    channel_name = os.getenv("CHANNEL_NAME")
    rtc_token = os.getenv("RTC_TOKEN")
    rtm_token = os.getenv("RTM_TOKEN")
    userid = os.getenv("USERID")
    app_id = os.getenv("APP_ID")

    if all([channel_name, rtc_token, rtm_token, userid, app_id]):
        auth_response_data = {
            "CHANNEL_NAME": channel_name,
            "RTC_TOKEN": rtc_token,
            "RTM_TOKEN": rtm_token,
            "USERID": userid,
            "APP_ID": app_id,
        }
        print(auth_response_data)
        return JSONResponse(content=auth_response_data)
    else:
        auth_header = os.getenv("SDK_API_TOKEN")
        bot_name = os.getenv("BOT_NAME")

        if not auth_header:
            raise HTTPException(status_code=500, detail="Authorization header not configured")
        if not bot_name:
            raise HTTPException(status_code=500, detail="Bot name not configured")

        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {auth_header}'
        }

        data = {
            "bot_name": bot_name
        }

        response = requests.post(
            FRODOBOTS_API_URL + "/sdk/token",
            headers=headers,
            json=data
        )

        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail="Failed to retrieve tokens")

        response_data = response.json()
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
    if not auth_response_data:
        await auth()

    app_id = auth_response_data.get("APP_ID", "")
    rtc_token= auth_response_data.get("RTC_TOKEN", "")
    rtm_token = auth_response_data.get("RTM_TOKEN", "")
    channel = auth_response_data.get("CHANNEL_NAME", "")
    uid= auth_response_data.get("USERID", "")

    with open("index.html", "r") as file:
        html_content = file.read()

    html_content = html_content.replace("{{ appid }}", app_id)
    html_content = html_content.replace("{{ rtc_token }}", rtc_token)
    html_content = html_content.replace("{{ rtm_token }}", rtm_token)
    html_content = html_content.replace("{{ channel }}", channel)
    html_content = html_content.replace("{{ uid }}", str(uid))


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
    print("")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
