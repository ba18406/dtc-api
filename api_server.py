from fastapi import FastAPI
import psycopg2
from pydantic import BaseModel
from fastapi import UploadFile, File, Form
import os
import shutil
from fastapi import HTTPException
from fastapi.responses import FileResponse
from supabase import create_client
import uuid
from fastapi.responses import Response

app = FastAPI()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "dtc-files")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# DATABASE CONNECTION
def get_connection():
    return psycopg2.connect(
        host="aws-1-ap-south-1.pooler.supabase.com",
        port="5432",
        dbname="postgres",
        user="postgres.dlqfrjjnkdtphjgdynpt",
        password="Bnh@99272700"
    )

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

    cur.execute("""
        INSERT INTO users (username, password, full_name, role)
        VALUES ('admin', '1234', 'System Admin', 'admin')
        ON CONFLICT (username) DO NOTHING;
    """)

    conn.commit()
    cur.close()
    conn.close()

init_database()

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
            return {
                "success": True,
                "message": "Login successful",
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

        storage_path = f"{uploaded_by}/{uuid.uuid4()}_{file_name}"
        file_bytes = file.file.read()

        supabase.storage.from_(SUPABASE_BUCKET).upload(
            storage_path,
            file_bytes
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
            "message": "File uploaded successfully",
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

        return Response(
            content=file_bytes,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{file_name}"'
            }
        )

    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))
        
