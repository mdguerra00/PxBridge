from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(input("API ID: ").strip())
api_hash = input("API HASH: ").strip()
phone = input("Telefone com DDI: ").strip()

with TelegramClient(StringSession(), api_id, api_hash) as client:
    client.send_code_request(phone)
    code = input("Código recebido: ").strip()

    try:
        client.sign_in(phone, code)
    except Exception as e:
        if "password" in str(e).lower():
            password = input("Senha 2FA: ").strip()
            client.sign_in(password=password)
        else:
            raise

    print("\nSTRING SESSION:\n")
    print(client.session.save())