import os
import base64
from typing import Optional
import requests
from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from .utils import log, Verbosity, LogLevel

# Scopes required for Discovery Engine API
SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

# Discovery Engine API Base URL
BASE_URL = "https://us-discoveryengine.googleapis.com/v1alpha"


def get_credentials(
    client_secrets_path: str, token_path: str, verbose: Verbosity = Verbosity.STATUS
) -> Optional[Credentials]:
    """
    Get OAuth2 credentials via browser flow.
    Caches token to token_path.
    """
    creds = None
    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)  # type: ignore
        except (IOError, ValueError) as err:
            log(f"Error loading token: {err}", verbose, level=LogLevel.ERROR)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as err:  # pylint: disable=broad-exception-caught
                log(f"Error refreshing token: {err}", verbose, level=LogLevel.ERROR)
                creds = None

        if not creds:
            if not os.path.exists(client_secrets_path):
                log(
                    f"Error: Client secrets file not found at {client_secrets_path}",
                    verbose,
                    level=LogLevel.ERROR,
                )
                return None
            try:
                flow = InstalledAppFlow.from_client_secrets_file(
                    client_secrets_path, SCOPES
                )
                creds = flow.run_local_server(port=0)
            except Exception as err:  # pylint: disable=broad-exception-caught
                log(f"Error during OAuth flow: {err}", verbose, level=LogLevel.ERROR)
                return None

        # Save credentials for next time
        try:
            with open(token_path, "w", encoding="utf-8") as token_fh:
                token_fh.write(creds.to_json())
        except (IOError, ValueError) as err:
            log(
                f"Warning: Could not save token: {err}",
                verbose,
                level=LogLevel.PROGRESS,
            )

    return creds if creds else None


def create_notebook(
    project_id: str,
    title: str,
    creds: Credentials,
    verbose: Verbosity = Verbosity.STATUS,
) -> Optional[str]:
    """Create a new notebook in NotebookLM Enterprise."""
    location = "us"
    parent = f"projects/{project_id}/locations/{location}"
    url = f"{BASE_URL}/{parent}/notebooks"

    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
    }
    payload = {"title": title}

    log(f"Creating notebook '{title}'...", verbose, level=LogLevel.STATUS)
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=30)
        res.raise_for_status()
        notebook = res.json()
        notebook_id = notebook["name"].split("/")[-1]
        log(f"Notebook created: {notebook_id}", verbose, level=LogLevel.PROGRESS)
        return str(notebook_id)
    except requests.RequestException as err:
        log(f"Error creating notebook: {err}", verbose, level=LogLevel.ERROR)
        if err.response is not None:
            log(f"Response: {err.response.text}", verbose, level=LogLevel.ERROR)
        return None


def upload_source(
    project_id: str,
    notebook_id: str,
    file_path: str,
    creds: Credentials,
    verbose: Verbosity = Verbosity.STATUS,
) -> bool:
    """Upload a file as a source to the notebook."""
    location = "us"
    parent = f"projects/{project_id}/locations/{location}/notebooks/{notebook_id}"
    url = f"{BASE_URL}/{parent}/sources:uploadFile"

    if not os.path.exists(file_path):
        log(f"Error: File not found {file_path}", verbose, level=LogLevel.ERROR)
        return False

    display_name = os.path.basename(file_path)
    try:
        with open(file_path, "rb") as file_fh:
            content = file_fh.read()
            encoded_content = base64.b64encode(content).decode("utf-8")
    except IOError as err:
        log(f"Error reading {file_path}: {err}", verbose, level=LogLevel.ERROR)
        return False

    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
    }
    payload = {
        "userContent": {
            "displayName": display_name,
            "content": encoded_content,
            "mimeType": "text/plain",
        }
    }

    log(f"Uploading {display_name}...", verbose, level=LogLevel.PROGRESS)
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=60)
        res.raise_for_status()
        return True
    except requests.RequestException as err:
        log(f"Error uploading {display_name}: {err}", verbose, level=LogLevel.ERROR)
        if err.response is not None:
            log(f"Response: {err.response.text}", verbose, level=LogLevel.ERROR)
        return False
