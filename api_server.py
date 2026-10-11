from fastapi import FastAPI, Depends, Request
import psycopg2
from pydantic import BaseModel
from fastapi import UploadFile, File, Form
import os
import shutil
from fastapi import HTTPException
from fastapi.responses import FileResponse
import uuid
from fastapi.responses import Response
from urllib.parse import quote
from supabase import create_client
from dotenv import load_dotenv
import os
from fastapi.middleware.cors import CORSMiddleware
import base64
import json
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import jwt
import msal
import requests
from cryptography.fernet import Fernet
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

load_dotenv()

app = FastAPI()

@app.options("/{full_path:path}")
async def options_handler(full_path: str):
    return {"status": "OK"}

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "dtc-files")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# DATABASE CONNECTION
def get_connection():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    return psycopg2.connect(database_url, sslmode=os.getenv("DATABASE_SSLMODE", "require"))

def init_database():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(100) UNIQUE NOT NULL,
            password VARCHAR(100) NOT NULL,
            full_name VARCHAR(255),
            role VARCHAR(50) NOT NULL,
            supervisor_id INTEGER REFERENCES users(id) ON DELETE SET NULL
        );
    """)

    cur.execute("""
        ALTER TABLE users
        ADD COLUMN IF NOT EXISTS supervisor_id INTEGER REFERENCES users(id) ON DELETE SET NULL;
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS activity_logs (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            action_type VARCHAR(100) NOT NULL,
            target_file TEXT,
            log_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id SERIAL PRIMARY KEY,
            file_name VARCHAR(255) NOT NULL,
            file_type VARCHAR(50),
            file_path TEXT,
            uploaded_by INTEGER REFERENCES users(id) ON DELETE CASCADE,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    bootstrap_username = os.getenv("DTC_BOOTSTRAP_ADMIN_USERNAME")
    bootstrap_password = os.getenv("DTC_BOOTSTRAP_ADMIN_PASSWORD")
    if bootstrap_username and bootstrap_password:
        cur.execute("""
            INSERT INTO users (username, password, full_name, role)
            VALUES (%s, %s, %s, 'admin')
            ON CONFLICT (username) DO NOTHING;
        """, (bootstrap_username, bootstrap_password,
              os.getenv("DTC_BOOTSTRAP_ADMIN_NAME", "DTC Administrator")))

    conn.commit()
    cur.close()
    conn.close()

init_database()

# Microsoft Graph configuration and server-side delegated token storage.
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPES = ["https://graph.microsoft.com/Files.ReadWrite"]
security = HTTPBearer(auto_error=False)

def _jwt_key():
    key = os.getenv("DTC_JWT_SECRET")
    if not key:
        raise HTTPException(status_code=503, detail="DTC_JWT_SECRET is not configured")
    return key

def _fernet():
    key = os.getenv("EXCEL_TOKEN_ENCRYPTION_KEY")
    if not key:
        raise HTTPException(status_code=503, detail="Excel token encryption is not configured")
    try:
        return Fernet(key.encode("ascii"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Invalid Excel token encryption key") from exc

def _msal_app(cache=None):
    client_id = os.getenv("MS_CLIENT_ID")
    client_secret = os.getenv("MS_CLIENT_SECRET")
    tenant_id = os.getenv("MS_TENANT_ID")
    if not all((client_id, client_secret, tenant_id)):
        raise HTTPException(status_code=503, detail="Microsoft Entra app settings are incomplete")
    return msal.ConfidentialClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        client_credential=client_secret,
        token_cache=cache,
    )

def _load_token_cache():
    cache = msal.SerializableTokenCache()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT encrypted_cache FROM excel_token_cache WHERE id = 1")
            row = cur.fetchone()
        if row:
            cache.deserialize(_fernet().decrypt(bytes(row[0])).decode("utf-8"))
        return cache
    finally:
        conn.close()

def _save_token_cache(cache):
    encrypted = _fernet().encrypt(cache.serialize().encode("utf-8"))
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO excel_token_cache (id, encrypted_cache, updated_at)
                VALUES (1, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (id) DO UPDATE SET
                    encrypted_cache = EXCLUDED.encrypted_cache,
                    updated_at = CURRENT_TIMESTAMP
            """, (encrypted,))
        conn.commit()
    finally:
        conn.close()

def _get_graph_token():
    cache = _load_token_cache()
    app_msal = _msal_app(cache)
    accounts = app_msal.get_accounts()
    result = app_msal.acquire_token_silent(GRAPH_SCOPES, account=accounts[0]) if accounts else None
    if cache.has_state_changed:
        _save_token_cache(cache)
    if not result or "access_token" not in result:
        raise HTTPException(status_code=503, detail="Microsoft workbook is not connected; an administrator must connect it")
    return result["access_token"]

def _graph_item_path():
    share_url = os.getenv("GRAPH_WORKBOOK_SHARE_URL")
    if not share_url:
        raise HTTPException(status_code=503, detail="GRAPH_WORKBOOK_SHARE_URL is not configured")
    share_id = "u!" + base64.urlsafe_b64encode(share_url.encode("utf-8")).decode("ascii").rstrip("=")
    return f"shares/{share_id}/driveItem/workbook"

def _graph(method, path, payload=None, params=None, session_id=None):
    headers = {"Authorization": f"Bearer {_get_graph_token()}", "Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if session_id:
        headers["workbook-session-id"] = session_id
    response = requests.request(method, f"{GRAPH_BASE}/{path.lstrip('/')}", headers=headers,
                                json=payload, params=params, timeout=45)
    if response.status_code >= 400:
        # Avoid returning response bodies that can contain tenant or workbook details.
        raise HTTPException(status_code=502, detail=f"Microsoft Graph request failed ({response.status_code})")
    return response.json() if response.content else {}

def _require_dtc_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials:
        raise HTTPException(status_code=401, detail="DTC sign-in required")
    try:
        claims = jwt.decode(credentials.credentials, _jwt_key(), algorithms=["HS256"])
        if not isinstance(claims.get("id"), int) or claims.get("role") not in {"admin", "supervisor", "user"}:
            raise ValueError("invalid claims")
        return claims
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired DTC session") from exc

def _require_admin(user=Depends(_require_dtc_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Administrator access required")
    return user

def _init_excel_auth_tables():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS excel_token_cache (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    encrypted_cache BYTEA NOT NULL,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS excel_oauth_flows (
                    state VARCHAR(128) PRIMARY KEY,
                    flow_json TEXT NOT NULL,
                    expires_at TIMESTAMP NOT NULL
                )
            """)
        conn.commit()
    finally:
        conn.close()

_init_excel_auth_tables()

class LoginRequest(BaseModel):
    username: str
    password: str

@app.get("/")
def home():
    return {"message": "DTC FastAPI Server Running"}

@app.get("/test-db")
def test_db():
    try:
        conn = get_connection()
        conn.close()
        return {"status": "Database Connected Successfully"}
    except Exception as e:
        return {"error": str(e)}

@app.post("/login")
def login(data: LoginRequest):
    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("""
            SELECT id, username, full_name, role
            FROM users
            WHERE username = %s AND password = %s
        """, (data.username, data.password))

        user = cur.fetchone()

        cur.close()
        conn.close()

        if user:
            session_token = jwt.encode(
                {
                    "id": int(user[0]),
                    "username": user[1],
                    "role": user[3],
                    "exp": datetime.now(timezone.utc) + timedelta(hours=8),
                    "iat": datetime.now(timezone.utc),
                },
                _jwt_key(),
                algorithm="HS256",
            )
            return {
                "success": True,
                "message": "Login successful",
                "access_token": session_token,
                "user": {
                    "id": user[0],
                    "username": user[1],
                    "full_name": user[2],
                    "role": user[3]
                }
            }

        return {
            "success": False,
            "message": "Invalid username or password"
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@app.post("/excel/connect-url")
def excel_connect_url(_admin=Depends(_require_admin)):
    """Start the one-time delegated Microsoft sign-in in an administrator's browser."""
    redirect_uri = os.getenv("MS_REDIRECT_URI")
    if not redirect_uri:
        raise HTTPException(status_code=503, detail="MS_REDIRECT_URI is not configured")
    state = secrets.token_urlsafe(32)
    flow = _msal_app().initiate_auth_code_flow(
        scopes=GRAPH_SCOPES,
        redirect_uri=redirect_uri,
        state=state,
    )
    if "auth_uri" not in flow:
        raise HTTPException(status_code=503, detail="Could not start Microsoft sign-in")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM excel_oauth_flows WHERE expires_at < CURRENT_TIMESTAMP")
            cur.execute("INSERT INTO excel_oauth_flows (state, flow_json, expires_at) VALUES (%s, %s, %s)",
                        (state, json.dumps(flow), datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10)))
        conn.commit()
    finally:
        conn.close()
    return {"authorization_url": flow["auth_uri"]}


@app.get("/excel/oauth/callback")
def excel_oauth_callback(request: Request, code: str | None = None, state: str | None = None,
                         error: str | None = None, error_description: str | None = None):
    if error:
        return {"success": False, "message": "Microsoft sign-in was not completed"}
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing Microsoft sign-in response")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT flow_json FROM excel_oauth_flows WHERE state = %s AND expires_at > CURRENT_TIMESTAMP", (state,))
            row = cur.fetchone()
            if row:
                cur.execute("DELETE FROM excel_oauth_flows WHERE state = %s", (state,))
        conn.commit()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=400, detail="Microsoft sign-in expired; start again from DTC")
    flow = json.loads(row[0])
    cache = msal.SerializableTokenCache()
    result = _msal_app(cache).acquire_token_by_auth_code_flow(flow, dict(request.query_params))
    if "access_token" not in result:
        raise HTTPException(status_code=401, detail="Microsoft sign-in failed")
    allowed_account = (os.getenv("MS_ALLOWED_USER") or "").strip().casefold()
    claims = result.get("id_token_claims") or {}
    signed_in_account = (claims.get("preferred_username") or claims.get("email") or "").strip().casefold()
    if not allowed_account or signed_in_account != allowed_account:
        raise HTTPException(status_code=403, detail="This Microsoft account is not allowed to connect the workbook")
    _save_token_cache(cache)
    return {"success": True, "message": "Microsoft workbook connection completed. You may return to DTC."}


@app.get("/excel/status")
def excel_status(_user=Depends(_require_dtc_user)):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT updated_at FROM excel_token_cache WHERE id = 1")
            row = cur.fetchone()
    finally:
        conn.close()
    return {"connected": bool(row), "connected_at": str(row[0]) if row else None}


@app.get("/excel/tables")
def excel_tables(_user=Depends(_require_dtc_user)):
    result = _graph("GET", f"{_graph_item_path()}/tables", params={"$select": "id,name,showHeaders,showTotals"})
    return {"tables": result.get("value", [])}


@app.get("/excel/tables/{table_id}/rows")
def excel_rows(table_id: str, skip: int = 0, top: int = 100, _user=Depends(_require_dtc_user)):
    if skip < 0 or top < 1 or top > 500:
        raise HTTPException(status_code=400, detail="Invalid page size")
    path = f"{_graph_item_path()}/tables/{quote(table_id, safe='')}/rows"
    result = _graph("GET", path, params={"$top": top, "$skip": skip})
    return {"rows": result.get("value", []), "next_link": result.get("@odata.nextLink")}

@app.get("/excel/tables/{table_id}/columns")
def excel_columns(table_id: str, _user=Depends(_require_dtc_user)):
    result = _graph("GET", f"{_graph_item_path()}/tables/{quote(table_id, safe='')}/columns")
    return {"columns": result.get("value", [])}


def _open_workbook_session():
    return _graph("POST", f"{_graph_item_path()}/createSession", {"persistChanges": True}).get("id")

def _close_workbook_session(session_id):
    if session_id:
        try:
            _graph("POST", f"{_graph_item_path()}/closeSession", {}, session_id=session_id)
        except HTTPException:
            pass

class ExcelRowUpdate(BaseModel):
    values: list[list[object]]

class ExcelRowAdd(BaseModel):
    values: list[list[object]]

@app.patch("/excel/tables/{table_id}/rows/{row_index}")
def excel_update_row(table_id: str, row_index: int, data: ExcelRowUpdate, _admin=Depends(_require_admin)):
    if row_index < 0 or len(data.values) != 1:
        raise HTTPException(status_code=400, detail="Provide one row and a valid row index")
    session_id = _open_workbook_session()
    try:
        result = _graph("PATCH", f"{_graph_item_path()}/tables/{quote(table_id, safe='')}/rows/{row_index}",
                        {"values": data.values}, session_id=session_id)
        return {"success": True, "row": result}
    finally:
        _close_workbook_session(session_id)

@app.post("/excel/tables/{table_id}/rows")
def excel_add_row(table_id: str, data: ExcelRowAdd, _admin=Depends(_require_admin)):
    if len(data.values) != 1:
        raise HTTPException(status_code=400, detail="Provide exactly one row")
    session_id = _open_workbook_session()
    try:
        result = _graph("POST", f"{_graph_item_path()}/tables/{quote(table_id, safe='')}/rows/add",
                        {"index": None, "values": data.values}, session_id=session_id)
        return {"success": True, "row": result}
    finally:
        _close_workbook_session(session_id)

@app.delete("/excel/tables/{table_id}/rows/{row_index}")
def excel_delete_row(table_id: str, row_index: int, _admin=Depends(_require_admin)):
    if row_index < 0:
        raise HTTPException(status_code=400, detail="Invalid row index")
    session_id = _open_workbook_session()
    try:
        _graph("DELETE", f"{_graph_item_path()}/tables/{quote(table_id, safe='')}/rows/{row_index}",
               session_id=session_id)
        return {"success": True}
    finally:
        _close_workbook_session(session_id)

@app.get("/users/visible/{user_id}/{role}")
def get_visible_users_api(user_id: int, role: str):
    try:
        conn = get_connection()
        cur = conn.cursor()

        if role == "admin":
            cur.execute("""
                SELECT id, username, full_name, role, supervisor_id
                FROM users
                ORDER BY id
            """)

        elif role == "supervisor":
            cur.execute("""
                SELECT id, username, full_name, role, supervisor_id
                FROM users
                WHERE supervisor_id = %s
                ORDER BY id
            """, (user_id,))

        else:
            cur.execute("""
                SELECT id, username, full_name, role, supervisor_id
                FROM users
                WHERE id = %s
                ORDER BY id
            """, (user_id,))

        rows = cur.fetchall()

        cur.close()
        conn.close()

        users = []
        for row in rows:
            users.append({
                "id": row[0],
                "username": row[1],
                "full_name": row[2],
                "role": row[3],
                "supervisor_id": row[4]
            })

        return {
            "success": True,
            "users": users
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }

@app.get("/supervisors")
def get_supervisors_api():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, username, full_name
        FROM users
        WHERE role IN ('admin', 'supervisor')
        ORDER BY role, id
    """)

    rows = cur.fetchall()
    cur.close()
    conn.close()

    return {
        "success": True,
        "supervisors": [
            {
                "id": r[0],
                "username": r[1],
                "full_name": r[2]
            }
            for r in rows
        ]
    }


class AddUserRequest(BaseModel):
    username: str
    password: str
    full_name: str
    role: str
    supervisor_id: int | None = None


@app.post("/users/add")
def add_user_api(data: AddUserRequest):
    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO users (username, password, full_name, role, supervisor_id)
            VALUES (%s, %s, %s, %s, %s)
        """, (
            data.username,
            data.password,
            data.full_name,
            data.role,
            data.supervisor_id
        ))

        conn.commit()
        cur.close()
        conn.close()

        return {"success": True, "message": "User added successfully"}

    except Exception as e:
        return {"success": False, "error": str(e)}


class UpdateUserRequest(BaseModel):
    user_id: int
    username: str
    password: str | None = None
    full_name: str
    role: str
    supervisor_id: int | None = None


@app.post("/users/update")
def update_user_api(data: UpdateUserRequest):
    try:
        conn = get_connection()
        cur = conn.cursor()

        if data.password:
            cur.execute("""
                UPDATE users
                SET username = %s,
                    password = %s,
                    full_name = %s,
                    role = %s,
                    supervisor_id = %s
                WHERE id = %s
            """, (
                data.username,
                data.password,
                data.full_name,
                data.role,
                data.supervisor_id,
                data.user_id
            ))
        else:
            cur.execute("""
                UPDATE users
                SET username = %s,
                    full_name = %s,
                    role = %s,
                    supervisor_id = %s
                WHERE id = %s
            """, (
                data.username,
                data.full_name,
                data.role,
                data.supervisor_id,
                data.user_id
            ))

        conn.commit()
        cur.close()
        conn.close()

        return {"success": True, "message": "User updated successfully"}

    except Exception as e:
        return {"success": False, "error": str(e)}


@app.delete("/users/delete/{user_id}")
def delete_user_api(user_id: int):
    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("DELETE FROM users WHERE id = %s", (user_id,))

        conn.commit()
        cur.close()
        conn.close()

        return {"success": True, "message": "User deleted successfully"}

    except Exception as e:
        return {"success": False, "error": str(e)}

class LogRequest(BaseModel):
    user_id: int
    action_type: str
    target_file: str = ""

@app.post("/logs/add")
def log_activity_api(data: LogRequest):
    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO activity_logs (user_id, action_type, target_file)
            VALUES (%s, %s, %s)
        """, (
            data.user_id,
            data.action_type,
            data.target_file
        ))

        conn.commit()
        cur.close()
        conn.close()

        return {
            "success": True
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }

@app.get("/logs")
def get_logs_api():
    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("""
            SELECT
                a.id,
                u.username,
                a.action_type,
                a.target_file,
                a.log_time
            FROM activity_logs a
            JOIN users u ON a.user_id = u.id
            ORDER BY a.log_time DESC
        """)

        rows = cur.fetchall()

        cur.close()
        conn.close()

        logs = []

        for row in rows:
            logs.append({
                "id": row[0],
                "username": row[1],
                "action_type": row[2],
                "target_file": row[3],
                "log_time": str(row[4])
            })

        return {
            "success": True,
            "logs": logs
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }

@app.get("/dashboard/stats/{user_id}")
def get_dashboard_stats_api(user_id: int):
    try:
        conn = get_connection()
        cur = conn.cursor()

        stats = {}

        cur.execute("SELECT COUNT(*) FROM users")
        stats["total_users"] = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM files")
        stats["total_files"] = cur.fetchone()[0]

        cur.execute("""
            SELECT COUNT(*)
            FROM files
            WHERE uploaded_by = %s
        """, (user_id,))

        stats["my_files"] = cur.fetchone()[0]

        cur.close()
        conn.close()

        return {
            "success": True,
            "stats": stats
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }

@app.get("/uploads/recent")
def get_recent_uploads_api(limit: int = 5):
    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("""
            SELECT
                f.file_name,
                u.username,
                f.uploaded_at
            FROM files f
            JOIN users u ON f.uploaded_by = u.id
            ORDER BY f.uploaded_at DESC
            LIMIT %s
        """, (limit,))

        rows = cur.fetchall()

        cur.close()
        conn.close()

        uploads = []

        for row in rows:
            uploads.append({
                "file_name": row[0],
                "username": row[1],
                "uploaded_at": str(row[2])
            })

        return {
            "success": True,
            "uploads": uploads
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }

@app.get("/files/visible/{user_id}/{role}/{username}")
def get_visible_files_api(user_id: int, role: str, username: str):
    try:
        conn = get_connection()
        cur = conn.cursor()

        if role == "admin":
            cur.execute("""
                SELECT f.id, f.file_name, f.file_type, f.file_path,
                       u.username AS uploaded_by,
                       f.uploaded_at
                FROM files f
                JOIN users u ON f.uploaded_by = u.id
                ORDER BY f.uploaded_at DESC
            """)

        elif role == "supervisor":
            cur.execute("""
                SELECT f.id, f.file_name, f.file_type, f.file_path,
                       u.username AS uploaded_by,
                       f.uploaded_at
                FROM files f
                JOIN users u ON f.uploaded_by = u.id
                WHERE f.uploaded_by = %s
                   OR f.uploaded_by IN (
                        SELECT id FROM users WHERE supervisor_id = %s
                   )
                ORDER BY f.uploaded_at DESC
            """, (user_id, user_id))

        else:
            cur.execute("""
                SELECT f.id, f.file_name, f.file_type, f.file_path,
                       u.username AS uploaded_by,
                       f.uploaded_at
                FROM files f
                JOIN users u ON f.uploaded_by = u.id
                WHERE f.uploaded_by = %s
                ORDER BY f.uploaded_at DESC
            """, (user_id,))

        rows = cur.fetchall()
        cur.close()
        conn.close()

        return {
            "success": True,
            "files": [
                {
                    "id": r[0],
                    "file_name": r[1],
                    "file_type": r[2],
                    "file_path": r[3],
                    "uploaded_by": r[4],
                    "uploaded_at": str(r[5])
                }
                for r in rows
            ]
        }

    except Exception as e:
        return {"success": False, "error": str(e)}

@app.delete("/files/delete/{file_id}/{user_id}/{role}/{username}")
def delete_file_api(file_id: int, user_id: int, role: str, username: str):
    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("""
            SELECT f.file_name, u.username
            FROM files f
            JOIN users u ON f.uploaded_by = u.id
            WHERE f.id = %s
        """, (file_id,))

        file_data = cur.fetchone()

        if not file_data:
            cur.close()
            conn.close()
            return {"success": False, "error": "File not found"}

        file_name, uploaded_by = file_data

        if role != "admin" and uploaded_by != username:
            cur.close()
            conn.close()
            return {"success": False, "error": "Permission denied"}

        cur.execute("DELETE FROM files WHERE id = %s", (file_id,))
        conn.commit()

        cur.close()
        conn.close()

        return {
            "success": True,
            "message": "File deleted successfully",
            "file_name": file_name
        }

    except Exception as e:
        return {"success": False, "error": str(e)}

UPLOAD_FOLDER = "uploads"

@app.post("/files/upload")
def upload_file_api(
    uploaded_by: int = Form(...),
    file: UploadFile = File(...)
):
    try:
        file_name = file.filename
        file_type = os.path.splitext(file_name)[1].lower()

        unique_name = f"{uuid.uuid4().hex}{file_type}"
        storage_path = f"uploads/{unique_name}"

        file_bytes = file.file.read()

        supabase.storage.from_(SUPABASE_BUCKET).upload(
            storage_path,
            file_bytes,
            {
                "content-type": file.content_type
            }
        )

        conn = get_connection()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO files (file_name, file_type, file_path, uploaded_by)
            VALUES (%s, %s, %s, %s)
        """, (file_name, file_type, storage_path, uploaded_by))

        conn.commit()
        cur.close()
        conn.close()

        return {
            "success": True,
            "message": "File uploaded to Supabase Storage successfully",
            "file_name": file_name
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }
        
@app.get("/files/download/{file_id}")
def download_file_api(file_id: int):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT file_path, file_name FROM files WHERE id = %s",
        (file_id,)
    )

    row = cur.fetchone()

    cur.close()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="File not found in database")

    storage_path, file_name = row

    try:
        file_bytes = supabase.storage.from_(SUPABASE_BUCKET).download(storage_path)

        safe_filename = quote(file_name)

        return Response(
            content=file_bytes,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{safe_filename}"
            }
        )

    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))

        
