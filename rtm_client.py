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

        payload = {
            "destination": self.channel.replace('sdk_', '', 1),
            "enable_offline_messaging": False,
            "enable_historical_messaging": False,
            "payload": message_json
        }

        response = requests.post(url, headers=headers, json=payload, timeout=1.5)

        # Quiet by default (autonav sends this 10x/sec). Set RTM_CLIENT_DEBUG=1
        # in the environment for verbose logs during troubleshooting.
        import os as _os
        if _os.getenv("RTM_CLIENT_DEBUG"):
            print(response)
            print(response.status_code)
            print(response.json())

        if response.status_code != 200:
            # Only surface non-200s. Raise so caller can log/handle.
            body = None
            try:
                body = response.json()
            except Exception:
                body = response.text
            raise RuntimeError(f"RTM send failed: {response.status_code} {body}")
