import requests
import json

class RtmClient:
    def __init__(self, auth_response_data):
        self.app_id = auth_response_data.get("APP_ID")
        self.channel = auth_response_data.get("CHANNEL_NAME")
        self.token = auth_response_data.get("RTM_TOKEN")
        self.uid = str(auth_response_data.get("USERID"))

    def send_message(self, message: dict):
        # Convert the message dictionary to a JSON string
        message_json = json.dumps(message, separators=(',', ':'))

        url = f"https://api.agora.io/dev/v2/project/{self.app_id}/rtm/users/{self.uid}/peer_messages"
        headers = {
            "x-agora-uid": self.uid,
            "x-agora-token": self.token
        }
        print("IMPORTANT: destination is the remote user for example: frodobot_72b943 (not the channel)")
        payload = {
            "destination": self.channel,
            "enable_offline_messaging": False,
            "enable_historical_messaging": False,
            "payload": message_json
        }

        response = requests.post(url, headers=headers, json=payload)

        print(response)
        print(response.status_code)
        print(response.json())

        if response.status_code == 200:
            print("Message sent successfully")
        else:
            print(response.json())
