"""One-time Google sign-in. Run:  python -m opzy.auth

A browser window opens; sign in with the TEST business Gmail and allow access.
This saves token.json next to credentials.json. Keep both files private.
"""
from google_auth_oauthlib.flow import InstalledAppFlow

from .config import settings
from .google_tools import SCOPES


def main():
    flow = InstalledAppFlow.from_client_secrets_file(settings.google_credentials_file, SCOPES)
    creds = flow.run_local_server(port=0)
    with open(settings.google_token_file, "w") as f:
        f.write(creds.to_json())
    print(f"Saved {settings.google_token_file}. You're connected.")


if __name__ == "__main__":
    main()
