"""One-off manual utility: mints a real, delegated Entra access token via
MSAL's interactive browser flow (auth code + PKCE), for curl-testing the
gateway without Power Apps.

Device-code flow (an earlier version of this script) is blocked outright by
Microsoft-managed Conditional Access policy in this tenant -- AADSTS530035,
confirmed live. Interactive flow carries full Conditional Access context and
is the documented alternative -- it's also the same flow family Power
Platform's own connector already uses successfully here.

Requires a "Mobile and desktop applications" platform redirect URI of
http://localhost registered on analytics-agent-connector-client (Entra portal
-> App registrations -> that app -> Authentication -> Add a platform) --
additive, doesn't touch the existing Power Platform redirect URI.

Run:
    uv run python scripts/get_dev_token.py
"""
import msal

TENANT_ID = "7e6d319c-ffb2-4bbf-8865-d2e7580a8998"
CLIENT_ID = "4c41170b-1f11-4a92-964c-421dcb35f19b"
SCOPE = "api://4b86032f-4507-4239-bf90-a9b1c33c571c/access_as_user"


def main() -> None:
    app = msal.PublicClientApplication(CLIENT_ID, authority=f"https://login.microsoftonline.com/{TENANT_ID}")
    result = app.acquire_token_interactive(scopes=[SCOPE])  # opens a system browser window
    if "access_token" not in result:
        raise RuntimeError(f"Token acquisition failed: {result}")
    print(result["access_token"])


if __name__ == "__main__":
    main()
