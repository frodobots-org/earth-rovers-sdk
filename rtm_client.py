import requests

"""
    RTM Client for handling real time messages send to the Bot
"""
class RtmClient:
    APP_ID = "3b64a6f5683d4abe9a7f3f72b7e7e9c8"
    BASE_URL = f"https://api.agora.io/dev/v2/project/{APP_ID}"

    def __init__(self, token: str, uid: int, channel: str):
        self.app_id = "3b64a6f5683d4abe9a7f3f72b7e7e9c8"
        self.channel = channel
        self.token = token
        self.uid = uid

    @staticmethod
    def create_client(app_id: str, token: str):
        client = RtmClient(app_id, token)
        return client


    def send_message(self, message: str):
        url = f"{RtmClient.BASE_URL}/rtm/users/{self.token}/peer_messages"
        headers = {
            "x-agora-uid": self.uid,
            "x-agora-token": self.token
        }
        payload = {
            "destination": self.channel,
            "enable_offline_messaging": False,
            "enable_historical_messaging": False,
            "payload": message
        }
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code == 200:
            print("Message sent successfully")
        else:
            print("Failed to send message")

if __name__ == "__main__":
    rtm_token = "0063b64a6f5683d4abe9a7f3f72b7e7e9c8IAAwmb8LnqPemSKNisjDrsOUzLaKP3ruecLPV7o+ijq7uqQcwhgh39v0IgDgF+rIatCHZgQAAQAqt4ZmAgAqt4ZmAwAqt4ZmBAAqt4Zm"
    channel = "frodobot_1947bf"
    uid=4857

    rtm_client = RtmClient(rtm_token, channel, uid)
    rtm_client.send_message("{linear: 0, angular: 0, lamp: 1}")
