"""Rode UMA vez no seu computador para gerar o token do YouTube.
Precisa do client_secret.json (OAuth, tipo "App para computador") na mesma pasta.
Copie o texto impresso para o Secret YT_TOKEN_JSON do GitHub."""
from google_auth_oauthlib.flow import InstalledAppFlow

flow = InstalledAppFlow.from_client_secrets_file(
    "client_secret.json", ["https://www.googleapis.com/auth/youtube.upload"])
creds = flow.run_local_server(port=0, prompt="consent")
print("\n=== COPIE TUDO ABAIXO PARA O SECRET YT_TOKEN_JSON ===\n")
print(creds.to_json())

