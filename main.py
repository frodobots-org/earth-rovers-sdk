from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from dotenv import load_dotenv
from fastapi.staticfiles import StaticFiles

# from rtm_client import RtmClient

load_dotenv()

app = FastAPI()

app.mount("/static", StaticFiles(directory="./static"), name="static")

@app.get("/")
def get_index():
    with open("index.html", "r") as file:
        html_content = file.read()
    return HTMLResponse(content=html_content, status_code=200)

@app.post("/control")
async def control(request: Request):
    body = await request.json()
    command = body.get("command")
    if not command:
        raise HTTPException(status_code=400, detail="Command not provided")

    # Here you would send the command via RTM session
    # For example:
    # agora_client.send_rtm_message(command)

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
