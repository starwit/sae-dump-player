import multiprocessing
import os
import shutil
import sqlite3
import uuid
from pathlib import Path
import json
from typing import Dict, Annotated

from fastapi import APIRouter, FastAPI, File, HTTPException, UploadFile, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pydantic_settings import BaseSettings

from . import player
from .player import get_streams


class Config(BaseSettings):
    redis_host: str = "localhost"
    redis_port: int = 6379
    upload_dir: Path = Path("./uploads")
    db_path: Path = Path("sqlite.db")

app = FastAPI()

app.add_middleware(
    CORSMiddleware, 
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

prefix_router = APIRouter(prefix="/api")

CONFIG = Config()
os.makedirs(CONFIG.upload_dir, exist_ok=True)

# Track running processes
active_processes: Dict[str, multiprocessing.Process] = {}

# Database setup
def init_db():
    conn = sqlite3.connect(CONFIG.db_path)
    conn.execute('''
    CREATE TABLE IF NOT EXISTS files (
        id TEXT PRIMARY KEY,
        filename TEXT NOT NULL,
        content_type TEXT,
        path TEXT NOT NULL,
        streams TEXT,
        upload_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    conn.execute('''
    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        file_id TEXT NOT NULL,
        status TEXT NOT NULL,
        mapping TEXT,
        start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (file_id) REFERENCES files (id)
    )
    ''')
    conn.commit()
    conn.close()

# Initialize database on startup
@app.on_event("startup")
async def startup_event():
    init_db()

# Helper function to get a new database connection
def get_db_connection():
    conn = sqlite3.connect(CONFIG.db_path)
    conn.row_factory = sqlite3.Row
    return conn

@app.get("/")
async def serve_index():
    return FileResponse('static/index.html')

# File endpoints
@prefix_router.post("/upload/")
async def upload_file(file: UploadFile = File(...)):
    # Connect to the database from within this request handler
    conn = get_db_connection()
    
    try:
        # Generate unique ID for the file
        file_id = str(uuid.uuid4())
        
        # Create path for saving the file
        file_path = CONFIG.upload_dir / file_id
        
        # Save the file
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        
        contained_streams = get_streams(file_path)
        
        # Store metadata in SQLite
        conn.execute(
            "INSERT INTO files (id, filename, content_type, path, streams) VALUES (?, ?, ?, ?, ?)",
            (file_id, file.filename, file.content_type, str(file_path), json.dumps(contained_streams))
        )
        conn.commit()
        
        return {"file_id": file_id, "filename": file.filename}
    
    finally:
        conn.close()

@prefix_router.delete("/files/{file_id}")
async def delete_file(file_id: str):
    conn = get_db_connection()
    
    try:
        # Check if file exists
        cursor = conn.execute("SELECT path FROM files WHERE id = ?", (file_id,))
        file_record = cursor.fetchone()
        
        if not file_record:
            raise HTTPException(status_code=404, detail="File not found")
        
        # Get file path
        file_path = file_record["path"]
        
        # Delete file from filesystem
        if os.path.exists(file_path):
            os.remove(file_path)
        
        # Remove from database
        conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        conn.commit()
        
        return {"status": "success", "message": f"File {file_id} deleted"}
    
    finally:
        conn.close()

@prefix_router.get("/files/")
async def list_files():
    conn = get_db_connection()
    
    try:
        cursor = conn.execute("SELECT * FROM files")
        files = []
        for row in cursor.fetchall():
            file = dict(row)
            file["streams"] = json.loads(row["streams"])
            files.append(file)
        
        return {"files": files}
    
    finally:
        conn.close()

class PlaybackBody(BaseModel):
    file_id: str
    mapping: Dict[str, str]

@prefix_router.post("/play")
async def start_playback(body: PlaybackBody):
    conn = get_db_connection()
    
    try:
        # Check if file exists
        cursor = conn.execute("SELECT path FROM files WHERE id = ?", (body.file_id,))
        file_record = cursor.fetchone()
        
        if not file_record:
            raise HTTPException(status_code=404, detail="File not found")
        
        file_path = file_record["path"]
        task_id = str(uuid.uuid4())

        # Create task record in database
        conn.execute(
            "INSERT INTO tasks (id, file_id, status, mapping) VALUES (?, ?, ?, ?)",
            (task_id, body.file_id, "starting", json.dumps(body.mapping))
        )
        conn.commit()
        
        # Create and start a new process
        process = multiprocessing.Process(
            target=start_player,
            args=(CONFIG, file_path, task_id, body.mapping)
        )
        process.start()
        
        # Store the process
        active_processes[task_id] = process
        
        return {"task_id": task_id, "status": "started", "file_id": body.file_id}
    
    finally:
        conn.close()

@prefix_router.get("/tasks/")
async def list_tasks():
    conn = get_db_connection()
    
    try:
        # Update status for completed processes
        for task_id, process in list(active_processes.items()):
            if not process.is_alive():
                process.join()
                active_processes.pop(task_id, None)
        
        # Get tasks from database
        cursor = conn.execute("SELECT * FROM tasks t JOIN files f on f.id == t.file_id")
        tasks = [dict(row) for row in cursor.fetchall()]
        
        # Add live process information
        for task in tasks:
            if task["id"] in active_processes:
                task["has_active_process"] = True
            else:
                task["has_active_process"] = False
            task["mapping"] = json.loads(task["mapping"]) if task["mapping"] else {}
        
        return {"tasks": tasks}
    
    finally:
        conn.close()

@prefix_router.delete("/tasks/{task_id}")
async def cancel_task(task_id: str):
    conn = get_db_connection()
    
    try:
        # Check if task exists
        cursor = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,))
        task_record = cursor.fetchone()
        
        if not task_record:
            raise HTTPException(status_code=404, detail="Task not found")
        
        # Check if there's an active process
        if task_id in active_processes and active_processes[task_id].is_alive():
            active_processes[task_id].terminate()
            active_processes[task_id].join()
            active_processes.pop(task_id, None)
            
            # Update status in database
            conn.execute(
                "UPDATE tasks SET status = ? WHERE id = ?",
                ("cancelled", task_id)
            )
            conn.commit()
            
            return {"status": "cancelled", "task_id": task_id}
        else:
            return {"status": "not_running", "task_id": task_id, "current_status": task_record["status"]}
    
    finally:
        conn.close()

# Simulate processing a log file
def start_player(config: Config, file_path: str, task_id: str, mapping: Dict[str, str]):
    # Connect to the database from this process
    conn = sqlite3.connect(config.db_path)
    
    try:
        print(f"Starting playing file {file_path} (task_id={task_id}) with mapping {mapping}")
        
        # Update status to running
        conn.execute(
            "UPDATE tasks SET status = ? WHERE id = ?",
            ("running", task_id)
        )
        conn.commit()
        
        try:
            # This blocks until the process receives SIGTERM
            player.play(file_path, config.redis_host, config.redis_port, mapping)
            
            # Update status to completed
            conn.execute(
                "UPDATE tasks SET status = ? WHERE id = ?",
                ("stopped", task_id)
            )
            conn.commit()
            print(f"Stopped playing file {file_path} (task_id={task_id})")
            
        except Exception as e:
            # Update status to failed
            conn.execute(
                "UPDATE tasks SET status = ? WHERE id = ?",
                (f"failed: {str(e)}", task_id)
            )
            conn.commit()
            print(f"Failed playing file {file_path} (task_id={task_id}): {e}")
    
    finally:
        conn.close()

app.include_router(prefix_router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)