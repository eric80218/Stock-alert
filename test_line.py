import requests

# 填入你剛剛取得的兩串資料
LINE_CHANNEL_ACCESS_TOKEN = "你的_CHANNEL_ACCESS_TOKEN"
LINE_USER_ID = "你的_U開頭_USER_ID"

def send_test_message():
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
    }
    payload = {
        "to": LINE_USER_ID,
        "messages": [
            {
                "type": "text", 
                "text": "🎉 LINE 股票到價推播串接成功！"
            }
        ]
    }
    
    response = requests.post(url, headers=headers, json=payload)
    if response.status_code == 200:
        print("✅ 測試訊息發送成功！請檢查手機 LINE。")
    else:
        print(f"❌ 發送失敗，狀態碼: {response.status_code}, 原因: {response.text}")

if __name__ == "__main__":
    send_test_message()
