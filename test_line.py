import requests

# 填入你剛剛取得的兩串資料
LINE_CHANNEL_ACCESS_TOKEN = "M/jRCnZn9BKDcXbWtyWQMX30rYEduFHNTFfuPfjK/Y58AH5s5JtoD3lKgFFjYej/LQ6a8ak+QNl3lyf9wjBL+Noc0UgcmDfl8wvgUvZqSwJyEfVMw8acKLAvyDOn7YJwW6mQmhre8MYVYqUhogrUkwdB04t89/1O/w1cDnyilFU="
LINE_USER_ID = "U19fd3b1462644d9ae7f0840077c1f52b"

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
