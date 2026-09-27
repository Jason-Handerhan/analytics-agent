from functools import lru_cache

import msal

from app.config import GCP_PROJECT_ID, get_secret


@lru_cache
def _msal_app() -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        get_secret("power-bi-sp-client-id", GCP_PROJECT_ID),
        authority=f"https://login.microsoftonline.com/{get_secret('azure-tenant-id', GCP_PROJECT_ID)}",
        client_credential=get_secret("power-bi-sp-client-secret", GCP_PROJECT_ID),
    )


def get_power_bi_token() -> str:
    result = _msal_app().acquire_token_for_client(
        scopes=["https://analysis.windows.net/powerbi/api/.default"]
    )
    return result["access_token"]
