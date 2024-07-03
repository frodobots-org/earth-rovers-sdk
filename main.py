from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import agora
import time
from agora_token_builder import RtcTokenBuilder
import os
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()
# Replace with your Agora App ID and Certificate
AGORA_APP_ID = os.getenv('AGORA_APP_ID')
AGORA_APP_CERTIFICATE = os.getenv('AGORA_APP_CERTIFICATE')

def generate_agora_token(channel_name, uid):
    current_time = int(time.time())
    expire_time = current_time + 3600  # Token valid for 1 hour
    return RtcTokenBuilder.buildTokenWithUid(AGORA_APP_ID, AGORA_APP_CERTIFICATE, channel_name, uid, 0, expire_time)

@app.get("/token")
def get_token(channel_name: str, uid: int):
    token = generate_agora_token(channel_name, uid)
    return {"token": token}

@app.get("/")
def get_index():
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Agora WebRTC</title>
        <script src="https://cdn.agora.io/sdk/release/AgoraRTC_N.js"></script>
    </head>
    <body>
        <div id="video" style="width: 640px; height: 480px;"></div>
        <script>
            const client = AgoraRTC.createClient({ mode: "rtc", codec: "vp8" });

            async function startCall() {
                const response = await fetch('/token?channel_name=test&uid=1234');
                const data = await response.json();
                const token = data.token;

                client.init("YOUR_AGORA_APP_ID", () => {
                    console.log("AgoraRTC client initialized");

                    client.join(token, "test", 1234, (uid) => {
                        console.log("User " + uid + " join channel successfully");

                        const localStream = AgoraRTC.createStream({
                            streamID: uid,
                            audio: true,
                            video: true,
                            screen: false
                        });

                        localStream.init(() => {
                            console.log("getUserMedia successfully");
                            localStream.play('video');
                            client.publish(localStream, (err) => {
                                console.log("Publish local stream error: " + err);
                            });
                        }, (err) => {
                            console.log("getUserMedia failed", err);
                        });
                    }, (err) => {
                        console.log("Join channel failed", err);
                    });
                }, (err) => {
                    console.log("AgoraRTC client init failed", err);
                });
            }

            startCall();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
