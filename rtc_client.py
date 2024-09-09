import agorartc
import cv2
import numpy as np
import time
import ctypes
import sys
import traceback

APP_ID = "3b64a6f5683d4abe9a7f3f72b7e7e9c8"
CHANNEL_NAME = "sdk_frodobot_40c2f4"
TOKEN = "0063b64a6f5683d4abe9a7f3f72b7e7e9c8IACzvx9wOxhc/UnqQ8eUEKypVBtizCG+tNQ28EbHHMQVsEHx+07dZY3dIgCcKcIVuvjfZgQAAQB6395mAgB6395mAwB6395mBAB6395m"
USER_ID = 29656  # Spectator user ID

captured_frame = None
connection_state = "DISCONNECTED"
remote_users = {}

class MyRtcEngineEventHandler(agorartc.RtcEngineEventHandlerBase):
    def onJoinChannelSuccess(self, channel, uid, elapsed):
        global connection_state
        print(f"Successfully joined channel {channel} with UID {uid}")
        connection_state = "CONNECTED"

    def onUserJoined(self, uid, elapsed):
        print(f"Remote user {uid} joined the channel")
        remote_users[uid] = {"uid": uid}

    def onUserOffline(self, uid, reason):
        print(f"Remote user {uid} left the channel")
        if uid in remote_users:
            del remote_users[uid]

    def onConnectionStateChanged(self, state, reason):
        global connection_state
        states = ["DISCONNECTED", "CONNECTING", "CONNECTED", "RECONNECTING", "FAILED"]
        if state < len(states):
            print(f"Connection state changed to {states[state]}, reason: {reason}")
            connection_state = states[state]

class MyVideoFrameObserver(agorartc.VideoFrameObserver):
    def onRenderVideoFrame(self, uid, width, height, yBuffer, uBuffer, vBuffer, yStride, uStride, vStride, rotation):
        global captured_frame
        print(f"Received video frame from user {uid}: {width}x{height}")

        try:
            # Minimal implementation: just acknowledge receipt of frame
            captured_frame = True
            print(f"Acknowledged frame from user {uid}")
        except Exception as e:
            print(f"Error in onRenderVideoFrame for user {uid}: {e}")
            print(f"Error type: {type(e).__name__}")
            print(f"Error args: {e.args}")
            print(f"Traceback:")
            traceback.print_exc()

        return True

def main():
    rtc = agorartc.createRtcEngineBridge()
    eventHandler = MyRtcEngineEventHandler()
    rtc.initEventHandler(eventHandler)

    ret = rtc.initialize(APP_ID, None, agorartc.AREA_CODE_GLOB & 0xFFFFFFFF)
    print(f"Initialize result: {ret}")

    vfo = MyVideoFrameObserver()
    agorartc.registerVideoFrameObserver(rtc, vfo)

    # Set client role to Audience
    rtc.setClientRole(agorartc.CLIENT_ROLE_AUDIENCE)

    # Join channel with USER_ID
    ret = rtc.joinChannel(TOKEN, CHANNEL_NAME, "", USER_ID)
    print(f"Join channel result: {ret}")

    rtc.enableVideo()
    rtc.enableLocalVideo(False)  # Disable local video to avoid conflicts

    print("Waiting for remote video frame...")
    start_time = time.time()
    timeout = 30  # 30 seconds timeout

    try:
        while captured_frame is None and time.time() - start_time < timeout:
            print(f"Connection state: {connection_state}")
            print(f"Remote users: {len(remote_users)}")
            print(f"Time elapsed: {time.time() - start_time:.2f} seconds")
            time.sleep(1)

            if connection_state == "CONNECTED" and len(remote_users) == 0:
                print("Connected but no remote users. Make sure there's an active stream in the channel.")

            if connection_state == "FAILED":
                print("Connection failed. Please check your network and Agora settings.")
                break

    except Exception as e:
        print(f"Error in main loop: {e}")
        traceback.print_exc()

    finally:
        try:
            agorartc.unregisterVideoFrameObserver(rtc, vfo)
            rtc.leaveChannel()
            rtc.release()
        except Exception as e:
            print(f"Error during cleanup: {e}")
            traceback.print_exc()

    if captured_frame is not None:
        print("Frame was acknowledged.")
    else:
        print("No video frame was acknowledged within the timeout period.")
        print("Possible reasons:")
        print("1. No active video stream in the channel")
        print("2. Token might have expired")
        print("3. Network issues")
        print("4. Incorrect channel name or APP ID")

if __name__ == "__main__":
    main()
